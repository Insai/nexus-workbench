# Changelog

All notable changes to this package are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] – unreleased

The first release.

### Added

- Command line `nexus-workbench` (also installed as `nexus`):
  - `setup`: install the command with uv, register the MCP server `nexus` with Claude
    Code and Codex, copy the skills, and sign in. `--agent`, `--no-login`, `--dry-run`,
    `--yes`.
  - `login`: sign in by approving this computer in the browser and typing the code shown,
    or with `--token` by pasting a token. `--keyring` stores it in the system keyring.
  - `logout`, `whoami`, `recordings`, `doctor` and `mcp`.
  - `sample`: write a synthetic night (EDF, two hypnograms, two event files) to try the
    local tools on.
  - `qc`: describe a local EDF file and check its signal quality.
  - Error messages with a stable code, and a JSON error object under `--json`.
- MCP server `nexus` with nine read-only tools:
  - Workspace tools (need sign-in): `whoami`, `list_projects`, `list_recordings`,
    `get_recording`.
  - Local tools (no account, no network): `inspect_edf`, `check_signal_quality`,
    `compute_band_power`, `compare_hypnograms`, `compare_events`.
- Ten skills: `connect-workbench`, `edf-intake`, `signal-qc`, `band-power-by-stage`,
  `scorer-agreement`, `event-detector-eval`, `prereg`, `verify-before-verdict`,
  `leakage-check`, `stats-rails`.
- Plugins for Claude Code (`.claude-plugin/`) and Codex (`.codex-plugin/`), from the
  `insai` marketplace in this repository.
- The local tools as a Python module, `nexus_workbench.local.api`.
- Releases from a version tag: the package goes to PyPI with trusted publishing, and the
  GitHub release gets the changelog section as notes. `scripts/bump_version.py` sets the
  version in all four places it is written.

[0.1.0]: https://github.com/Insai/nexus-workbench/releases
