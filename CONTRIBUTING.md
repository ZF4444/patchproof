# Contributing

Thanks for looking at patchproof. Issues and pull requests are welcome.

## Before you start

- For a starter issue, claim it with a comment first, so two people do not build the same thing.
- Keep each change small and focused on one issue.

## Development environment

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (CI uses
0.13.8), then run from the repository root:

```console
uv sync --locked --all-extras
```

uv manages Python 3.12 (from `.python-version`) and the local `.venv`.
The published package continues to support Python 3.10+.
Commit `uv.lock` alongside every dependency change. Use `uv add` for runtime
dependencies, `uv add --dev` for test tools, and `uv add --optional http` for
HTTP-only dependencies. To deliberately upgrade, run `uv lock --upgrade`,
then sync and test before committing. CI uses `--locked` to reject stale locks.

## Running the tests

```console
uv run --locked --all-extras pytest -q
```

The full suite (core and HTTP MCP tests, collected by pytest) must pass before a pull request is reviewed. The ground-truth suite's border-recovery numbers (28 of 28 sides exact) must not go backwards. If your change alters the CLI or its output, update the README in the same pull request — and SKILL.md too, if install, commands, flags, exit codes or output wording changed.

## Pull requests

- Say what you changed and how you tested it.
- A detection change should say what it does to the UNCERTAIN pile: a panel that cannot be proved must stay UNCERTAIN, never gain made-up borders.

Build distributable packages with `uv build`.
