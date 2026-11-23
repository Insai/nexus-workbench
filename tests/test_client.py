from __future__ import annotations

import logging

import httpx
import pytest

from nexus_workbench import client as c
from tests.fake_api import API_BASE, FakeAPI, make_token, sample_data


def make(api: FakeAPI, token: str | None, sleeps: list[float] | None = None) -> c.WorkbenchClient:
    recorded = sleeps if sleeps is not None else []
    return c.WorkbenchClient(
        api_base=API_BASE, token=token, transport=api.transport, sleep=recorded.append
    )


def respond(status: int, json: object | None = None, text: str | None = None, **headers: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if json is not None:
            return httpx.Response(status, json=json, headers=headers)
        return httpx.Response(status, text=text or "", headers=headers)

    return c.WorkbenchClient(
        api_base=API_BASE,
        token=make_token(),
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )


# -- API base ----------------------------------------------------------------


def test_default_api_base() -> None:
    assert (
        c.resolve_api_base()
        == c.PRODUCTION_API_BASE
        == ("https://europe-west4-insai-nexus.cloudfunctions.net")
    )


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_API_URL", "https://example.test.invalid/")
    assert c.resolve_api_base() == "https://example.test.invalid"


@pytest.mark.parametrize(
    "url", ["http://example.com", "ftp://example.com", "example.com", "http://127.0.0.1:8080"]
)
def test_env_override_must_be_https(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("NEXUS_API_URL", url)
    with pytest.raises(c.ConfigError) as info:
        c.resolve_api_base()
    assert "https://" in str(info.value)
    assert "NEXUS_API_URL" in str(info.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:9000"])
def test_insecure_local_only_with_flag(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("NEXUS_API_URL", url)
    monkeypatch.setenv("NEXUS_ALLOW_INSECURE_LOCAL", "1")
    assert c.resolve_api_base() == url


def test_insecure_flag_does_not_allow_remote_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_API_URL", "http://example.com")
    monkeypatch.setenv("NEXUS_ALLOW_INSECURE_LOCAL", "1")
    with pytest.raises(c.ConfigError):
        c.resolve_api_base()


# -- requests ------------------------------------------------------------------


def test_request_shape() -> None:
    api = FakeAPI()
    token = api.add_token()
    with make(api, token) as client:
        data = client.call("whoami")
    assert data["workspace"]["name"] == "Sleep lab"
    request = api.requests[0]
    assert request.method == "POST"
    assert request.url == f"{API_BASE}/workbench_agent_api"
    assert request.headers["Authorization"] == f"Bearer {token}"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["User-Agent"].startswith("nexus-workbench/")
    assert request.content == b'{"action":"whoami"}'


def test_none_arguments_are_left_out() -> None:
    api = FakeAPI()
    sample_data(api)
    with make(api, api.add_token()) as client:
        client.call("list_recordings", projectId=None, limit=2, cursor=None)
    assert api.requests[0].content == b'{"action":"list_recordings","limit":2}'


def test_timeouts_and_redirects() -> None:
    client = c.WorkbenchClient(api_base=API_BASE, token=make_token())
    assert client._http.timeout.connect == 5.0
    assert client._http.timeout.read == 30.0
    assert client._http.follow_redirects is False
    client.close()


def test_body_size_checked_locally() -> None:
    api = FakeAPI()
    with make(api, api.add_token()) as client, pytest.raises(c.InvalidArgument):
        client.call("get_recording", id="x" * 3000)
    assert api.requests == []


def test_uses_stored_token() -> None:
    from nexus_workbench import auth

    api = FakeAPI()
    token = api.add_token()
    auth.save_token(API_BASE, token)
    with make(api, None) as client:
        assert client.call("whoami")["role"] == "member"


def test_not_signed_in() -> None:
    api = FakeAPI()
    with make(api, None) as client, pytest.raises(c.NotSignedIn) as info:
        client.call("whoami")
    assert "nexus-workbench login" in str(info.value)
    assert api.requests == []


# -- errors (contract 3.1) ----------------------------------------------------------

ERROR_CASES = [
    (400, "invalid_argument", c.InvalidArgument, "did not accept"),
    (401, "unauthenticated", c.Unauthenticated, "nexus-workbench login"),
    (401, "token_expired", c.TokenExpired, "expired"),
    (403, "forbidden", c.Forbidden, "workspace owner"),
    (403, "early_access_required", c.EarlyAccessRequired, c.REQUEST_ACCESS_HINT),
    (404, "not_found", c.NotFound, "does not exist"),
    (405, "invalid_argument", c.InvalidArgument, "did not accept"),
    (409, "token_limit", c.TokenLimit, "limit of 10 tokens"),
    (429, "rate_limited", c.RateLimited, "Wait"),
    (500, "internal", c.InternalError, "Try again"),
]


@pytest.mark.parametrize(("status", "code", "exc", "needle"), ERROR_CASES)
def test_error_codes(status: int, code: str, exc: type, needle: str) -> None:
    body = {"error": {"code": code, "message": "Server says no."}}
    client = respond(status, json=body, **({"Retry-After": "60"} if status == 429 else {}))
    with pytest.raises(exc) as info:
        client.call("whoami")
    assert needle in str(info.value)
    assert info.value.code == code
    assert info.value.status == status


def test_early_access_message_is_exact() -> None:
    body = {"error": {"code": "early_access_required", "message": "x"}}
    with pytest.raises(c.EarlyAccessRequired) as info:
        respond(403, json=body).call("whoami")
    assert str(info.value) == (
        "Agent access is not on for this workspace yet. "
        "Request it in Workbench: account menu, Connect an agent, Request agent access."
    )


def test_invalid_argument_includes_server_sentence() -> None:
    body = {"error": {"code": "invalid_argument", "message": "limit must be\n1 to 100."}}
    with pytest.raises(c.InvalidArgument) as info:
        respond(400, json=body).call("list_recordings", limit=500)
    assert "limit must be 1 to 100." in str(info.value)


def test_404_without_body_is_early_access() -> None:
    with pytest.raises(c.EarlyAccessRequired) as info:
        respond(404, text="<html>Not Found</html>").call("whoami")
    assert "Agent access is not on" in str(info.value)
    assert c.REQUEST_ACCESS_HINT in str(info.value)


def test_fake_server_404_is_early_access() -> None:
    api = FakeAPI(agent_api_available=False)
    with make(api, api.add_token()) as client, pytest.raises(c.EarlyAccessRequired):
        client.call("whoami")


def test_unknown_code_falls_back_on_status() -> None:
    with pytest.raises(c.Forbidden):
        respond(403, json={"error": {"code": "something_new", "message": "x"}}).call("whoami")


def test_redirect_is_not_followed() -> None:
    with pytest.raises(c.UnexpectedResponse) as info:
        respond(302, text="", Location="https://elsewhere.invalid/").call("whoami")
    assert "redirect" in str(info.value)


def test_non_json_success_is_unexpected() -> None:
    with pytest.raises(c.UnexpectedResponse):
        respond(200, text="hello").call("whoami")


def test_real_fake_server_errors() -> None:
    api = FakeAPI()
    sample_data(api)
    expired = api.add_token(expired=True)
    gone = api.add_token(workspace_ok=False)
    off = api.add_token(early_access=False)
    with make(api, make_token()) as client, pytest.raises(c.Unauthenticated):
        client.call("whoami")
    with make(api, expired) as client, pytest.raises(c.TokenExpired):
        client.call("whoami")
    with make(api, gone) as client, pytest.raises(c.Forbidden):
        client.call("whoami")
    with make(api, off) as client, pytest.raises(c.EarlyAccessRequired):
        client.call("whoami")
    ok = api.add_token()
    with make(api, ok) as client:
        with pytest.raises(c.NotFound):
            client.call("get_recording", id="e_missing")
        with pytest.raises(c.NotFound):
            client.call("list_recordings", projectId="p_missing")
        with pytest.raises(c.InvalidArgument):
            client.call("no_such_action")


# -- retries --------------------------------------------------------------------------


def test_retry_on_429_honours_retry_after() -> None:
    api = FakeAPI(rate_limit_next=["3"])
    sleeps: list[float] = []
    with make(api, api.add_token(), sleeps) as client:
        assert client.call("whoami")["role"] == "member"
    assert sleeps == [3.0]
    assert len(api.requests) == 2


def test_no_retry_when_retry_after_too_long() -> None:
    api = FakeAPI(rate_limit_next=["30"])
    sleeps: list[float] = []
    with make(api, api.add_token(), sleeps) as client, pytest.raises(c.RateLimited) as info:
        client.call("whoami")
    assert sleeps == []
    assert info.value.retry_after == 30.0
    assert "30 seconds" in str(info.value)
    assert len(api.requests) == 1


def test_only_one_retry_on_429() -> None:
    api = FakeAPI(rate_limit_next=["1", "1", "1"])
    sleeps: list[float] = []
    with make(api, api.add_token(), sleeps) as client, pytest.raises(c.RateLimited):
        client.call("whoami")
    assert sleeps == [1.0]
    assert len(api.requests) == 2


def test_retry_once_on_connection_error() -> None:
    api = FakeAPI(fail_next=["connect"])
    with make(api, api.add_token()) as client:
        assert client.call("whoami")["role"] == "member"
    assert len(api.requests) == 2


def test_connection_error_twice_is_network_error() -> None:
    api = FakeAPI(fail_next=["connect", "connect"])
    with make(api, api.add_token()) as client, pytest.raises(c.NetworkError) as info:
        client.call("whoami")
    assert "internet connection" in str(info.value)


def test_read_timeout_is_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    client = c.WorkbenchClient(
        api_base=API_BASE, token=make_token(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(c.NetworkError) as info:
        client.call("whoami")
    assert "too long" in str(info.value)
    assert len(calls) == 1


# -- secrecy ---------------------------------------------------------------------------


def test_token_never_logged_or_in_errors(caplog: pytest.LogCaptureFixture, capsys) -> None:
    caplog.set_level(logging.DEBUG)
    api = FakeAPI(rate_limit_next=["1"], fail_next=["connect"])
    token = make_token()  # unknown to the server
    texts: list[str] = []
    client = make(api, token)
    for _ in range(3):
        try:
            client.call("whoami")
        except c.WorkbenchError as exc:
            texts.append(str(exc))
            texts.append(repr(exc))
            texts.append(repr(exc.args))
    texts.append(repr(client))
    for code, status in [("unauthenticated", 401), ("internal", 500), ("weird", 418)]:
        try:
            respond(status, json={"error": {"code": code, "message": token}}).call("whoami")
        except c.WorkbenchError as exc:
            texts.append(str(exc))
    captured = capsys.readouterr()
    blob = "\n".join([*texts, caplog.text, captured.out, captured.err])
    assert token not in blob
    assert token[8:24] not in blob
    assert "Bearer" not in blob


# -- section 5 client side ---------------------------------------------------------------


def test_login_start_and_exchange() -> None:
    api = FakeAPI()
    with make(api, None) as client:
        session = client.cli_login_start(device_name="x" * 100)
        code = api.approve(session["approvalUrl"])
        data = client.cli_login_exchange(
            session_id=session["sessionId"], poll_secret=session["pollSecret"], code=code
        )
    assert data["token"] in api.tokens
    start = api.requests[0]
    assert "Authorization" not in start.headers
    import json

    body = json.loads(start.content)
    assert body["action"] == "start"
    assert len(body["deviceName"]) == 64


def test_login_404_means_not_available() -> None:
    api = FakeAPI(login_available=False)
    with make(api, None) as client, pytest.raises(c.LoginNotAvailable) as info:
        client.cli_login_start(device_name="laptop")
    assert "Connect an agent" in str(info.value)


def test_code_wrong_carries_attempts_left() -> None:
    api = FakeAPI()
    with make(api, None) as client:
        session = client.cli_login_start(device_name="laptop")
        api.approve(session["approvalUrl"])
        with pytest.raises(c.CodeWrong) as info:
            client.cli_login_exchange(
                session_id=session["sessionId"], poll_secret=session["pollSecret"], code="AAAAAAAA"
            )
    assert info.value.attempts_left == 4
    assert "4 attempts left" in str(info.value)


def test_wrong_poll_secret_is_login_expired() -> None:
    api = FakeAPI()
    with make(api, None) as client:
        session = client.cli_login_start(device_name="laptop")
        code = api.approve(session["approvalUrl"])
        with pytest.raises(c.LoginExpired) as info:
            client.cli_login_exchange(session_id=session["sessionId"], poll_secret="x", code=code)
    assert "nexus-workbench login" in str(info.value)
    assert api.sessions[session["sessionId"]].attempts_left == 5


def test_approval_url_must_be_https() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sessionId": "a" * 32,
                "pollSecret": "b" * 43,
                "approvalUrl": "file:///etc/passwd",
            },
        )

    client = c.WorkbenchClient(api_base=API_BASE, transport=httpx.MockTransport(handler))
    with pytest.raises(c.ConfigError):
        client.cli_login_start(device_name="laptop")
