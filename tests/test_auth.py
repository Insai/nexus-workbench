from __future__ import annotations

import json
import os
import stat
import sys
import zlib

import pytest

from nexus_workbench import auth
from tests.fake_api import make_token

API = "https://api.test.invalid"
OTHER = "https://staging.test.invalid"
posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


# -- token format -----------------------------------------------------------


def test_generated_token_is_valid() -> None:
    token = make_token()
    assert len(token) == 57
    assert auth.TOKEN_RE.match(token)
    auth.validate_token(token)


def test_check_digits_follow_contract() -> None:
    body = "0" * 43
    crc = zlib.crc32(body.encode("ascii"))
    digits = []
    n = crc
    while n:
        n, r = divmod(n, 62)
        digits.append(auth.BASE62[r])
    expected = "".join(reversed(digits)).rjust(6, "0")
    assert auth.check_digits(body) == expected
    auth.validate_token("nxs_pat_" + body + expected)


def test_base62_padding() -> None:
    assert auth.base62_encode(0, 6) == "000000"
    assert auth.base62_encode(61, 6) == "00000z"
    assert auth.base62_encode(62, 6) == "000010"


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        ("hello", "starts with nxs_pat_"),
        ("nxs_pat_short", "57"),
        ("nxs_pat_" + "!" * 49, "characters"),
    ],
)
def test_validate_rejects(bad: str, needle: str) -> None:
    with pytest.raises(auth.InvalidTokenError) as info:
        auth.validate_token(bad)
    assert needle in str(info.value)


def test_validate_rejects_bad_check_digits_without_echoing_token() -> None:
    token = make_token()
    last = token[-1]
    broken = token[:-1] + ("A" if last != "A" else "B")
    with pytest.raises(auth.InvalidTokenError) as info:
        auth.validate_token(broken)
    assert "check" in str(info.value)
    assert broken not in str(info.value)
    assert broken[8:20] not in str(info.value)


def test_mask_shows_last4_only() -> None:
    token = make_token()
    assert auth.mask_token(token) == f"nxs_pat_…{token[-4:]}"


def test_token_id_matches_contract() -> None:
    import hashlib

    token = make_token()
    assert auth.token_id(token) == "tok_" + hashlib.sha256(token.encode()).hexdigest()[:16]


# -- storage ----------------------------------------------------------------


def test_save_and_load_roundtrip() -> None:
    token = make_token()
    assert auth.save_token(API, token, workspace_id="w_1") == "file"
    stored = auth.load_token(API)
    assert stored.token == token
    assert stored.source == "file"
    assert stored.workspace_id == "w_1"
    assert stored.token_id == auth.token_id(token)
    assert stored.saved_at and stored.saved_at.endswith("Z")


def test_file_shape() -> None:
    token = make_token()
    auth.save_token(API, token, workspace_id="w_1")
    data = json.loads(auth.credentials_path().read_text())
    assert set(data) == {API}
    assert set(data[API]) == {"token", "tokenId", "workspaceId", "savedAt"}


@posix_only
def test_file_and_directory_modes() -> None:
    auth.save_token(API, make_token())
    path = auth.credentials_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@posix_only
def test_existing_loose_directory_is_tightened() -> None:
    directory = auth.config_dir()
    directory.mkdir(parents=True)
    os.chmod(directory, 0o755)
    auth.save_token(API, make_token())
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_per_api_base_storage() -> None:
    a, b = make_token(), make_token()
    auth.save_token(API, a)
    auth.save_token(OTHER, b)
    assert auth.load_token(API).token == a
    assert auth.load_token(OTHER).token == b
    assert auth.delete_token(API) is True
    assert auth.load_token(API).token is None
    assert auth.load_token(OTHER).token == b
    assert auth.delete_token(API) is False


def test_atomic_write_uses_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    auth.save_token(API, make_token())
    before = auth.credentials_path().read_text()
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def failing_replace(src: str, dst: str) -> None:
        calls.append((str(src), str(dst)))
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        auth.save_token(OTHER, make_token())
    monkeypatch.setattr(os, "replace", real_replace)
    # The old file is untouched and no temp file is left behind.
    assert auth.credentials_path().read_text() == before
    assert calls and calls[0][1] == str(auth.credentials_path())
    leftovers = [p.name for p in auth.config_dir().iterdir() if p.name != "credentials.json"]
    assert leftovers == []


def test_env_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    saved, env = make_token(), make_token()
    auth.save_token(API, saved)
    monkeypatch.setenv("NEXUS_TOKEN", env)
    stored = auth.load_token(API)
    assert stored.token == env
    assert stored.source == "env"


def test_bad_env_token_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_TOKEN", "nxs_pat_nope")
    with pytest.raises(auth.InvalidTokenError) as info:
        auth.load_token(API)
    assert "NEXUS_TOKEN" in str(info.value)


@posix_only
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_refuses_loose_permissions(mode: int) -> None:
    auth.save_token(API, make_token())
    path = auth.credentials_path()
    os.chmod(path, mode)
    with pytest.raises(auth.CredentialsError) as info:
        auth.load_token(API)
    message = str(info.value)
    assert "chmod 600" in message
    assert str(path) in message


def test_missing_file_means_signed_out() -> None:
    stored = auth.load_token(API)
    assert stored.token is None
    assert stored.source == "none"


def test_corrupt_file_has_a_fix() -> None:
    path = auth.credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    os.chmod(path, 0o600)
    with pytest.raises(auth.CredentialsError) as info:
        auth.load_token(API)
    assert "login" in str(info.value)


def test_save_rejects_invalid_token() -> None:
    with pytest.raises(auth.InvalidTokenError):
        auth.save_token(API, "nxs_pat_bad")
    assert not auth.credentials_path().exists()


# -- keyring ----------------------------------------------------------------


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def delete_password(self, service: str, user: str) -> None:
        self.store.pop((service, user), None)


def test_keyring_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeKeyring()
    monkeypatch.setitem(sys.modules, "keyring", fake)
    token = make_token()
    assert auth.save_token(API, token, use_keyring=True) == "keyring"
    data = json.loads(auth.credentials_path().read_text())
    assert data[API]["token"] is None
    assert token not in auth.credentials_path().read_text()
    stored = auth.load_token(API)
    assert stored.token == token
    assert stored.source == "keyring"
    auth.delete_token(API)
    assert fake.store == {}


def test_keyring_error_falls_back_to_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken:
        def set_password(self, *args: object) -> None:
            raise RuntimeError("no backend")

    monkeypatch.setitem(sys.modules, "keyring", Broken())
    token = make_token()
    assert auth.save_token(API, token, use_keyring=True) == "file"
    err = capsys.readouterr().err
    assert "keyring" in err
    assert err.count("\n") == 1
    assert auth.load_token(API).token == token


def test_keyring_not_installed_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # import raises ImportError
    assert auth.save_token(API, make_token(), use_keyring=True) == "file"


def test_keyring_not_imported_by_default() -> None:
    import subprocess

    code = (
        "import sys, nexus_workbench.auth, nexus_workbench.cli, nexus_workbench.mcp_server;"
        "print('keyring' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_keyring_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    assert auth.keyring_requested() is False
    monkeypatch.setenv("NEXUS_KEYRING", "1")
    assert auth.keyring_requested() is True
