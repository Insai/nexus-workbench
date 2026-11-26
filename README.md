# nexus-workbench

<!-- mcp-name: io.github.Insai/nexus-workbench -->

One command adds Insai Workbench to Claude Code or Codex: `uvx nexus-workbench setup`.
It gives your agent an MCP server named `nexus` with 22 tools and four prompts, and
thirteen skills for sleep-EEG research. Six tools work on EDF files on your own computer, with no account. The others
read your Workbench workspace, where sleep researchers review EEG recordings, and, when
you ask for it at sign-in, propose marks for review, give review tasks or ask for an
analysis.

Local tools send nothing anywhere. Workspace tools send only your token and the request
to the Workbench API over HTTPS. Once a day at most, the server asks PyPI whether a newer
version exists (see [Update](#update)). There is no telemetry.

A token reads, and nothing more, unless you ask for more scopes when you sign in. See
[Writing to Workbench](#writing-to-workbench).

**Early access.** Agent access is switched on per workspace. Request it in Workbench:
open the account menu, choose Connect an agent, then Request agent access. The local tools
work without it.

For research use only. Not for clinical decision-making.

## Install

You need [uv](https://docs.astral.sh/uv/). Then run:

```sh
uvx nexus-workbench setup
```

This installs the `nexus-workbench` command, registers the MCP server with Claude Code
and Codex, copies the skills and signs you in. See [What setup does](#what-setup-does).

The command is also installed as `nexus`, a shorter name for the same thing. Agent
configs always use `nexus-workbench`.

## Try it in 60 seconds (no account)

```sh
uvx nexus-workbench setup --no-login
nexus-workbench sample
nexus-workbench qc nexus-sample/sample_night.edf
```

`sample` writes a synthetic night into `./nexus-sample/`: an EDF file, two scorers'
hypnograms and two event files. Nothing in it comes from a recording. `qc` prints the
file's format and length, and one row per channel with the share of ok, suspect, flat and
railed epochs.

Then start Claude Code or Codex in the same folder and ask:

> Check the signal quality of ./nexus-sample/sample_night.edf

> Compare ./nexus-sample/sample_hypnogram.csv with ./nexus-sample/sample_hypnogram_scorer2.txt

> Compute band power per sleep stage for ./nexus-sample/sample_night.edf with the hypnogram ./nexus-sample/sample_hypnogram.csv

All three use the local tools, so they work signed out and send nothing anywhere. The
same three are also MCP prompts (`qc_edf_file`, `scorer_agreement`,
`band_power_by_stage`); see [Prompts](#prompts).

## What setup does

`setup` does four things and tells you what it did:

1. Installs `nexus-workbench` with `uv tool install`, so it keeps working in new
   terminals. It installs the same version that is running
   (`nexus-workbench==<version>`). uv's own settings, such as `UV_INDEX_URL`, apply as
   usual.
2. Registers the MCP server with Claude Code (`claude mcp add --scope user`)
   and Codex (`codex mcp add`), using the full path to the installed command.
   An agent that is not installed is skipped.
3. Copies the packaged skills into `~/.claude/skills/` and `~/.codex/skills/`
   (or `$CLAUDE_CONFIG_DIR/skills/` and `$CODEX_HOME/skills/`). Each skill folder it
   writes holds a `.nexus-workbench` marker file. A skill folder that is already there
   and has no marker is left alone.
4. Signs you in to Workbench, unless you are signed in already. If no sign-in is
   finished (you cancel, or there is no input), this step is skipped, `setup` still
   exits 0, and it tells you what to try next.

Options: `--agent claude|codex|all`, `--no-login`, `--dry-run` (print every
command and file write, change nothing), `--yes` (do not ask first). Running
`setup` again changes nothing that is already in place.

`setup` asks before it changes anything. When standard input is not a terminal
(an agent or CI runs it), there is no one to ask, so it goes ahead as if you
had passed `--yes`, and prints one line saying so.

A new MCP server shows up in a new agent session. Restart Claude Code or Codex after
`setup`.

### Codex

`setup` registers the server and copies the skills for Codex too, when the `codex`
command is on your PATH. Use `--agent codex` to set up Codex only.

### Or: install the plugin

Instead of `setup`, you can install the plugin. It starts the server with
`uvx nexus-workbench mcp` and brings the skills.

In Claude Code:

```text
/plugin marketplace add Insai/nexus-workbench
/plugin install nexus-workbench@insai
```

In Codex:

```sh
codex plugin marketplace add Insai/nexus-workbench
codex plugin add nexus-workbench@insai
```

Then run `uvx nexus-workbench login` to sign in. The plugin and `setup` both register
a server named `nexus`, so use one or the other, not both.

## Sign in

`nexus-workbench login` opens Workbench in your browser. Approve this computer there,
then type the code it shows (8 characters, like ABCD-EFGH) into the terminal. The token
it makes lasts 90 days and is saved only on this computer.

If the browser flow is not available, or you prefer a token: in Workbench open the
account menu, choose "Connect an agent", make a token, then run
`nexus-workbench login --token` and paste it.

Instead of saving a token, you can set the `NEXUS_TOKEN` environment variable to it.

Signed out, the local tools work and need no account. The workspace tools answer
"Not signed in" and tell the agent to ask you to run `nexus-workbench login`.

### Scopes

A token reads by default. To let it do more, name the scopes when you sign in:

```sh
nexus-workbench login --scope detail --scope marks:write
```

| Scope | Lets the token | Tools |
|---|---|---|
| `read` | Read projects, recordings, nights, layers, tasks and members. Always included. | the read tools |
| `detail` | Read per-epoch stages and quality, per-event events and marks. | `get_events`, `get_stages`, `get_quality`, `list_marks` |
| `marks:write` | Propose marks as a review layer, and withdraw them. | `propose_marks`, `withdraw_marks` |
| `tasks:write` | Give review tasks to members. | `create_review_task` |
| `analysis:run` | Ask for an analysis of a recording you uploaded (not available yet, see below). | `request_analysis` |
| `channels:write` | Set the electrode sites of a recording's channels (staging only). | none: the `channels set` and `channels clear` commands |

The approval page shows the scopes before you approve, and the person approving may give
fewer. Write scopes can be approved only in a workspace where Insai has switched agent
writes on; elsewhere the approval page refuses them, so sign in again without them. The
scopes the token got are saved with it and shown by `whoami` and `doctor`.
Scopes never widen: to add one, sign in again. A token pasted with `login --token` keeps
the scopes it was made with on the page.

The MCP server lists the detail and write tools only when the saved token has their
scope. It reads the scopes from the credentials file when it starts, without a call, so
restart Claude Code or Codex after signing in with new scopes. With `NEXUS_TOKEN` the
scopes are not known without a call, so the server lists the read tools only.

## Commands

| Command | What it does |
|---|---|
| `nexus-workbench setup` | Install, register the MCP server, install skills, sign in. |
| `nexus-workbench login` | Sign in: approve this computer in the browser and type the code shown. `--scope` asks for more than reading (repeat it). `--token` pastes a token instead. |
| `nexus-workbench login --token` | Paste a token made in Workbench. Piped input works too: `nexus-workbench login --token < token.txt`. A token is never accepted as a command-line argument. |
| `nexus-workbench logout` | Remove the saved token from this computer. |
| `nexus-workbench whoami` | Show the workspace your token reads, your member id and the token's scopes. |
| `nexus-workbench recordings` | List recordings (`--project`, `--limit`, `--cursor`, `--json`). |
| `nexus-workbench members` | List the workspace's members as opaque ids, with their role. |
| `nexus-workbench tasks` | List review tasks given to you (`--all` adds the ones you gave). |
| `nexus-workbench nights` | List nights (recordings recorded together) with an opaque person key (`--project`). |
| `nexus-workbench layers` | List analysis and agent layers and how many recordings each covers (`--project`). |
| `nexus-workbench events ID` | List a recording's machine events (`--kind`, `--limit`, `--cursor`; `--save FILE` writes every event as a file `compare_events` reads). Needs `detail`. |
| `nexus-workbench stages ID` | Minutes per stage from the machine staging, or your own scoring with `--source mine` (`--save FILE` writes a hypnogram `compare_hypnograms` reads). Needs `detail`. |
| `nexus-workbench quality ID` | A recording's signal quality grade and a row per channel. Needs `detail`. |
| `nexus-workbench marks ID` | A recording's marks (`--origin` `human`, `machine` or `agent`; `--limit`, `--cursor`). Needs `detail`. |
| `nexus-workbench propose ID FILE` | Propose marks from a CSV or JSON file as a review layer. `--dry-run` checks the file and sends nothing. Needs `marks:write`. |
| `nexus-workbench withdraw ID` | Withdraw marks this token proposed (`--mark ID`, repeat for more; without it all, after a question or `--yes`). Needs `marks:write`. |
| `nexus-workbench task give IDS` | Give a member review tasks (`--member`, `--mode` `blind` or `assisted`, `--confirm-machine-seen`). Says whether the member was emailed. Needs `tasks:write`. |
| `nexus-workbench analyse ID` | Ask for an analysis of a recording you uploaded. Needs `analysis:run`. Not available yet: Workbench checks the request and answers `not_available`; use Analyse on the recording page. |
| `nexus-workbench preprocess PATH` | Band-pass, notch, resample or pick channels of a local EDF into a new file (`--low`, `--high`, `--notch`, `--resample`, `--channels`, `--out`). No account needed. |
| `nexus-workbench channels show REC` | `REC` is the recording's label as members see it (exact, case-sensitive) or its id (24 hex characters; `--id` forces an id). The electrode site each channel records, its label in the app, whether the sites are checked, whether the device fixes them, and the version (`--json`). |
| `nexus-workbench channels set REC` | Set a headband's four port sites: `--port Ec1=M1` (repeat) or `--preset frontal_on_et`, then `--checked` or `--unchecked`; `--expected-version N`. Needs a token with `channels:write`. Prints the old and new sites and the command that undoes it. |
| `nexus-workbench channels clear REC` | Remove the sites set on a recording. |
| `nexus-workbench sample` | Write a synthetic sample night to try the local tools on (`--out`, `--hours`, `--seed`, `--force`). |
| `nexus-workbench qc PATH` | Describe a local EDF file and check its signal quality in 30 s epochs (`--json` for the raw results). The recording must be 30 s or longer. |
| `nexus-workbench doctor` | Check the install, the sign-in and the agent registrations. Being signed out is not a failure. |
| `nexus-workbench mcp` | Run the MCP server on stdio. Your agent starts this; you do not need to. |

Every command has `--help` with an example. Every command that reads or writes data
takes `--json` for the raw result. Tables use aligned columns, with a bold header only on
a terminal and never when `NO_COLOR` is set.

`propose` reads a CSV with a header row `kind,t0,t1` and optionally `channels`, `label`,
`confidence`, `text` (channels as 1-based positions separated by `;`), or a JSON list of
the same objects:

```csv
kind,t0,t1,channels,label,confidence,text
arousal,2100,2104,2,EMG rise,0.6,EMG rise with alpha intrusion for 4 s
bad_signal,3600,3690,,flat,,SD below 0.5 uV for 90 s on all channels
```

A file of more than 500 marks goes in several calls. Each call has a retry key made from
its marks, so running the same command again within 24 hours sends only what did not
arrive.

### Errors and exit codes

Exit codes: 0 ok, 1 failure, 2 usage error. `task give` exits 1 only when no task was
given. A result that is withheld by blinding is not an error: the command says so and
exits 0. The `channels` commands add 3 (Workbench refused the change:
the code and the reason are printed, with what to do) and 4 (sign-in, token scope or
workspace access).

Error messages carry a stable code in brackets, right after the first sentence, for
example:

```text
Not signed in (not_signed_in). Run `nexus-workbench login` in a terminal, then try again.
```

With `--json`, an error is printed to stderr as one JSON object, and stdout stays empty: The `channels`
commands add `fix` to the error object, the request id when the server sent one, and the
refusal's details at the top level (`current` on `changed`, `candidates` on
`ambiguous_label`).

```json
{"error": {"code": "not_signed_in", "message": "Not signed in. Run `nexus-workbench login` in a terminal, then try again."}}
```

The codes are listed in [docs/contract.md](docs/contract.md) (sections 3.1, 3.3 and
7.10). The 1.1 ones: `scope_required` (the token lacks a scope; the message names the
`login` command), `write_access_required` (the workspace does not take agent writes yet),
`already_analysed`, `mark_limit`, `too_large` (a recording's events are too large to read
through the API; open it in Workbench) and `not_available` (`request_analysis`, for now).

## Tools

The server has 22 tools: eight read tools, four detail tools, four write tools and six
local tools. A token lists the read and local tools, and the detail and write tools its
scopes cover.

Agent API version 1.1 (the detail and write tools, and the read tools `list_members`,
`list_tasks`, `list_nights` and `list_layers`) ships with this release. Against an older
server that does not offer them, they answer "This Workbench server does not offer ...
yet". Inputs, outputs and examples for every tool are in
[docs/tools.md](docs/tools.md).

### Read tools (need sign-in)

Every token has the `read` scope. These change nothing.

| Tool | Returns |
|---|---|
| `whoami` | The workspace name and id, your role and member id, and the token's scopes, name and expiry. |
| `list_projects` | Live projects with their recording counts. |
| `list_recordings` | One page of recordings, newest first (50 by default, at most 100), with each one's quality grade and score when graded. Omit `cursor` for the first page; pass `nextCursor` from the previous result for the next. `get_events` and `list_marks` page the same way. |
| `get_recording` | One recording: channel summary, minutes per sleep stage, event and mark counts, when you are allowed to see them. |
| `list_members` | The workspace's people as opaque member ids with their role. No names or emails. |
| `list_tasks` | Review tasks given to you, or also the ones you gave. |
| `list_nights` | Recordings recorded together, with an opaque key for the person who wore the device. |
| `list_layers` | Analysis and agent layers, and per recording whether each has rows. |

### Detail tools (`detail` scope)

These change nothing. What blinding withholds reads `{"hidden": "blind"}`.

| Tool | Returns |
|---|---|
| `get_events` | One page of a recording's machine events: onset, duration, kind, channel, confidence. |
| `get_stages` | The stage of every epoch, from the machine staging or your own scoring. |
| `get_quality` | Signal quality per epoch and per channel. |
| `list_marks` | One page of a recording's marks: human, machine and agent rows, with members' verdicts. |

### Write tools (write scopes)

Listed only when the token has the scope, and they work only where Insai has switched
agent writes on. See [Writing to Workbench](#writing-to-workbench).

| Tool | Scope | Does |
|---|---|---|
| `propose_marks` | `marks:write` | Adds up to 500 marks to this token's own review layer on a recording. |
| `withdraw_marks` | `marks:write` | Withdraws marks this token proposed. |
| `create_review_task` | `tasks:write` | Gives a member review tasks, as you would from the page, and says whether the member was emailed. |
| `request_analysis` | `analysis:run` | Checks a request for an analysis of a recording you uploaded, as Analyse does. Starts nothing yet: it answers that this is not available and to use Analyse on the recording page. |

### Local tools (no account, no network)

These work on files on your computer and send nothing anywhere. They work signed out.
Inputs, outputs, methods and a Python example are in [docs/tools.md](docs/tools.md).

| Tool | Returns |
|---|---|
| `inspect_edf` | Format, duration and channels of an EDF or EDF+ file. |
| `check_signal_quality` | Per channel, how many epochs are ok, suspect, flat or railed, and the longest bad stretches. |
| `compute_band_power` | Delta, theta, alpha, sigma and beta power per EEG channel over clean epochs, and per sleep stage with a hypnogram. |
| `compare_hypnograms` | Agreement, Cohen's kappa, per-stage precision, recall and F1, and the confusion matrix for two hypnograms of one night. |
| `compare_events` | One-to-one matching of candidate events to reference events, with precision, recall, F1 and a shuffled null. |
| `preprocess` | Writes a new EDF: band-pass, 50 or 60 Hz notch, resample and channel choice. Never changes the input. A notch or high edge at or above half a channel's sample rate is skipped on that channel and listed in `notes`. |

Only `preprocess` writes, and only a new file; the other local tools write nothing.

`compare_hypnograms` and `compare_events` can take one side from the workspace: leave out
that side's path and pass `recording_id`. For hypnograms, `source` picks the machine
staging (`machine`) or your own scoring (`mine`); for events, the recording's machine
events stand in, and `label` picks the kind. That needs sign-in with `detail`, and it reads
Workbench. For example, in Claude Code: "Compare ./my_scoring.csv with the machine staging
of recording e_123".

`nexus-workbench sample` writes `sample_night.edf`, two hypnograms and two event files
into `./nexus-sample/` (`--out` picks another folder, `--hours` the length). If sample
files are already in the folder it stops and changes nothing; add `--force` to replace
them.

In Claude Code or Codex, ask for example:

- "Inspect ./nexus-sample/sample_night.edf" (`inspect_edf`)
- "Check the signal quality of ./nexus-sample/sample_night.edf" (`check_signal_quality`)
- "Compute band power per sleep stage for ./nexus-sample/sample_night.edf with the
  hypnogram ./nexus-sample/sample_hypnogram.csv" (`compute_band_power`)
- "Compare ./nexus-sample/sample_hypnogram.csv with
  ./nexus-sample/sample_hypnogram_scorer2.txt" (`compare_hypnograms`)
- "Compare the candidate events ./nexus-sample/sample_events_candidate.json with the
  reference ./nexus-sample/sample_events_reference.csv for an 8 hour recording"
  (`compare_events`)

### Prompts

The server also offers four prompts, which clients show as starters or slash commands.
Each names the tools to use.

| Prompt | Does | Needs |
|---|---|---|
| `qc_edf_file` | Channels and signal quality of an EDF file (`inspect_edf`, `check_signal_quality`). | Nothing |
| `scorer_agreement` | Agreement and kappa of two hypnograms of one night (`compare_hypnograms`). | Nothing |
| `band_power_by_stage` | Band power per sleep stage from an EDF file and its hypnogram (`check_signal_quality`, `compute_band_power`). | Nothing |
| `review_workspace_recordings` | A read-only overview of your workspace's recordings: counts, machine staging, quality grades. | Sign-in |

The first three default to the files `nexus-workbench sample` writes.

### When a tool refuses

A tool that cannot run returns an error with a sentence for you and the same error as
structured data for the agent:

```json
{"ok": false, "code": "not_signed_in", "reason": "Not signed in.",
 "howToFix": ["nexus-workbench login"],
 "localAlternative": ["inspect_edf", "check_signal_quality", "compute_band_power",
                      "compare_hypnograms", "compare_events", "preprocess"],
 "requestId": null, "retryAfter": null}
```

`code` is one of the codes in [Errors and exit codes](#errors-and-exit-codes). `howToFix`
lists the steps; a command in it is for you to run in your own terminal. Never paste a
token into the chat. `localAlternative` names the tools that still work, for example when
you are signed out or agent access is not on yet. `retryAfter` is the wait in seconds
after a rate limit, and `requestId` the server's id for the request when it sends one.

## Writing to Workbench

Writes are version 1.1 of the agent API. They work only in a workspace where Insai staff
have switched on agent writes.

- **Two switches.** A workspace needs agent access for any call and agent writes
  (`agentWrite`) for any write. Insai staff set both, per workspace. With writes off, a
  write answers "This workspace does not take agent writes yet", and reads still work.
- **Scopes.** A write also needs its scope on the token (see [Scopes](#scopes)).
- **Marks are a review layer.** `propose_marks` adds rows to a layer of the token's own,
  named `agent:<token id>`. Members see it on the desk beside the analysis layers,
  labelled with the token's name, and agree or disagree with each row. The rows never
  change anyone's scoring or marks, are never counted as anyone's marks, and never become
  human labels. A member who agrees still writes their own mark if they want one. No
  member can edit the rows; only the token withdraws them. At most 5,000 rows per token
  per recording, withdrawn ones included.
- **Blinding.** A member held blind on a night sees none of the agent's marks, as they see
  none of the analysis layers. Marks cannot be proposed or withdrawn on a recording where
  you are held blind. No write echoes machine output.
- **Tasks and analyses act as you.** `create_review_task` gives a task exactly as you would
  from the page, in your name; the member gets the page's one email, which never names the
  agent, at most one a minute and five an hour. `request_analysis` is checked as the
  uploader's Analyse would be, but for now starts nothing and answers `not_available`:
  press Analyse on the recording page. It can never re-analyse.
- **Audit.** Every write that changes something leaves one row in the workspace's audit
  log, attributed to you "via agent" with the token id, the recording ids and counts. Never
  a mark's text, a label or machine output.
- **Retries are safe.** `propose_marks` sends a retry key, so a call sent again within 24
  hours is answered from the first and writes nothing twice.

## Skills

The skills tell your agent how to use the tools well and what not to conclude
from them. `setup` copies them into your agent's skills folder.

| Skill | Use it to |
|---|---|
| `connect-workbench` | Install, sign in with the right scopes, and handle a workspace tool that refuses. |
| `edf-intake` | Take a first look at a local EDF file before any analysis. |
| `signal-qc` | Check signal quality channel by channel and epoch by epoch. |
| `band-power-by-stage` | Compute band power overall and per sleep stage. |
| `scorer-agreement` | Compare two hypnograms of one night. |
| `event-detector-eval` | Compare detected events with reference events against a shuffled null. |
| `prereg` | Write down an analysis plan before running it. |
| `verify-before-verdict` | Check a number or a negative result before reporting it. |
| `leakage-check` | Audit an evaluation for leakage and confounds. |
| `stats-rails` | Pick the right statistic across nights and people. |
| `review-loop` | Analyse locally, propose marks with reasons, and hand the recording to a reviewer. |
| `nights-by-person` | Group nights by person and summarise with the person as the unit. |
| `preprocess-and-qc` | Filter, notch or resample a local EDF into a new file, with quality before and after. |

## Privacy

- Read-only unless you ask for more. Version 1 of the agent API is read-only. Version 1.1
  adds detail reads and reviewed writes: a token has the `read` scope, and a write needs a
  write scope you chose at sign-in and the workspace's agent writes switch. Writes land as
  proposals that members review (see [Writing to Workbench](#writing-to-workbench)).
- `list_members` and `list_tasks` name people only by opaque member ids, and
  `list_nights` names the person who wore the device only by an opaque key that differs
  per workspace. No names, initials or emails.
- The workspace tools never return recording names, notes, participant codes,
  file names, recording start dates or times, or who added or scored a
  recording. Channel labels that are not standard electrode names come back as
  `ch1`, `ch2` and so on. A recording you are blinded on comes back without
  stage, event or mark counts.
- A token sees what its owner sees in Workbench and never more.
- The local tools never parse, keep or return an EDF file's identity fields
  (patient name, id, birth date, the recording's free text, the start date and
  time), and never return raw signal samples. Files are
  named by an id made from their content, not by their path. `preprocess` writes a new
  file with placeholder identity fields and no annotations.
- Sign-in sends this computer's name (cleaned and cut to 64 characters) and the
  package version, so you can tell your tokens apart in Workbench.
- Tokens are stored in `credentials.json`, a file only you can read
  (permissions `600`), one per Workbench server, in your user config folder:
  `~/Library/Application Support/nexus-workbench/` on macOS,
  `$XDG_CONFIG_HOME/nexus-workbench/` (by default `~/.config/nexus-workbench/`)
  on Linux, and `%LOCALAPPDATA%\nexus-workbench\` on Windows. Set
  `NEXUS_WORKBENCH_CONFIG_DIR` to use another folder. `nexus-workbench login
  --keyring` (or `NEXUS_KEYRING=1`) stores the token in the system keyring
  instead; install `nexus-workbench[keyring]` for that. The `NEXUS_TOKEN`
  environment variable, if set, is used before the saved token.
- The token is only sent to the Workbench API over HTTPS and is never printed
  in full. `doctor` shows only its last four characters.
- Tokens expire (90 days by default). To revoke one, open Workbench, go to the
  account menu, then "Connect an agent". `logout` only removes the copy on this
  computer.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Settings

| Variable | Use |
|---|---|
| `NEXUS_TOKEN` | A token to use instead of the saved one. |
| `NEXUS_API_URL` | A different Workbench API address. Must start with `https://`. |
| `NEXUS_KEYRING` | `1` to store new tokens in the system keyring. |
| `NEXUS_WORKBENCH_CONFIG_DIR` | The folder for the credentials file, instead of the user config folder (see [Privacy](#privacy)). `setup` passes it on to the MCP server. |
| `NEXUS_DEBUG` | `1` to print the traceback when `qc` or `preprocess` hits an unexpected error. |
| `NEXUS_WORKBENCH_NO_UPDATE_CHECK` | `1` to turn off the check for a newer version (see [Update](#update)). |
| `NO_COLOR` | Set to anything to turn off the bold table headers. |

## Update

```sh
uv tool upgrade nexus-workbench
nexus-workbench setup
```

The second line refreshes the skill copies in your agent's skills folder. It changes
nothing else that is already in place. Check [CHANGELOG.md](CHANGELOG.md) for what
changed.

Plugin users update the plugin in their agent instead, for example
`claude plugin update nexus-workbench@insai`.

Once a day at most, the MCP server and `doctor` ask PyPI for the newest version of
`nexus-workbench`, with a two-second timeout, and keep the answer in your user cache
folder. When a newer one exists, one tool result per session and `doctor` say so. The
request carries nothing about you or your files, and when it fails (offline, for
example) nothing is shown. Set `NEXUS_WORKBENCH_NO_UPDATE_CHECK=1` to turn it off.

## Troubleshooting

Run `nexus-workbench doctor` first. It shows the version, the sign-in and whether each
agent has the `nexus` server registered.

**The `nexus` tools do not show up in the agent.** Restart Claude Code or Codex: a new
server is picked up by a new session. If they still do not show up, run
`nexus-workbench doctor`. If it says the server is not registered, run
`nexus-workbench setup` again.

**A detail or write tool does not show up.** The saved token lacks its scope. Run
`nexus-workbench whoami` to see the scopes, sign in again with the scopes you need (for
example `nexus-workbench login --scope detail`), then restart Claude Code or Codex.

**"This token cannot ..." (`scope_required`).** Same as above: the message names the
`login` command to run.

**"This workspace does not take agent writes yet."** Writes are switched on per workspace
by Insai. Reads keep working.

**"Agent access is not on for this workspace yet."** Your token works, but your workspace
does not have agent access yet. Nothing on your computer can change that. Request it in
Workbench: open the account menu, choose Connect an agent, then Request agent access.
Insai emails you when it is switched on. The local tools work in the meantime.

**`uvx: command not found`.** Install uv. On macOS and Linux:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

On Windows, and for other ways to install it, see the
[uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
Open a new terminal afterwards.

**`nexus-workbench: command not found` after `setup`.** The folder where uv puts
commands is not on your PATH yet. Run `uv tool update-shell`, then open a new terminal.
Until then, `uvx nexus-workbench <command>` works too.

**"Your Workbench token has expired."** Run `nexus-workbench login` to sign in again.
Tokens last 90 days by default.

**"Not signed in" although you signed in.** If you set `NEXUS_WORKBENCH_CONFIG_DIR` or
`NEXUS_API_URL` after running `setup`, the agent may look in another place. Run
`nexus-workbench setup` again so the server gets the same settings.

**An EDF+D file is refused.** `check_signal_quality`, `compute_band_power` and `qc`
refuse discontinuous EDF+D recordings: their data records have gaps, and the epoch
maths assumes there are none. `inspect_edf` still describes the file. Export the
recording as continuous EDF or EDF+C. 24-bit BDF files are not supported.

**"Something went wrong while reading the file."** Check that the file is a 16-bit EDF
or EDF+ file. To see the details, rerun with `NEXUS_DEBUG=1`, for example
`NEXUS_DEBUG=1 nexus-workbench qc night.edf`. Include that output, with any file paths
removed, if you open an issue.

**Windows.** The commands are the same in PowerShell. The credentials file is in
`%LOCALAPPDATA%\nexus-workbench\`, inside your own user folder; Windows does not use
the `600` file permission.

## Uninstall

Remove the parts that `setup` added, in this order:

```sh
claude mcp remove --scope user nexus
codex mcp remove nexus
nexus-workbench logout
uv tool uninstall nexus-workbench
```

Then remove the skill folders `setup` copied. They are in `~/.claude/skills/` and
`~/.codex/skills/` (or `$CLAUDE_CONFIG_DIR/skills/` and `$CODEX_HOME/skills/`), and
each one holds a `.nexus-workbench` marker file. This removes only folders with the
marker:

```sh
find ~/.claude/skills ~/.codex/skills -mindepth 2 -maxdepth 2 -name .nexus-workbench 2>/dev/null |
  while read -r marker; do rm -rf "$(dirname "$marker")"; done
```

`logout` removes the token from this computer only. To revoke it, use the
"Connect an agent" page in Workbench.

If you installed the plugin instead, run `claude plugin uninstall nexus-workbench@insai`
or `codex plugin remove nexus-workbench@insai`.

## Contributing and security

- [CONTRIBUTING.md](CONTRIBUTING.md): set up a development copy, run the tests, and the
  rules for changes.
- [SECURITY.md](SECURITY.md): report a vulnerability privately.
- [CHANGELOG.md](CHANGELOG.md): what changed in each release.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
