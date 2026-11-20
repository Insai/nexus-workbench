# nexus-workbench

Connects Claude Code and Codex to Insai Workbench, where sleep researchers
review EEG recordings. It gives your coding agent a local MCP server named
`nexus` with nine read-only tools, ten skills, and a command line to sign in.
Four tools read your Workbench workspace; five read EEG files on your own
computer and need no account.

**Early access.** Agent access is switched on per workspace. Ask for it at
<https://insai.tech/workbench>.

For research use only. Not for clinical decision-making.

## Install

You need [uv](https://docs.astral.sh/uv/). Then run:

```sh
uvx nexus-workbench setup
```

`setup` does four things and tells you what it did:

1. Installs `nexus-workbench` with `uv tool install`, so it keeps working in new
   terminals.
2. Registers the MCP server with Claude Code (`claude mcp add --scope user`)
   and Codex (`codex mcp add`), using the full path to the installed command.
   An agent that is not installed is skipped.
3. Copies the packaged skills into `~/.claude/skills/` and `~/.codex/skills/`
   (or `$CLAUDE_CONFIG_DIR/skills/` and `$CODEX_HOME/skills/`). A skill folder
   that is already there and was not installed by this package is left alone.
4. Signs you in to Workbench with a token, unless you are signed in already.

Options: `--agent claude|codex|all`, `--no-login`, `--dry-run` (print every
command and file write, change nothing), `--yes` (do not ask first). Running
`setup` again changes nothing that is already in place.

Then open Claude Code or Codex and ask: "list my Workbench recordings".

## Commands

| Command | What it does |
|---|---|
| `nexus-workbench setup` | Install, register the MCP server, install skills, sign in. |
| `nexus-workbench login` | Sign in with a token (see [Sign in](#sign-in)). |
| `nexus-workbench login --token` | Paste a token made in Workbench. Piped input works too: `nexus-workbench login --token < token.txt`. A token is never accepted as a command-line argument. |
| `nexus-workbench logout` | Remove the saved token from this computer. |
| `nexus-workbench whoami` | Show the workspace your token reads. |
| `nexus-workbench recordings` | List recordings (`--project`, `--limit`, `--cursor`, `--json`). |
| `nexus-workbench sample` | Write a synthetic sample night to try the local tools on (`--out`, `--hours`, `--seed`, `--force`). |
| `nexus-workbench qc PATH` | Describe a local EDF file and check its signal quality in 30 s epochs (`--json` for the raw results). The recording must be 30 s or longer. |
| `nexus-workbench doctor` | Check the install, the sign-in and the agent registrations. |
| `nexus-workbench mcp` | Run the MCP server on stdio. Your agent starts this; you do not need to. |

`nexus` is a shorter name for the same command.

## Sign in

Today you sign in with a token:

1. In Workbench, open the account menu and choose "Connect an agent".
2. Make a token there and copy it.
3. Run `nexus-workbench login --token` and paste it. The token is checked with
   Workbench, then saved on this computer.

Instead of saving it, you can set the `NEXUS_TOKEN` environment variable to the
token. Signing in through the browser comes later.

## Signed out and signed in

Signed out, the local file tools work and need no account. The workspace tools
answer "Not signed in" and tell the agent to ask you to run
`nexus-workbench login`.

Signed in, the workspace tools read your Workbench workspace.

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
out. Inputs, outputs and methods are in [docs/tools.md](docs/tools.md).

| Tool | Returns |
|---|---|
| `inspect_edf` | Format, duration and channels of an EDF or EDF+ file. |
| `check_signal_quality` | Per channel, how many epochs are ok, suspect, flat or railed, and the longest bad stretches. |
| `compute_band_power` | Delta, theta, alpha, sigma and beta power per EEG channel over clean epochs, and per sleep stage with a hypnogram. |
| `compare_hypnograms` | Agreement, Cohen's kappa, per-stage precision, recall and F1, and the confusion matrix for two hypnograms of one night. |
| `compare_events` | One-to-one matching of candidate events to reference events, with precision, recall, F1 and a shuffled null. |

To try them, write the synthetic sample night. Nothing in it comes from a
recording:

```sh
nexus-workbench sample --out ./nexus-sample --hours 8
```

This writes `sample_night.edf`, two hypnograms and two event files. If sample
files are already in the folder it stops and changes nothing; add `--force` to
replace them. Check the EDF from the terminal:

```sh
nexus-workbench qc ./nexus-sample/sample_night.edf
```

It prints the file's format and length, and one row per channel with the share
of ok, suspect, flat and railed epochs. Add `--json` for the full results.

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
- Tokens are stored in a file only you can read (permissions `600`) in your
  user config folder, one per Workbench server. `nexus-workbench login
  --keyring` (or `NEXUS_KEYRING=1`) stores the token in the system keyring
  instead; install `nexus-workbench[keyring]` for that. The `NEXUS_TOKEN`
  environment variable, if set, is used before the saved token.
- The token is only sent to the Workbench API over HTTPS and is never printed
  in full. `doctor` shows only its last four characters.
- Tokens expire (90 days by default). To revoke one, open Workbench, go to the
  account menu, then "Connect an agent". `logout` only removes the copy on this
  computer.

## Settings

| Variable | Use |
|---|---|
| `NEXUS_TOKEN` | A token to use instead of the saved one. |
| `NEXUS_API_URL` | A different Workbench API address. Must start with `https://`. |
| `NEXUS_KEYRING` | `1` to store new tokens in the system keyring. |

## Licence

Apache-2.0. See [LICENSE](LICENSE).
