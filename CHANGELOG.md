# Changelog

All notable changes to this package are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.2.0] – unreleased

Agent API version 1.1 (contract section 7): scopes, per-epoch and per-event reads, and
writes that land as a review layer. Version 1.1 is on staging and used internally first,
before any outside workspace sees it.

### Added

- Scopes. `nexus-workbench login --scope <scope>` (repeatable; `read` is always included)
  asks for `detail`, `marks:write`, `tasks:write` or `analysis:run`. The approved scopes
  are saved with the token and shown by `login`, `whoami` and `doctor`. `whoami` also shows
  your member id.
- MCP tools, each with typed output and annotations:
  - Read (`read` scope): `list_members`, `list_tasks`, `list_nights`, `list_layers`.
  - Detail (`detail` scope): `get_events`, `get_stages`, `get_quality`, `list_marks`.
  - Write: `propose_marks` and `withdraw_marks` (`marks:write`), `create_review_task`
    (`tasks:write`), `request_analysis` (`analysis:run`).
- The server lists detail and write tools only when the saved token has their scope,
  read from the credentials file at start with no call. Instructions say what writes do.
- Local tool `preprocess` (and `nexus-workbench preprocess`): band-pass, 50 or 60 Hz notch,
  resample and channel choice into a new EDF file. The input is never changed.
- `compare_hypnograms` and `compare_events` take one side from a Workbench recording with
  `recording_id` (machine staging or your own scoring; machine events).
- Commands `members`, `tasks`, `nights`, `layers`, `events`, `stages`, `quality`, `marks`,
  `propose` (with `--dry-run`), `withdraw`, `task give` and `analyse`, each with `--json`
  and an example in `--help`. `events --save` and `stages --save` write files the local
  comparisons read. Tables have aligned columns and respect `NO_COLOR`.
- Error codes `scope_required`, `write_access_required`, `already_analysed`,
  `mark_limit`, `too_large` and `not_available`, with a sentence and the next step. A
  version 1 server's answer to a 1.1 action (400 `Unknown action.`) reads as "not
  available on this server yet".
- `create_review_task` returns `mailed`, and `task give` says whether the member was
  emailed (the server sends at most one a minute and five an hour per member).
- `login --scope` with a write scope says that the approval page refuses write scopes in
  a workspace without agent writes, and a refusal reads as one clear sentence.
- Skills `review-loop`, `nights-by-person` and `preprocess-and-qc`. `connect-workbench`
  covers scopes, the agent writes switch and what writes do; `event-detector-eval` and
  `scorer-agreement` cover workspace events and stages.

### Changed

- The contract and client match what the 1.1 server does: `request_analysis` is checked
  and then answered with 501 `not_available` (nothing is started until the analysis
  service takes a service identity), so the tool, `analyse`, the docs and the
  `review-loop` skill say to press Analyse on the recording page; `approve` refuses write
  scopes while agent writes are off; switching agent access off also switches writes off;
  `get_events` and `list_marks` are limited to 10 calls a minute; `get_events` refuses
  events over 20 MB with `too_large`; a `list_marks` page may be short and still carry a
  cursor; cursors are signed; `list_layers` hides blind-desk recordings too and reads the
  newest 1,000 recordings; `get_quality` sends no `railed` share today; a token name may
  not hold an email address or an id.
- Comparisons that read workspace events read at most 10 pages (10,000 rows), to stay
  inside the 10 calls a minute of `get_events`.
- The MCP server is no longer read-only when the token has a write scope and the workspace
  takes agent writes. Without a write scope nothing is written, as before.
- `compare_hypnograms` and `compare_events` are marked open-world, since they can read
  Workbench.
- `preprocess` skips a notch or a high edge at or above half a channel's sample rate on
  that channel, where there is nothing to remove, and names each skip in `notes` (printed
  as `Note:` lines). The sample night at 100 Hz now takes `--low 0.3 --high 35 --notch 50`.
  `applied` lists only what ran on at least one channel.
- `preprocess` no longer clips a channel whose filtered signal leaves the input's physical
  range. That channel gets a wider range, fitted to the filtered data, and the full 16-bit
  digital range; a channel that still fits keeps its range. `clippedSamples` is 0 on the
  sample night (it was 538 on E1-M2).
- Tool descriptions say how to page (omit `cursor` for the first page, pass `nextCursor`
  for the next), what `kinds` in `get_events` means, and that `list_recordings` rows carry
  the quality grade and score. A `cursor` of `""` or `"null"` asks for the first page.
- A missing scope names the `login` command for that scope, and through the MCP server
  adds "then restart the agent session".

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

[0.2.0]: https://github.com/Insai/nexus-workbench/releases
[0.1.0]: https://github.com/Insai/nexus-workbench/releases
