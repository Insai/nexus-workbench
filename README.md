# nexus-workbench

One command adds Insai Workbench to Claude Code or Codex: `uvx nexus-workbench setup`.
It gives your agent an MCP server named `nexus` with nine read-only tools and ten
skills for sleep-EEG research. Five tools work on EDF files on your own computer, with
no account. The other four read your Workbench workspace, where sleep researchers
review EEG recordings.

Local tools send nothing anywhere. Workspace tools send only your token and the request
to the Workbench API over HTTPS. There is no telemetry.

**Early access.** Agent access to a workspace is switched on per workspace. Ask for it
at <https://insai.tech/workbench>. The local tools work without it.

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

## Try it in 30 seconds (no account)

```sh
uvx nexus-workbench setup --no-login
nexus-workbench sample
nexus-workbench qc nexus-sample/sample_night.edf
```

`sample` writes a synthetic night into `./nexus-sample/`. Nothing in it comes from a
recording. `qc` prints the file's format and length, and one row per channel with the
share of ok, suspect, flat and railed epochs.

Then in Claude Code or Codex, ask:

> Check the signal quality of ./nexus-sample/sample_night.edf

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

## Commands

| Command | What it does |
|---|---|
| `nexus-workbench setup` | Install, register the MCP server, install skills, sign in. |
| `nexus-workbench login` | Sign in: approve this computer in the browser and type the code shown. `--token` pastes a token instead. |
| `nexus-workbench login --token` | Paste a token made in Workbench. Piped input works too: `nexus-workbench login --token < token.txt`. A token is never accepted as a command-line argument. |
| `nexus-workbench logout` | Remove the saved token from this computer. |
| `nexus-workbench whoami` | Show the workspace your token reads. |
| `nexus-workbench recordings` | List recordings (`--project`, `--limit`, `--cursor`, `--json`). |
| `nexus-workbench channels show ID` | The electrode site each channel records, its label in the app, whether the sites are checked, whether the device fixes them, and the version (`--json`). |
| `nexus-workbench channels set ID` | Set a headband's four port sites: `--port Ec1=M1` (repeat) or `--preset frontal_on_et`, then `--checked` or `--unchecked`; `--expected-version N`. Needs a token with `channels:write`. Prints the old and new sites and the command that undoes it. |
| `nexus-workbench channels clear ID` | Remove the sites set on a recording. |
| `nexus-workbench sample` | Write a synthetic sample night to try the local tools on (`--out`, `--hours`, `--seed`, `--force`). |
| `nexus-workbench qc PATH` | Describe a local EDF file and check its signal quality in 30 s epochs (`--json` for the raw results). The recording must be 30 s or longer. |
| `nexus-workbench doctor` | Check the install, the sign-in and the agent registrations. Being signed out is not a failure. |
| `nexus-workbench mcp` | Run the MCP server on stdio. Your agent starts this; you do not need to. |

Every command has `--help`.

### Errors and exit codes

Exit codes: 0 ok, 1 failure, 2 usage error. The `channels` commands add 3 (Workbench
refused the change: the code and the reason are printed, with what to do) and 4 (sign-in,
token scope or workspace access).

Error messages carry a stable code in brackets, right after the first sentence, for
example:

```text
Not signed in (not_signed_in). Run `nexus-workbench login` in a terminal, then try again.
```

With `--json` (on `whoami`, `recordings`, `qc` and `channels`), an error is printed to stderr as one
JSON object, and stdout stays empty:

```json
{"error": {"code": "not_signed_in", "message": "Not signed in. Run `nexus-workbench login` in a terminal, then try again."}}
```

The codes are listed in [docs/contract.md](docs/contract.md) (sections 3.1 and 3.3).

## Tools

Every tool is read-only. None of them changes anything in Workbench or on your
computer.

### Workspace tools (need sign-in)

| Tool | Returns |
|---|---|
| `whoami` | The workspace name and id, your role, and the token's name and expiry. |
| `list_projects` | Live projects with their recording counts. |
| `list_recordings` | One page of recordings, newest first (50 by default, at most 100; page with `cursor`). |
| `get_recording` | One recording: channel summary, minutes per sleep stage, event and mark counts, when you are allowed to see them. |

### Local tools (no account, no network)

These read a file on your computer and send nothing anywhere. They work signed
out. Inputs, outputs, methods and a Python example are in [docs/tools.md](docs/tools.md).

| Tool | Returns |
|---|---|
| `inspect_edf` | Format, duration and channels of an EDF or EDF+ file. |
| `check_signal_quality` | Per channel, how many epochs are ok, suspect, flat or railed, and the longest bad stretches. |
| `compute_band_power` | Delta, theta, alpha, sigma and beta power per EEG channel over clean epochs, and per sleep stage with a hypnogram. |
| `compare_hypnograms` | Agreement, Cohen's kappa, per-stage precision, recall and F1, and the confusion matrix for two hypnograms of one night. |
| `compare_events` | One-to-one matching of candidate events to reference events, with precision, recall, F1 and a shuffled null. |

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

## Skills

The skills tell your agent how to use the tools well and what not to conclude
from them. `setup` copies them into your agent's skills folder.

| Skill | Use it to |
|---|---|
| `connect-workbench` | Install, sign in, and handle a workspace tool that refuses. |
| `edf-intake` | Take a first look at a local EDF file before any analysis. |
| `signal-qc` | Check signal quality channel by channel and epoch by epoch. |
| `band-power-by-stage` | Compute band power overall and per sleep stage. |
| `scorer-agreement` | Compare two hypnograms of one night. |
| `event-detector-eval` | Compare detected events with reference events against a shuffled null. |
| `prereg` | Write down an analysis plan before running it. |
| `verify-before-verdict` | Check a number or a negative result before reporting it. |
| `leakage-check` | Audit an evaluation for leakage and confounds. |
| `stats-rails` | Pick the right statistic across nights and people. |

## Privacy

- Read-only. A token has the `read` scope and no tool writes anything.
- The workspace tools never return recording names, notes, participant codes,
  file names, recording start dates or times, or who added or scored a
  recording. Channel labels that are not standard electrode names come back as
  `ch1`, `ch2` and so on. A recording you are blinded on comes back without
  stage, event or mark counts.
- A token sees what its owner sees in Workbench and never more.
- The local tools never parse, keep or return an EDF file's identity fields
  (patient name, id, birth date, the recording's free text, the start date and
  time), and never return raw signal samples. Files are
  named by an id made from their content, not by their path.
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
| `NEXUS_DEBUG` | `1` to print the traceback when `qc` hits an unexpected error. |

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

## Troubleshooting

Run `nexus-workbench doctor` first. It shows the version, the sign-in and whether each
agent has the `nexus` server registered.

**The `nexus` tools do not show up in the agent.** Restart Claude Code or Codex: a new
server is picked up by a new session. If they still do not show up, run
`nexus-workbench doctor`. If it says the server is not registered, run
`nexus-workbench setup` again.

**"Agent access is in early access and is not switched on for this workspace yet."**
Your token works, but your workspace does not have agent access yet. Nothing on your
computer can change that. Ask for access at <https://insai.tech/workbench>. The local
tools work in the meantime.

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
