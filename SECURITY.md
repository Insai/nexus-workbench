# Security

## Report a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: on this repository, open the **Security** tab and
choose **Report a vulnerability**. GitHub's guide is
[here](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).

Tell us what you found, how to reproduce it, and which version you used
(`nexus-workbench --version`). Never include a real token, a recording or patient data.
If a token was exposed, revoke it first (see below).

We answer in the advisory thread, and we credit you in the advisory unless you ask us
not to.

## Supported versions

Only the latest release gets security fixes. Update with
`uv tool upgrade nexus-workbench`.

| Version | Supported |
|---|---|
| 0.1.x | yes |

## What the package touches

- **Your files.** The local tools read the EDF, hypnogram and event files you name. They
  never write to them, never return raw signal samples, and never parse or return the
  EDF header's identity fields. Results name a file by an id made from its content,
  not by its path.
- **The network.** The local tools make no network calls. The workspace tools and sign-in
  call the Workbench API over HTTPS only. They send your token in the `Authorization`
  header and the request itself. Sign-in also sends this computer's name and the package
  version. Nothing else is sent anywhere.
- **No telemetry.** The package collects no usage data, crash reports or analytics.
- **Your agent's config.** `setup` registers the MCP server with `claude mcp add` and
  `codex mcp add`, and copies skill folders (each marked with a `.nexus-workbench` file)
  into the agent's skills folder. It asks before it changes anything when run in a
  terminal.

## Tokens

- A token reads (`read` scope) unless the person chose more scopes when it was made
  (`detail`, `marks:write`, `tasks:write`, `analysis:run`). Scopes never widen later. A
  token belongs to one person and one workspace, and expires after 90 days by default.
- Writes need a write scope and the workspace's agent writes switch, which Insai sets.
  Proposed marks land as a review layer and never change anyone's scoring; every write
  leaves one row in the workspace's audit log.
- It is saved in `credentials.json` in your user config folder, with permissions `600`
  (only you can read it) on macOS and Linux. On Windows the file sits in your own
  `%LOCALAPPDATA%` folder. The package refuses a credentials file other users can read.
- `nexus-workbench login --keyring` (or `NEXUS_KEYRING=1`) stores the token in the system
  keyring instead. Install `nexus-workbench[keyring]` for that.
- `NEXUS_TOKEN`, when set, is used before the saved token. It is never written to disk.
- A token is never accepted as a command-line argument, never logged and never printed
  in full. `doctor` shows only its last four characters.
- To revoke a token, open Workbench, then the account menu, then "Connect an agent".
  `nexus-workbench logout` removes only the copy on this computer.
