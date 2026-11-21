# Workbench agent API contract

Version 1, frozen 20 November 2026. The server, the Workbench page and this
package are built against this file. Change it only by editing this file first.

Everything here is read-only. No action changes Workbench data.

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

### 3.3 Codes the client adds

The command line puts the code in brackets in its error messages, like
`Not signed in (not_signed_in)`. With `--json` it prints the error to stderr
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
| `local_file` | A local tool refused the file (missing, not an EDF, too short, EDF+D). |
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

## 5. Terminal sign-in (version 1.1, may ship after launch)

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
