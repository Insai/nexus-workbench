# Changelog

All notable changes to this package are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `nexus-workbench channels show | set | clear`: read and set the electrode sites of a
  recording's channels through the agent API (`get_channels`, `set_channels`, contract
  section 7.12; staging only for now). A recording is named by its label as members see it
  or by its id. `set` and `clear` print the old and new sites and the command that undoes
  the change. Exit 3 when Workbench refuses the change (the code, the reason and what to do
  are printed), 4 on sign-in, token scope or workspace access.
- The `channels:write` scope (a write scope: `nexus-workbench login --scope channels:write`).
  A token without it gets `scope_required` with the exact `login` command.

### Changed

- `nexus-workbench login` waits for you to approve in the browser, then asks for the code.
  When the page says none of your workspaces has agent access yet, or you are not in a
  workspace yet, it explains (again only if the reason changes) and keeps waiting until
  approval, denial or expiry, so you can switch browser accounts or pass the link to someone
  who can approve. The reason is advisory: it comes from whoever opened the page last, so
  someone else opening the link cannot end your sign-in. A denial and an expiry are told
  apart by design. If the 10-minute sign-in runs out while a reason is showing, it exits 1
  instead of waiting at `Code:`, and tells you where in Workbench to request access (a page
  that does not expire, not the approval link). A network blip while waiting is retried with backoff; after a few in
  a row, or on a Workbench without this check (contract section 5.1, `status`), you get the
  code prompt, as before.

## [0.2.1] - 2026-11-24

Small changes that help an agent on its first try.

### Added

- When a tool cannot run, its error now also comes as structured data: a stable `code`
  from docs/contract.md, the reason, `howToFix` (the exact commands to run in your own
  terminal, such as `nexus-workbench login --scope marks:write`), `localAlternative` (the
  local tools that still work, for example when you are signed out or agent access is not
  on yet), `retryAfter` after a rate limit, and `requestId` when the server sends one. The
  sentence for you is still there. No step ever asks for a token in the chat.
- Four MCP prompts: `qc_edf_file`, `scorer_agreement`, `band_power_by_stage` and
  `review_workspace_recordings`. The first three work signed out on the files
  `nexus-workbench sample` writes.
- A one-line notice when a newer version is on PyPI: in one tool result per session and in
  `doctor`. It checks at most once a day, waits at most two seconds, and says nothing when
  offline. `NEXUS_WORKBENCH_NO_UPDATE_CHECK=1` turns it off.
- `server.json` for the MCP Registry and `llms-install.md` with install steps for agents.
- "Try it in 60 seconds" in the README: three things to ask that work signed out.

### Changed

- The command line prints the server's request id after an error message when there is
  one (with `--json`, as `requestId`).
- `scripts/bump_version.py` also sets the version in `server.json`.

## [0.2.0] - 2026-11-24

The first public release. It connects Claude Code and Codex to an Insai Workbench
workspace and adds local tools for EEG files. It implements agent API version 1.1
(docs/contract.md): scopes, per-epoch and per-event reads, and writes that land as a
review layer for members to review. Writes work only in a workspace where Insai staff
have switched on agent writes.

### Added

- Command line `nexus-workbench` (also installed as `nexus`):
  - `setup`: install the command with uv, register the MCP server `nexus` with Claude
    Code and Codex, copy the skills, and sign in. `--agent`, `--no-login`, `--dry-run`,
    `--yes`.
  - `login`: sign in by approving this computer in the browser and typing the code shown,
    or with `--token` by pasting a token. `--keyring` stores it in the system keyring.
  - `logout`, `whoami`, `recordings`, `doctor` and `mcp`.
  - `members`, `tasks`, `nights`, `layers`, `events`, `stages`, `quality`, `marks`,
    `propose` (with `--dry-run`), `withdraw`, `task give` and `analyse`, each with `--json`
    and an example in `--help`. `events --save` and `stages --save` write files the local
    comparisons read. Tables have aligned columns and respect `NO_COLOR`.
  - `sample`: write a synthetic night (EDF, two hypnograms, two event files) to try the
    local tools on.
  - `qc` and `preprocess` for local EDF files.
  - Error messages with a stable code and the next step, and a JSON error object under
    `--json`.
- Scopes. `nexus-workbench login --scope <scope>` (repeatable; `read` is always included)
  asks for `detail`, `marks:write`, `tasks:write` or `analysis:run`. The approved scopes
  are saved with the token and shown by `login`, `whoami` and `doctor`. The approval page
  refuses write scopes in a workspace without agent writes.
- MCP server `nexus` with 22 tools, each with typed output and annotations:
  - Read (`read` scope): `whoami`, `list_projects`, `list_recordings`, `get_recording`,
    `list_members`, `list_tasks`, `list_nights`, `list_layers`.
  - Detail (`detail` scope): `get_events`, `get_stages`, `get_quality`, `list_marks`.
  - Write: `propose_marks` and `withdraw_marks` (`marks:write`), `create_review_task`
    (`tasks:write`), `request_analysis` (`analysis:run`). Proposed marks form a review
    layer of the token's own and never change anyone's scoring. `request_analysis` is
    checked and then answered with `not_available` for now; use Analyse on the recording
    page.
  - Local (no account, no network): `inspect_edf`, `check_signal_quality`,
    `compute_band_power`, `compare_hypnograms`, `compare_events`, `preprocess`.
    `preprocess` band-passes, notches, resamples and picks channels into a new EDF file
    and never changes the input. `compare_hypnograms` and `compare_events` can take one
    side from a Workbench recording.
- The server lists detail and write tools only when the saved token has their scope,
  read from the credentials file at start with no call.
- Thirteen skills: `connect-workbench`, `edf-intake`, `signal-qc`, `preprocess-and-qc`,
  `band-power-by-stage`, `scorer-agreement`, `event-detector-eval`, `review-loop`,
  `nights-by-person`, `prereg`, `verify-before-verdict`, `leakage-check`, `stats-rails`.
- Plugins for Claude Code (`.claude-plugin/`) and Codex (`.codex-plugin/`), from the
  `insai` marketplace in this repository.
- The local tools as a Python module, `nexus_workbench.local.api`.
- Requesting agent access. A workspace without agent access answers "Agent access is not
  on for this workspace yet" and points to Request agent access in Workbench (account
  menu, Connect an agent). The local tools work without it.
- Releases from a version tag: the package goes to PyPI with trusted publishing, and the
  GitHub release gets the changelog section as notes. `scripts/bump_version.py` sets the
  version in all four places it is written.

[0.2.1]: https://github.com/Insai/nexus-workbench/releases/tag/v0.2.1
[0.2.0]: https://github.com/Insai/nexus-workbench/releases/tag/v0.2.0
