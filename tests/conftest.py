from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nexus_workbench import auth

LOCAL_TESTS = Path(__file__).parent / "local"


@pytest.fixture(autouse=True)
def isolated(request: pytest.FixtureRequest) -> Iterator[Path | None]:
    """Keep every CLI, client and server test away from the real config, agents and environment.

    The local file tool tests in ``tests/local`` touch none of these and keep their own
    fixtures, so they are left alone.
    """
    if Path(str(request.node.path)).is_relative_to(LOCAL_TESTS):
        yield None
        return
    tmp_path: Path = request.getfixturevalue("tmp_path")
    monkeypatch: pytest.MonkeyPatch = request.getfixturevalue("monkeypatch")
    config = tmp_path / "config"
    monkeypatch.setattr(auth, "config_dir", lambda: config)
    for name in (
        "NEXUS_TOKEN",
        "NEXUS_API_URL",
        "NEXUS_KEYRING",
        "NEXUS_ALLOW_INSECURE_LOCAL",
        "UV",
        "UV_CACHE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    yield tmp_path
