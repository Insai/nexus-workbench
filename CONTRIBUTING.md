# Contributing

Thanks for helping. Bug reports, fixes and clearer docs are all welcome.

## Set up

You need [uv](https://docs.astral.sh/uv/) and git.

```sh
git clone https://github.com/Insai/nexus-workbench.git
cd nexus-workbench
uv sync
```

`uv sync` creates `.venv` with the package and the dev tools.

## Check your change

Run these from the repository root. All of them must pass before a pull request is
merged.

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
claude plugin validate . --strict
```

The last line needs Claude Code. It checks the plugin and marketplace manifests.

Tests never contact Workbench. The workspace tools are tested against a fake API in
`tests/fake_api.py`, which implements the contract's version 1.1 rules (scopes, switches,
blinding, caps, `opId`, audit). A change to those rules starts in the contract, then the
fake, then the client.

## Test a wheel before a release

A development build is not on the package index, so `setup` run from it cannot install
it from there. Point uv at the folder that holds the wheel:

```sh
uv build
UV_FIND_LINKS=dist uvx --from dist/nexus_workbench-<version>-py3-none-any.whl nexus-workbench setup
```

Or install the wheel yourself with `uv tool install <path-to-wheel>`, then run
`nexus-workbench setup`.

To keep your own agent config untouched, set `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`UV_TOOL_DIR`, `UV_TOOL_BIN_DIR` and `NEXUS_WORKBENCH_CONFIG_DIR` to empty folders
first.

## Testing a branch against a staging server

To try a branch before it is released, install it from that branch:

```sh
uv tool install --force git+https://github.com/Insai/nexus-workbench@<branch>
```

Point it at the server you test against in the shell you use for the rest of these
steps:

```sh
export NEXUS_API_URL=<staging API URL>
```

Sign in with the scopes you want to test. Each `--scope` adds one; `read` is always
included:

```sh
nexus-workbench login --scope detail --scope marks:write --scope tasks:write
```

Then register the MCP server without signing in again:

```sh
nexus-workbench setup --no-login
```

`setup` saves `NEXUS_API_URL` with the server entry, so your agent talks to the same
server. Restart the agent session to pick it up. To go back to the released version, run
`uv tool install --force nexus-workbench`, unset `NEXUS_API_URL`, then run
`nexus-workbench setup` again.

## Rules for changes

- **Contract first.** [docs/contract.md](docs/contract.md) is the source of truth for the
  Workbench API and the error codes. A change to a request, a reply or a code starts as
  an edit to that file. [docs/tools.md](docs/tools.md) does the same for the local tools.
- **Writes are scoped and reviewed.** A tool changes Workbench only with a write scope the
  person chose, and its result is a proposal members review or exactly what the person
  could do from the page (contract section 7). Read tools stay read-only. A local tool
  never changes an existing file; `preprocess` writes only a new one.
- **Privacy.** No tool returns names, paths, EDF header identity fields or raw samples.
  Keep the rules in the README's Privacy section true.
- **Plain language.** Short sentences. Say what happened and what to do next. No hype
  words. Keep the research-use notice where it is.
- **Tests.** Every change in behaviour comes with a test. Update tests that assert text
  you changed.
- **Changelog.** Add a line under the unreleased version in
  [CHANGELOG.md](CHANGELOG.md).

## Releasing

Pushing a tag like `v1.2.3` runs [.github/workflows/release.yml](.github/workflows/release.yml).
It checks that the tag matches the version in the package, runs the checks, builds,
publishes to PyPI and creates a GitHub release with the changelog section as notes and
the built files attached.

The version is written in four places: `pyproject.toml`,
`src/nexus_workbench/__init__.py`, `.claude-plugin/plugin.json` and
`.codex-plugin/plugin.json`. `scripts/bump_version.py` sets all four, and a test fails
if they differ.

1. Set the version:

   ```sh
   python scripts/bump_version.py 1.2.3
   ```

2. In [CHANGELOG.md](CHANGELOG.md), replace `unreleased` in the `## [1.2.3]` heading
   with today's date, like `## [1.2.3] – 2030-01-31`.
3. Check that the release will be accepted, then run the checks above:

   ```sh
   python scripts/bump_version.py --check v1.2.3
   ```

4. Commit on `main` and push the commit.
5. Tag and push the tag:

   ```sh
   git tag v1.2.3
   git push origin v1.2.3
   ```

6. Approve the `pypi` deployment in the Actions run, if the environment asks for it.
7. After the release, set the next development version, like
   `python scripts/bump_version.py 1.2.4.dev0`, and add a new unreleased section to the
   changelog.

The release stops before publishing if any of the four versions differs from the tag,
if a version contains `dev`, or if the changelog section is missing or still says
unreleased.

To try the build without publishing, run the Release workflow by hand from the Actions
tab. It builds and uploads `dist/` as an artifact, and skips PyPI and the GitHub
release.

### One-time setup

PyPI uses trusted publishing, so there is no token to store.

1. On PyPI, open Your account, then Publishing, and add a pending publisher under
   GitHub (it turns into a normal trusted publisher on the first upload):
   - PyPI project name: `nexus-workbench`
   - Owner: `Insai`
   - Repository name: `nexus-workbench`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. On GitHub, open the repository's Settings, then Environments, and create an
   environment named `pypi`:
   - Required reviewers: add at least one maintainer, so every upload waits for a
     person to approve it.
   - Deployment branches and tags: choose selected branches and tags, and add the tag
     rule `v*.*.*`.

## Report a problem

Open an issue with the bug report or feature request form. For a security problem, see
[SECURITY.md](SECURITY.md) instead.
