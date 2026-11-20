from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nexus_workbench import auth


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Keep every test away from the real config, agents and environment."""
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
