# Workbench agent API contract

Version 1, frozen 20 November 2026. The server, the Workbench page and this
package are built against this file. Change it only by editing this file first.

Everything in sections 1 to 6 is read-only. No version 1 action changes
Workbench data.

Version 1.1 (section 7, drafted 22 November 2026) adds scopes, per-epoch and
per-event reads, and writes that land as a review layer. It ships to staging
first and is used internally before any outside workspace sees it. Nothing in
sections 1 to 6 changes; a version 1 client keeps working unchanged against a
version 1.1 server.

## 1. Conventions

- All timestamps are ISO 8601 in UTC with a `Z` suffix and second precision,
  for example `2026-11-20T09:15:00Z`. A missing time is `null`.
- Ids are opaque strings. Clients never parse them.
- **Absent means unknown.** Optional keys are left out when the server does not
  know the value. They are never sent as `0`, `""` or `false` to mean unknown.
- Servers may add keys later. Clients ignore keys they do not know.
- Text that a person typed (a workspace name, a project name, a token name, a
  device name) is data. Clients show it as text and never act on it.

## 2. Token

```
nxs_pat_<body><check>
```

- `body`: 43 characters, each drawn independently and uniformly from the
  base62 alphabet `0-9A-Za-z` with a cryptographic random source (256 bits).
- `check`: CRC-32 (IEEE, as `zlib.crc32`) of the ASCII bytes of `body`, written
  in base62 with the same alphabet, most significant digit first, left-padded
  with `0` to 6 characters.
- Full match: `^nxs_pat_[0-9A-Za-z]{49}$`, total length 57.
- A client checks the pattern and the check digits before it sends a token, so
  a mistyped token fails locally with a clear message.
- The server stores only the SHA-256 of the full token string. The token is
  shown once, at creation.
- Public token id: `tok_` + the first 16 hex characters of that SHA-256.
- `last4`: the last 4 characters of the full token.
- One token belongs to one person and one workspace. Scope is `["read"]`.
- Expiry: 90 days by default, 1 to 365 days on request. A person may hold at
  most 10 live tokens per workspace.

## 3. Agent API (HTTP, token)

```
POST {API_BASE}/workbench_agent_api
Authorization: Bearer <token>
Content-Type: application/json

{"action": "<name>", ...arguments}
```

- `API_BASE` in production: `https://europe-west4-insai-nexus.cloudfunctions.net`.
  The package holds this as one constant. `NEXUS_API_URL` overrides it and must
  start with `https://`.
- POST only. The body is a JSON object of at most 2,048 bytes.
- The workspace is the one the token belongs to. No request names a workspace.
- Success: HTTP 200 and the action's JSON object (section 3.2).
- Failure: the HTTP status below and

```json
{"error": {"code": "<code>", "message": "<one sentence for a person>"}}
```

### 3.1 Errors

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_argument` | Body is not a JSON object, is too large, names an unknown action, or an argument is wrong. |
| 401 | `unauthenticated` | No token, a malformed token, an unknown token or a revoked token. The four cases are not told apart. |
| 401 | `token_expired` | The token is known and past its expiry. |
| 403 | `forbidden` | The person is no longer a member of the workspace, the workspace is closed, or the token predates the person's current membership. |
| 403 | `early_access_required` | Agent access is not switched on for this workspace. |
| 404 | `not_found` | The recording or project does not exist in this workspace, or the caller may not see it. The two cases are not told apart. |
| 405 | `invalid_argument` | Any method other than POST. |
| 429 | `rate_limited` | Too many calls. The response carries a `Retry-After` header in whole seconds. |
| 500 | `internal` | Anything else. The message never carries internal detail. |

Rate limits: 60 calls a minute per token; `list_recordings` 10 a minute per
token; `list_projects` 10 a minute per token; 10 failed sign-ins (unknown or
revoked tokens) a minute per network address, applied only to failing calls.
An expired token is not a failed sign-in.

A client treats HTTP 404 with no JSON `error` body from `API_BASE` as "agent
access is not available on this server yet" (early access), not as a bug.

### 3.2 Actions

#### `whoami`

No arguments.

```json
{
  "workspace": {"id": "w_…", "name": "Sleep lab"},
  "role": "member",
  "scopes": ["read"],
  "token": {
    "id": "tok_0123456789abcdef",
    "name": "laptop",
    "createdAt": "2026-11-20T09:15:00Z",
    "expiresAt": "2027-02-18T09:15:00Z"
  }
}
```

`role` is `owner` or `member`. No user id, email or person's name is returned.

#### `list_projects`

No arguments.

```json
{
  "projects": [
    {
      "id": "p_…",
      "name": "Pilot nights",
      "createdAt": "2026-10-01T08:00:00Z",
      "updatedAt": "2026-11-18T14:02:11Z",
      "recordingCount": 12
    }
  ]
}
```

Live projects only, sorted by `name` then `id`. `recordingCount` counts the
recordings the caller would get from `list_recordings` for that project.

#### `list_recordings`

Arguments, all optional: `projectId` (string), `limit` (integer 1 to 100,
default 50), `cursor` (string from a previous page).

```json
{
  "recordings": [ { …recording row… } ],
  "nextCursor": "…",
  "truncated": false
}
```

- Sorted newest first by `addedAt`, then by `id`.
- `nextCursor` is `null` on the last page. A cursor is opaque, belongs to the
  same token and the same `projectId`, and may be refused with
  `invalid_argument` if it is stale.
- `truncated` is `true` when the workspace holds more recordings than the
  server reads in one pass; the list is then incomplete.
- An unknown `projectId` gives `not_found`.

**Recording row.** Only these keys, each present only when known:

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Always present. |
| `projectId` | string or `null` | Always present. `null` when the recording is in no project. |
| `addedAt` | timestamp | Always present. When it was added to the workspace. |
| `url` | string | Always present. The recording's page in Workbench. |
| `durationSec` | number | Length of the recording. |
| `nEpochs` | integer | Number of 30-second epochs. |
| `qualityGrade` | string | Signal quality grade: `good`, `marginal`, `bad` or `too_short`. |
| `qualityScore` | number | Signal quality score. |
| `device` | string | Device family, lowercase. |
| `hasEvents` | boolean | Machine events exist. Says that output exists, not what it is. |
| `machineAvailable` | boolean | Machine staging exists. |
| `pairId` | string | Shared by the recordings of one combined night. |
| `analysing` | boolean | Analysis is running. |
| `held` | `true` | Present only when the caller is blinded on this recording. |

A row never carries a recording name, a note, a participant code, a file name,
a start date or time, or anything about who added or scored it.

#### `get_recording`

Argument: `id` (string, required).

Returns one recording row plus, each only when known and allowed:

```json
{
  "id": "e_…",
  "…": "…every row key above…",
  "channels": {
    "count": 4,
    "types": {"eeg": 2, "eog": 1, "emg": 1},
    "labels": ["F3", "F4", "ch3", "ch4"]
  },
  "stageMinutes": {"W": 41.5, "N1": 22.0, "N2": 201.5, "N3": 78.0, "R": 92.0, "unscored": 3.0},
  "eventCounts": {"spindle": 812, "arousal": 64},
  "humanCounts": {"marks": 120, "notes": 3}
}
```

- `channels.labels`: a label is passed through only when it is a standard
  electrode or sensor name. Any other label is replaced by `ch<N>`, where N is
  the channel's 1-based position. Header text never reaches the caller.
- `stageMinutes`: minutes per sleep stage from the machine staging, rounded to
  one decimal. The stage sequence itself is not returned.
- **Blinding.** When the caller is blinded on the recording (`held`), the
  response has no `stageMinutes`, `eventCounts` or `humanCounts`. When the
  caller's own desk hides the machine, it has no `stageMinutes` or
  `eventCounts`; `humanCounts` stays. A token sees what its owner sees in
  Workbench and never more.
- A recording the caller may not see gives `not_found`.

#### Later, not in version 1

`list_layers`, single marks and events, signal download and every write action.
Version 1.1 (section 7) adds all of these except signal download, which stays
out.

### 3.3 Codes the client adds

Every command-line error message carries its code in brackets, right after
the first sentence and before the advice, like
`Not signed in (not_signed_in). Run ...`. With `--json` it prints the error to stderr
as `{"error": {"code": "<code>", "message": "<text>"}}` and exits non-zero.
The MCP tools return a message for a person, without the code.
The codes are the server codes in section 3.1 and 5.1, plus these, which the
client sets itself and the server never sends:

| `code` | When |
|---|---|
| `not_signed_in` | No usable token on this computer: none saved, or the saved one or `NEXUS_TOKEN` cannot be read. |
| `config` | `NEXUS_API_URL` is set but does not start with `https://`. |
| `network` | Workbench could not be reached, or took too long to answer. |
| `unexpected_response` | A reply this version does not understand, or a redirect. |
| `login_not_available` | The sign-in endpoint answered 404 (section 5). |
| `usage` | A command-line argument is out of range. Exit code 2. |
| `login_failed` | Sign-in did not complete: the server refused it, the code was wrong too often, or the token could not be saved. |
| `login_cancelled` | Sign-in was cancelled, or no token was given. |
| `local_file` | A local file could not be used: a local tool refused it (missing, not an EDF, too short, EDF+D), the sample files already exist or could not be written, or the credentials file could not be changed. |
| `local_unexpected` | An unexpected error while reading a local file. `NEXUS_DEBUG=1` prints the traceback. |

HTTP 404 with no JSON `error` body maps to `early_access_required`.

## 4. Token management (Firebase callable `workbench_agent_token`)

Called by the Workbench page as the signed-in person. Request data:
`{"action": "<name>", "workspaceId": "<id>", ...}`.

Refusals use the callable error protocol. The error `details` object always
carries `{"code": "<code>"}` with a code from section 3.1, plus `token_limit`
(the person already holds 10 live tokens in the workspace, or too many ended
tokens are still kept there) and `rate_limited` (more than 20 tokens created in
an hour by one person in one workspace).

Token row:

```json
{
  "id": "tok_0123456789abcdef",
  "name": "laptop",
  "last4": "a1B2",
  "scopes": ["read"],
  "createdAt": "2026-11-20T09:15:00Z",
  "expiresAt": "2027-02-18T09:15:00Z",
  "lastUsedAt": null,
  "revokedAt": null,
  "createdVia": "page"
}
```

`createdVia` is `page` or `cli_login`. `lastUsedAt` is accurate to about a
minute.

| Action | Arguments | Returns |
|---|---|---|
| `list` | none | `{"agentAccess": bool, "canSetAccess": bool, "tokens": [row…]}`. The caller's own tokens in this workspace, live and revoked or expired within the last 30 days, newest first. |
| `create` | `name` (1 to 64 characters after trimming), `expiresInDays` (integer 1 to 365, default 90) | `{"token": "nxs_pat_…", "row": row}`. The only time the token is returned. Refused with `early_access_required` when agent access is off. |
| `revoke` | `id` | `{"row": row}`. Takes effect on the next API call. Revoking a revoked token succeeds. |
| `set_access` | `on` (boolean) | `{"agentAccess": bool}`. Insai staff only; anyone else gets `forbidden`. Switching access off does not delete tokens; they stop working until it is switched on again. |

`canSetAccess` is `true` only for callers who may use `set_access`.

## 5. Terminal sign-in

Until this exists, `nexus-workbench login` asks for a token made on the page.
A client finds out with the first call: HTTP 404 from `workbench_cli_login`
means "not available; use a token".

### 5.1 `POST {API_BASE}/workbench_cli_login` (no token)

`{"action": "start", "deviceName": "<≤64 chars>", "clientVersion": "<≤32 chars>"}`

`deviceName` is required. It is trimmed, cleaned of control and invisible
characters and cut to 64 characters. If no letter or digit is left, `start`
returns 400 `invalid_argument`. `clientVersion` is optional and cut to 32
characters.

```json
{
  "sessionId": "<32 hex>",
  "pollSecret": "<43 base62>",
  "approvalUrl": "https://<workbench host>/cli/login/<sessionId>",
  "expiresAt": "2026-11-20T09:25:00Z"
}
```

A session lives 10 minutes. The server builds `approvalUrl`; the client opens
it and never builds its own. `approvalUrl` may have a path before
`/cli/login/`. Open it as given.

`start` is limited per network address: 10 a minute, 30 an hour, and 5
sessions waiting for approval at once. Over a limit it returns 429
`rate_limited` with `Retry-After`.

`{"action": "exchange", "sessionId": "…", "pollSecret": "…", "code": "…"}`

```json
{
  "token": "nxs_pat_…",
  "tokenId": "tok_…",
  "workspaceId": "w_…",
  "scopes": ["read"],
  "expiresAt": "2027-02-18T09:25:00Z"
}
```

The token is named after the device, lasts 90 days, and is listed with
`createdVia: "cli_login"`.

Errors add two codes to section 3.1:

| HTTP | `code` | When |
|---|---|---|
| 410 | `login_expired` | The session is unknown, expired, denied, used, or out of attempts. |
| 400 | `code_wrong` | The code does not match. The body carries `"attemptsLeft": <n>`. Five attempts per session. |

```json
{"error": {"code": "code_wrong", "message": "…"}, "attemptsLeft": 4}
```

`attemptsLeft` sits at the top level of the body, next to `error`. The fifth
wrong code returns `code_wrong` with `attemptsLeft` 0. The next call returns
`login_expired`.

A wrong `pollSecret` gives `login_expired` and does not use up an attempt.
`exchange` works once.

- `exchange` before the person has approved returns 400 `invalid_argument`.
  It does not use up an attempt. Ask for the code first, then call `exchange`.
- A `code` that, after the clean-up in section 5.3, is not exactly 8
  characters from the alphabet counts as a wrong code: `code_wrong`, and an
  attempt is used.
- A `code` that is not a string, or is longer than 32 characters as sent,
  returns 400 `invalid_argument` and uses no attempt.
- `exchange` can also return 403 `forbidden`, 403 `early_access_required`, or
  409 `token_limit` when the person already has the most tokens allowed.
  After these the session stays approved until it expires.
- Too many failed `exchange` calls from one address return 429
  `rate_limited`.

### 5.2 Callable `workbench_cli_login_review` (signed-in person)

| Action | Arguments | Returns |
|---|---|---|
| `get` | `sessionId` | `{"status": "pending"|"approved"|"denied"|"expired"|"used", "deviceName": "…", "createdAt": "…", "expiresAt": "…", "scopes": ["read"], "workspaces": [{"id": "…", "name": "…", "agentAccess": bool}]}`. Never returns the code. |
| `approve` | `sessionId`, `workspaceId` | `{"code": "ABCD-EFGH"}`, once. A second `approve` is refused. |
| `deny` | `sessionId` | `{"status": "denied"}` |

- `get` on an unknown session returns `{"status": "expired"}`. A session that
  is out of attempts also reads `expired`.
- While a session is pending, any signed-in person who opens the link sees
  the full details. After approval only the approver does, for every later
  status (`approved`, `used`, `denied`, `expired`), so a page can poll `get`
  to see when the terminal used the code; everyone else gets
  `{"status": "…"}` alone.
- `workspaces` lists up to 100 active workspaces the person belongs to,
  sorted by name.
- `approve` is refused with `forbidden`, `early_access_required`,
  `token_limit`, or `login_expired` if the session is no longer pending. It
  also returns `unauthenticated` (not signed in) and `invalid_argument` (a
  malformed session or workspace id). Membership and early access are checked
  before the session state, so an expired session approved for a workspace
  without access reads `early_access_required`.
- Any signed-in person can deny a pending session. Only the approver can deny
  an approved one; others get `forbidden`. Denying a denied session returns
  `{"status": "denied"}` again. Denying an expired or used session returns
  `login_expired`.

### 5.3 Code

8 characters from `ABCDEFGHJKMNPQRSTVWXYZ23456789` (no `I`, `L`, `O`, `U`, `0`,
`1`), shown as two groups of four with a hyphen. Before comparing, a client
and the server uppercase the input and drop spaces and hyphens.

## 6. Local tools (no account, no network)

These run on files on the user's own machine. Their exact inputs and outputs
are in `docs/tools.md`. Names are fixed:

`inspect_edf`, `check_signal_quality`, `compute_band_power`,
`compare_hypnograms`, `compare_events`.

Rules for all of them:

- They never parse, keep or return the EDF header's identity fields: patient
  name, id and birth date, the recording field's free text, and the start date
  and time.
- A file is named in results by an opaque id derived from its content, never
  by its path or file name.
- Results are summaries and counts. No tool returns raw signal samples.

## 7. Version 1.1 (staging, internal first)

Everything in this section is version 1.1. It is built on staging and used by
Insai's own agents before any outside workspace sees it. A version 1 server
answers every action named here with 400 `invalid_argument` ("Unknown
action"), and a client treats that as "not available on this server yet".

Three things change:

1. A token carries **scopes**. Version 1 tokens read; a 1.1 token may also
   read per-epoch and per-event detail, propose marks, give review tasks, or
   ask for an analysis. The person chooses the scopes when the token is made.
2. A workspace gets a second switch, **`agentWrite`**, beside `agentAccess`.
   Reads need `agentAccess`; every write needs both.
3. Writes are **reviewed, never authoritative**. An agent's marks land as a
   review layer of their own that members agree or disagree with. They never
   become human labels and never touch a person's scoring. An agent gives a
   review task exactly as a member would from the page. An agent asks for an
   analysis exactly as the uploader would.

The conventions of section 1 hold throughout. Every list below of "Only these
keys" is a literal allowlist on the server, as in version 1.

### 7.1 Scopes

| Scope | Lets the token | Needs |
|---|---|---|
| `read` | Everything in section 3, plus `list_layers`, `list_nights`, `list_tasks` and `list_members`. Every token has it. | `agentAccess` |
| `detail` | `get_events`, `get_stages`, `get_quality`, `list_marks`: per-epoch and per-event reads. | `agentAccess` |
| `marks:write` | `propose_marks`, `withdraw_marks`. | `agentAccess` and `agentWrite` |
| `tasks:write` | `create_review_task`. | `agentAccess` and `agentWrite` |
| `analysis:run` | `request_analysis`. | `agentAccess` and `agentWrite` |

- Scopes are chosen when the token is made: `create` on the page (section
  7.8) and `start` of the terminal sign-in (section 7.9) take a `scopes`
  list. The approval page shows the scopes asked for, and `approve` may
  narrow them. Nothing widens a token later; make a new one.
- `read` is always included. A `scopes` list without it gets it added. An
  unknown scope name is 400 `invalid_argument`.
- Tokens made before version 1.1 carry `["read"]` and keep it.
- `whoami` returns the token's own scopes, `token` rows carry them, and
  `exchange` returns them. Order in the list is `read`, `detail`,
  `marks:write`, `tasks:write`, `analysis:run`.
- Every action below names the scope it needs. A call without it is 403
  `scope_required`, and the body names the scope:

```json
{"error": {"code": "scope_required", "message": "This token cannot read per-epoch detail. Make a token with the detail scope."}, "scope": "detail"}
```

`scope` sits at the top level, next to `error`, as `attemptsLeft` does in
section 5.1.

### 7.2 Workspace switches

| Switch | Set by | Gates |
|---|---|---|
| `agentAccess` (version 1) | Insai staff, `set_access` | Every call. Off: 403 `early_access_required`. |
| `agentWrite` (1.1) | Insai staff, `set_write_access` | Every write-scope action. Off: 403 `write_access_required`. |

`agentWrite` is off when absent and is never on while `agentAccess` is off:
switching `agentAccess` off makes every write fail with
`early_access_required`, which is checked first. Switching `agentWrite` off
does not delete tokens or their scopes; writes stop until it is on again.
`detail` reads need only `agentAccess`.

### 7.3 Errors, limits, usage and audit

Two codes are added to section 3.1, and one for analysis:

| HTTP | `code` | When |
|---|---|---|
| 403 | `scope_required` | The token lacks the scope the action needs. The body carries `scope`. |
| 403 | `write_access_required` | A write-scope action in a workspace whose `agentWrite` is off. |
| 409 | `already_analysed` | `request_analysis` on a recording that already has machine output. |
| 409 | `mark_limit` | `propose_marks` would take the token past 5,000 rows on the recording. |

Order of checks on every call: method and body (400, 405), token (401),
the token's rate limits (429), membership and the workspace (403
`forbidden`), `agentAccess` (403 `early_access_required`), scope (403
`scope_required`), `agentWrite` for a write (403 `write_access_required`),
then the action's own arguments and refusals. A refused call before the
action writes nothing.

Rate limits added to section 3.1, each per token:

| Actions | Limit |
|---|---|
| `list_layers`, `list_nights`, `list_tasks`, `list_members` | 10 a minute each, as `list_recordings` (they read the whole workspace). |
| `propose_marks`, `withdraw_marks`, `create_review_task`, `request_analysis` | 30 a minute, all writes together. |
| Everything else | Inside the 60 a minute of version 1. |

Every call counts toward the 60 a minute. A write counts once however many
rows it carries.

Usage: every call, writes included, is counted in the token's usage as in
version 1; writes are also counted on their own so the page can show them.

Audit: every write that changes something leaves exactly one audit row in
the workspace's audit log, attributed to the token's owner "via agent" with
the token id, the recording id and counts. Never a mark's text, a label or
machine output. A write that changes nothing (a withdraw of rows already
withdrawn, a repeated `opId`) leaves no row. The log is the one Insai staff
already read; nothing in it reaches the API.

### 7.4 Blinding

Two states of the caller on a recording, as in version 1:

- **Held**: a blind task holds the caller on the recording's source or on any
  recording of its night. The row carries `held: true`.
- **Blind desk**: the caller's own desk on the recording hides the machine
  (no hold). The row carries nothing; `get_recording` omits `stageMinutes`
  and `eventCounts`.

**Machine hidden** means either. Then:

- `get_events`, `get_stages {source: "machine"}` and the machine and agent
  rows of `list_marks` are withheld. The response is HTTP 200 with
  `{"hidden": "blind"}` where the output would be (the exact shape is under
  each action).
- `get_stages {source: "mine"}` is served: the caller's own labels are not
  the machine's.
- `get_quality` is served. Signal quality is not machine output in
  Workbench: the desk shows it on a blind night and the version 1 row
  carries `qualityGrade` held or not.
- `list_layers` lists a held recording as `{"id": "…", "hidden": "blind"}`
  with no counts.
- Held, `list_marks` returns the caller's own human marks only. A blind desk
  without a hold returns every human mark.
- `propose_marks` and `withdraw_marks` are refused with 403 `forbidden` on a
  recording where the caller is held. A blind desk does not refuse them.
- No write echoes machine output.

Agent marks are a machine-side layer for blinding: a member held blind on a
night sees none of them, exactly as they see none of the analysis layers.
The rows are authored by the token id, not by a person, so no rule that
shows a held member their own rows shows them an agent's.

### 7.5 Opaque ids added

| Id | Shape | Meaning |
|---|---|---|
| `memberId` | `m_` + 16 hex | A member of this workspace. Derived on the server from the workspace and the person with a server-side secret: the same person has another id in another workspace, and the id cannot be turned back into an account. Returned by `whoami`, `list_members`, `list_tasks` and `list_marks`; taken by `create_review_task`. |
| `nightId` | as `pairId` | A night of two or more recordings. The same value as `pairId` on a recording row. |
| `personKey` | `pk_` + 16 hex | The person who wore the device, as named by the workspace's participant list, hashed per workspace with a server-side secret. The same person has the same key across nights of one workspace and another key in another workspace. Never the participant code itself. |
| task `id` | `task_` + 16 hex | One task of one member on one recording. Derived on the server. |
| mark `id` | string | An annotation row's id. Agent rows are `agent-<tokenId>-<n>`; other rows' ids are opaque. |
| `source` | string | A layer: `analysis:<name>-<version>` for an analysis layer, `agent:<tokenId>` for an agent's. |

`whoami` adds `memberId` (the caller's own) to the keys of section 3.2.

### 7.6 Reads

| Action | Scope | Input | Output keys | Refusals beyond section 7.3 |
|---|---|---|---|---|
| `list_members` | `read` | none | `members` | none |
| `list_tasks` | `read` | `mine?` | `tasks` | `invalid_argument` |
| `list_nights` | `read` | `projectId?` | `nights`, `truncated` | `invalid_argument`, `not_found` |
| `list_layers` | `read` | `projectId?` | `layers`, `truncated` | `invalid_argument`, `not_found` |
| `get_events` | `detail` | `id`, `kind?`, `cursor?`, `limit?` | `available`, `events`, `nextCursor`, `kinds`, `hidden` | `invalid_argument`, `not_found` |
| `get_stages` | `detail` | `id`, `source` | `source`, `available`, `epochSec`, `nEpochs`, `stages`, `model`, `status`, `questionable`, `noSignal`, `hidden` | `invalid_argument`, `not_found` |
| `get_quality` | `detail` | `id` | `available`, `grade`, `score`, `epochSec`, `perEpoch`, `channels` | `invalid_argument`, `not_found` |
| `list_marks` | `detail` | `id`, `origin?`, `cursor?`, `limit?` | `marks`, `nextCursor`, `hidden` | `invalid_argument`, `not_found` |

Every `id` is a recording id. A recording the caller may not see, or one
that is hidden as in version 1, is `not_found`. Cursors follow section 3.2:
opaque, bound to the token and the same arguments, refused with
`invalid_argument` when stale.

#### `list_members`

```json
{"members": [{"id": "m_…", "role": "owner", "you": true}, {"id": "m_…", "role": "member"}]}
```

- Active people of the workspace, sorted by `id`. `role` is `owner` or
  `member`. `you` is present, as `true`, only on the caller's own row.
- No name, initials, email, label, colour or join date. Initials identify
  a person and are left out on purpose.
- The workspace's analysis service member (the author of analysis layers) is
  not a person and is not listed; it cannot be given a task.

#### `list_tasks`

Argument: `mine` (boolean, default `true`).

```json
{
  "tasks": [
    {
      "id": "task_…",
      "recordingId": "e_…",
      "projectId": "p_…",
      "assignee": "m_…",
      "givenBy": "m_…",
      "mode": "blind",
      "state": "open",
      "givenAt": "2026-11-21T10:00:00Z",
      "doneAt": null
    }
  ]
}
```

- `mine: true`: tasks assigned to the caller. `mine: false`: tasks assigned
  to the caller or given by the caller, the two lists the Tasks page shows.
  Nobody sees other people's tasks through the API.
- `mode` is `blind` or `assisted`. `state` is `open`, `done` or `cancelled`.
  Cancelled and done tasks of the last 30 days are listed; older ones are
  not.
- Sorted by `givenAt` newest first, then by `id`. At most 500 rows.
- Never the agreement numbers, a label, a note or a batch id.

#### `list_nights`

Argument: `projectId` (optional).

```json
{
  "nights": [
    {"id": "n_…", "recordingIds": ["e_…", "e_…"], "projectId": "p_…", "personKey": "pk_…"}
  ],
  "truncated": false
}
```

- A night is two or more live recordings sharing a `pairId`; `id` is that
  value. `recordingIds` lists the ones the caller would get from
  `list_recordings`; a night with fewer than two of those is left out.
- `projectId` is present when every listed recording is in the same
  project. With a `projectId` argument, only those nights; an unknown project
  is `not_found`.
- `personKey` is present when the night's recordings name one participant
  and agree on it. Absent when none is named or they disagree.
- Sorted by `id`. `truncated` as in `list_recordings`.
- No night name, note, reference recording or date.

#### `list_layers`

Argument: `projectId` (optional).

```json
{
  "layers": [
    {
      "source": "analysis:ladder-3",
      "name": "ladder",
      "version": "3",
      "origin": "analysis",
      "status": "stable",
      "recordings": [
        {"id": "e_…", "state": "have", "rows": 212, "counts": {"spindle": 180, "slow_wave": 32}},
        {"id": "e_…", "state": "notRun"},
        {"id": "e_…", "hidden": "blind"}
      ],
      "truncated": false
    },
    {
      "source": "agent:tok_0123456789abcdef",
      "name": "agent",
      "version": "tok_0123456789abcdef",
      "origin": "agent",
      "status": "proposed",
      "recordings": [ {"id": "e_…", "state": "have", "rows": 40, "counts": {"arousal": 40}} ],
      "truncated": false
    }
  ],
  "truncated": false
}
```

- One entry per layer that has a status document on at least one listed
  recording, plus every layer the project ticked. `origin` is `analysis` or
  `agent`.
- `status` of an analysis layer comes from the layer catalog:
  `experimental`, `stable` or `retired`; `unknown` when the catalog does not
  name it. An agent layer is always `proposed`.
- `recordings` names each recording the caller would get from
  `list_recordings` (all of them, or the project's). `state` is `have` (rows
  exist), `notRun` (the layer ran and refused or found nothing to write) or
  `missing` (no status document). `rows` counts live rows; `counts` is per
  kind for an agent layer and per class for an analysis layer; each present
  only when known. A held recording is `{"id", "hidden": "blind"}` and
  nothing else.
- At most 1,000 recordings per layer; `truncated` on the layer says more
  exist. The outer `truncated` is `list_recordings`'s.
- Never a class description, a refusal reason or the detector's parameters.

#### `get_events`

Arguments: `id` (required), `kind` (optional, one kind name), `cursor`
(optional), `limit` (integer 1 to 1000, default 500).

```json
{
  "available": true,
  "kinds": ["arousal", "spindle"],
  "events": [
    {"onsetSec": 1832.5, "durationSec": 0.9, "kind": "spindle", "channel": 1, "confidence": 0.81}
  ],
  "nextCursor": null
}
```

- Machine events of the recording's analysis. `kinds` lists the kinds the
  analysis produced (the keys of `eventCounts` in version 1), sorted.
- An event: `onsetSec` from the recording start and `durationSec`, both
  seconds to three decimals; `kind`; `channel`, the 1-based position of the
  channel in `channels.labels` of `get_recording`, left out when the event's
  channel is not one of the recording's channels; `confidence` 0 to 1, only
  when the detector gave one. Nothing else: no free text, no feature values.
- Sorted by `onsetSec`, then `channel`, then `kind`. One event seen on
  several channels is one row per channel; the version 1 `eventCounts`
  counts it once, so totals here are not the same number.
- `available: false` and no `events` key when the recording has no machine
  events (`hasEvents` absent or `false`).
- Machine hidden (section 7.4): the whole body is `{"hidden": "blind"}`.

#### `get_stages`

Arguments: `id` (required), `source` (required): `machine` or `mine`.

```json
{
  "source": "machine",
  "available": true,
  "model": "usleep",
  "epochSec": 30,
  "nEpochs": 960,
  "stages": "WWWN1N1N2N2N2N3…"
}
```

```json
{
  "source": "mine",
  "available": true,
  "status": "in_progress",
  "epochSec": 30,
  "nEpochs": 960,
  "stages": "WW??N2N2…",
  "questionable": [41, 42],
  "noSignal": [900]
}
```

- `stages` is one code per epoch, written one after another with no
  separator: `W`, `N1`, `N2`, `N3`, `R` or `?` (not scored). A reader splits
  it with `W|N[123]|R|\?`; the number of codes is `nEpochs`. `epochSec` is
  the recording's epoch length, 30 unless the recording says otherwise.
- `machine`: the primary machine staging, the one the desk shows. `model` is
  the model's short name as Workbench shows it; other arms are not returned.
  `available: false` and no `stages` when there is no machine staging.
- `mine`: the caller's own scoring of this recording. `status` is
  `in_progress` or `done`. `questionable` and `noSignal` are the epoch
  indexes (0-based) the caller flagged. `available: false` and no `stages`
  when the caller never opened the recording.
- Machine hidden: `{"source": "machine", "hidden": "blind"}`. `mine` is never
  hidden.
- Another person's scoring is not available. There is no `source` for it.

#### `get_quality`

Argument: `id` (required).

```json
{
  "available": true,
  "grade": "marginal",
  "score": 0.71,
  "epochSec": 30,
  "perEpoch": "gggmmbbgg…",
  "channels": [
    {"index": 1, "grade": "good", "score": 0.9, "shares": {"ok": 0.96, "suspect": 0.03, "flat": 0.01, "railed": 0.0}},
    {"index": 2, "grade": "bad", "score": 0.3, "shares": {"ok": 0.4, "suspect": 0.2, "flat": 0.4}, "excluded": "flat"}
  ]
}
```

- `grade` and `score` are the recording's, as on the row. `perEpoch` is one
  letter per epoch: `g` good, `m` marginal, `b` bad, `?` not graded.
- `channels`: one row per graded channel, `index` as in `get_events`.
  `shares` are fractions of epochs: `ok` (no reason fired), `suspect` (any
  reason other than a lost or clipped contact), `flat` (a stuck or lost
  contact), `railed` (clipped at the amplifier's limit). Each present only
  when the stored result tells it apart; when the result does not tell a
  flat row from a railed one, `flat` carries both and `railed` is absent.
  `excluded` names why the channel is left out of the recording grade
  (`flat` or `non_eeg`), present only when it is.
- `available: false` and nothing else when the recording was never graded.
- Not blinded (section 7.4). Never reason codes per epoch, measured values
  or thresholds.

#### `list_marks`

Arguments: `id` (required), `origin` (optional: `human`, `machine` or
`agent`), `cursor` (optional), `limit` (integer 1 to 500, default 200).

```json
{
  "marks": [
    {"id": "…", "origin": "human", "kind": "arousal", "target": "channel", "t0": 1832.0, "t1": 1840.5, "channels": [1], "author": "m_…", "createdAt": "2026-11-20T09:15:00Z"},
    {"id": "analysis-ladder-3-00017", "origin": "machine", "source": "analysis:ladder-3", "kind": "spindle", "target": "channel", "t0": 1832.5, "t1": 1833.4, "channels": [1], "label": "sigma burst", "confidence": 0.81, "verdicts": {"agree": 2, "disagree": 0}},
    {"id": "agent-tok_0123456789abcdef-12", "origin": "agent", "source": "agent:tok_0123456789abcdef", "kind": "arousal", "target": "channel", "t0": 2100.0, "t1": 2104.0, "channels": [2], "label": "EMG rise", "text": "EMG rise with alpha intrusion, 4 s", "confidence": 0.6, "verdicts": {"agree": 0, "disagree": 1}, "createdAt": "2026-11-21T10:00:00Z"}
  ],
  "nextCursor": null
}
```

Only these keys, each present only when known:

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Always present. |
| `origin` | string | Always present: `human`, `machine` (an analysis layer) or `agent`. |
| `source` | string | The layer, for `machine` and `agent` rows. |
| `kind` | string | One of the annotation kinds (section 7.7). |
| `target` | string | `epoch` (a span of the whole recording) or `channel`. |
| `t0`, `t1` | number | Seconds from the recording start, `t0 <= t1`. |
| `channels` | integer list | 1-based channel positions; empty for `target: "epoch"`. |
| `author` | `memberId` | Who wrote a `human` row. Never on `machine` or `agent` rows. |
| `label` | string | A `machine` row's class label or an `agent` row's own label. |
| `text` | string | An `agent` row's own text. Never on a `human` row: a person's text is free text and may hold what a person must not leak. Never on a `machine` row. |
| `confidence` | number | 0 to 1, when the layer gave one. |
| `verdicts` | object | `{"agree": n, "disagree": n}` from members' reviews. On `machine` and `agent` rows. |
| `createdAt` | timestamp | When the row was written. |

- Soft-deleted rows (a withdrawn agent row, a mark a person removed) are
  not listed. Notes (`kind: "note"`) are not listed: they are text.
- Sorted by `t0`, then `id`. `origin` filters to one origin.
- Held: `human` rows are the caller's own only; `machine` and `agent` rows
  are withheld. Blind desk: `human` rows are everyone's; `machine` and
  `agent` rows are withheld. Whenever rows are withheld, the response
  carries `"hidden": "blind"` next to `marks`, and a call with `origin` set
  to a withheld origin returns `{"marks": [], "nextCursor": null, "hidden":
  "blind"}`.

### 7.7 Writes

| Action | Scope | Input | Output keys | Refusals beyond section 7.3 |
|---|---|---|---|---|
| `propose_marks` | `marks:write` | `id`, `marks` (1 to 500), `opId?` | `source`, `accepted`, `ids`, `total` | `invalid_argument`, `not_found`, `forbidden` (held), `mark_limit` |
| `withdraw_marks` | `marks:write` | `id`, `markIds?` (1 to 500) | `withdrawn`, `total` | `invalid_argument`, `not_found`, `forbidden` (held) |
| `create_review_task` | `tasks:write` | `ids` (1 to 100), `memberId`, `mode`, `confirmMachineSeen?` | `batchId`, `given`, `refused` | `invalid_argument`, `not_found` |
| `request_analysis` | `analysis:run` | `id` | `status` | `invalid_argument`, `not_found`, `forbidden`, `already_analysed` |

All four need `agentAccess` and `agentWrite` (section 7.2). Each writes one
audit row (section 7.3) and counts against the writes limit.

#### Agent marks are a review layer

- An agent's marks are rows of their own layer, `source: "agent:<tokenId>"`,
  one layer per token. Members see them on the desk beside the analysis
  layers, labelled with the token's name, and review each row with the same
  agree or disagree the analysis layers get. The verdict counts come back in
  `list_marks`.
- They never change a person's scoring, a person's marks, or an analysis
  layer. They are never counted as anyone's marks (`humanCounts` in
  version 1 does not move). Nothing promotes them to human labels; a member
  who agrees with one still writes their own mark if they want one.
- Their author is the token id, not a person. No member can edit a row from
  the page, including the token's owner; only the token withdraws them.
- Blinding: a member held blind on the night sees none of them (section
  7.4).
- Per token and recording the layer has a status document the desk reads:
  `source`, `rows` (live rows), `counts` per kind, `status: "ok"`,
  `authorLabel`, `pushedAt`. `propose_marks` and `withdraw_marks` keep it
  current.

#### `propose_marks`

Arguments: `id` (recording, required), `marks` (list of 1 to 500, required),
`opId` (optional string of 1 to 64 characters from `0-9A-Za-z_-`).

One mark:

| Key | Required | Rule |
|---|---|---|
| `kind` | yes | One of `bad_signal`, `movement`, `electrode_pop`, `flatline`, `arousal`, `spindle`, `k_complex`, `slow_wave`, `sawtooth`, `eye_movement`. Not `note` (a person's) and not `finding` (the analysis importer's). |
| `t0`, `t1` | yes | Numbers in seconds from the recording start, `0 <= t0 <= t1 <= durationSec`. |
| `channels` | no | 1-based channel positions, each within `channels.count`, no repeats, at most 16. Present and non-empty gives `target: "channel"`; absent or empty gives `target: "epoch"`. |
| `label` | no | 1 to 40 characters. |
| `confidence` | no | Number 0 to 1. |
| `text` | no | 1 to 200 characters. |

`label` and `text` are plain text: trimmed, with control and invisible
characters and line breaks removed; refused when what is left is empty, or
holds an email address, an account id or a recording id. They are shown to
members as the agent's words and are never acted on by the server.

```json
{"source": "agent:tok_0123456789abcdef", "accepted": 40, "ids": ["agent-tok_0123456789abcdef-1", "…"], "total": 40}
```

- All or nothing: one bad mark refuses the call with `invalid_argument`
  naming its position, and nothing is written.
- Rows are numbered per token and recording from 1, in the order sent, and
  a number is never reused; `total` counts the token's live rows on the
  recording after the call. Withdrawn rows keep their number and still count
  toward the cap.
- Cap: 5,000 rows per token per recording, withdrawn ones included. A call
  that would pass it is 409 `mark_limit` and writes nothing.
- `opId`: a call sent again with the same `opId` and the same token within
  24 hours is answered from the first call's result and writes nothing, so a
  retry after a network error does not double the rows. A different body
  under the same `opId` is `invalid_argument`.
- The request body of this action alone may be up to 262,144 bytes. Every
  other action keeps the 2,048 bytes of section 3.
- Refused with `forbidden` while the caller is held on the recording
  (section 7.4). A blind desk does not refuse.
- Nothing about the recording comes back: not a count of machine events,
  not a stage, not a grade.

#### `withdraw_marks`

Arguments: `id` (recording, required), `markIds` (optional list of 1 to 500
mark ids).

```json
{"withdrawn": 12, "total": 28}
```

- Without `markIds`: every live row of this token on the recording. With
  them: those rows. A row that is not this token's on this recording, or that
  does not exist, is `not_found`, and nothing is written.
- Withdrawing is a soft delete: the row leaves the desk and `list_marks`;
  the members' verdicts on it are kept with it. A row already withdrawn
  counts as withdrawn and leaves no audit row.
- `total` is the token's live rows on the recording after the call.
- Refused with `forbidden` while the caller is held.

#### `create_review_task`

Arguments: `ids` (list of 1 to 100 recording ids, required, no repeats),
`memberId` (required), `mode` (`blind` or `assisted`, required),
`confirmMachineSeen` (boolean, default `false`).

```json
{
  "batchId": "…",
  "given": ["e_…", "e_…"],
  "refused": [{"id": "e_…", "code": "open_task"}, {"id": "e_…", "code": "machine_seen"}]
}
```

This is the page's "give a task", with the same rules. The task is given by
the token's owner to the member; the owner may give one to themselves. The
member gets the one email the page sends, naming the workspace and a link to
their tasks, never the agent.

Per recording, `refused` carries one of these codes and the task is not
given; the other recordings are:

| `code` | Why |
|---|---|
| `not_found` | The recording is not in the workspace, or the caller may not see it. |
| `open_task` | The member already has an open task on it. |
| `no_source` | It cannot be given blind: the server does not know its source. Blind only. |
| `staged` | The member already staged it with the machine visible, so blind is impossible. Blind only. |
| `machine_seen` | The member has seen machine output on this source before (another workspace, a share). Blind only. Sent again with `confirmMachineSeen: true`, the task is given and recorded as not truly blind, as the page does. |

- `batchId` names the blind batch: every task given in one call stays blind
  until all of them are done. It is `null` when nothing was given.
- The whole call is refused, writing nothing, with `invalid_argument` (bad
  `ids`, `mode` or `memberId` shape, or the analysis service member) or
  `not_found` (`memberId` is not an active person of the workspace).
- A `machine_seen` refusal is a per-recording code, not a confirm step: the
  first call never gives those tasks. The agent reports them to the person,
  who decides whether to confirm.
- Never a batch id of another call, a label, an email or the agreement
  numbers.

#### `request_analysis`

Argument: `id` (recording, required).

```json
{"status": "started"}
```

The existing "Analyse" of the recording editor, with its rules: the token's
owner must be the person who uploaded the recording, or Insai staff;
anyone else is `forbidden`. The server starts the analysis under its own
service identity on the owner's behalf and never with a person's sign-in.

- `started`: a run was started. `already` (HTTP 200): a run is pending from
  an earlier request; nothing new was started. The call is idempotent: a
  repeat answers `already`.
- 409 `already_analysed`: the recording already has machine output. Nothing
  is started; a re-analysis is not something an agent can ask for.
- `invalid_argument`: the recording was not uploaded as a file, so it cannot
  be analysed here.
- When the analysis service does not answer, 500 `internal`; nothing was
  started, and the call may be sent again.
- Nothing about the analysis comes back: not a job id, not progress. The
  version 1 row's `analysing` shows the run and `machineAvailable` its end.

### 7.8 Token management additions

`workbench_agent_token` (section 4) changes as follows. Everything else in
section 4 holds.

| Action | Change |
|---|---|
| `list` | Returns `agentWrite` (boolean) beside `agentAccess`. `canSetAccess` covers both switches. |
| `create` | Takes `scopes` (optional list of scope names from section 7.1; `["read"]` when absent). The row carries the scopes given. Refused with `invalid_argument` for an unknown scope. A write scope may be put on a token while `agentWrite` is off; the token's writes then fail with `write_access_required` until it is on. |
| `set_write_access` | `on` (boolean). `{"agentWrite": bool}`. Insai staff only; anyone else gets `forbidden`. Refused with `early_access_required` when `agentAccess` is off. Audited as `agent_access_on` and `agent_access_off` are. |

The page shows a token's scopes on its row and lets the person pick them at
creation. Scopes are not edited; a new token is made.

### 7.9 Terminal sign-in additions

`workbench_cli_login` and `workbench_cli_login_review` (section 5) change as
follows. Everything else in section 5 holds.

- `start` takes `scopes` (optional list of scope names). `["read"]` when
  absent; `read` is added when missing; an unknown name is 400
  `invalid_argument`. The session stores the list as asked.
- `get` returns `scopes`: the scopes the terminal asked for while pending,
  and the scopes approved once approved. The approval page shows them to the
  person as a list of what the token will be able to do, before the code.
- `approve` takes `scopes` (optional). When given, it must be a subset of
  the session's requested scopes that still holds `read`; it narrows the
  token to that list. A scope not asked for is `invalid_argument`: the page
  never widens what the terminal asked for. When absent, the token gets the
  requested scopes.
- `exchange` returns `scopes` as minted.
- A write scope may be approved for a workspace whose `agentWrite` is off,
  with the same effect as on the page (section 7.8).

### 7.10 Using scopes (client)

The command line asks for scopes at sign-in:

```
nexus-workbench login --scope detail --scope marks:write
```

- `--scope` repeats; `read` is always included and need not be named. With
  no `--scope`, the token reads, as in version 1.
- The approval page lists the scopes before the person approves. The person
  may approve fewer; `exchange` returns what was approved, and the client
  saves that list beside the token and shows it in `whoami` and `doctor`.
- `login --token` takes a token made on the page; its scopes are whatever
  the page gave it. The client learns them from `whoami` on first use.
- A command or tool that needs a scope the saved token lacks fails locally
  with `scope_required` before any call when the client knows the token's
  scopes, and with the server's `scope_required` otherwise. The message
  names the scope and the `login` command to run.
- The MCP server exposes one tool per action, named as the action. A tool
  the token's scopes do not cover is still listed, so the agent can tell the
  person which scope to add.

Codes added to section 3.3 (the server sends them; the client passes them
through unchanged):

| `code` | When |
|---|---|
| `scope_required` | The token lacks the scope (section 7.1). The client's message names the scope and the `login` command. |
| `write_access_required` | The workspace does not take agent writes yet (section 7.2). The client's message says to ask Insai. |
| `already_analysed` | `request_analysis` on a recording with machine output. |
| `mark_limit` | `propose_marks` past 5,000 rows per token on the recording. |

### 7.11 Still later

Signal download; a `personKey` on recording rows; reading another person's
scoring; editing or re-labelling an existing mark; re-analysis; a staff
action to promote an agent layer. None of these is in version 1.1.
