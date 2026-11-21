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
`tests/fake_api.py`.

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

## Rules for changes

- **Contract first.** [docs/contract.md](docs/contract.md) is the source of truth for the
  Workbench API and the error codes. A change to a request, a reply or a code starts as
  an edit to that file. [docs/tools.md](docs/tools.md) does the same for the local tools.
- **Read-only.** No tool may change anything in Workbench or on the user's computer.
- **Privacy.** No tool returns names, paths, EDF header identity fields or raw samples.
  Keep the rules in the README's Privacy section true.
- **Plain language.** Short sentences. Say what happened and what to do next. No hype
  words. Keep the research-use notice where it is.
- **Tests.** Every change in behaviour comes with a test. Update tests that assert text
  you changed.
- **Changelog.** Add a line under the unreleased version in
  [CHANGELOG.md](CHANGELOG.md).

## Report a problem

Open an issue with the bug report or feature request form. For a security problem, see
[SECURITY.md](SECURITY.md) instead.
