# themis

Themis — agentic rare-disease curation platform (placeholder name).

## Repositories

- `populationgenomics/themis-internal` — primary working tree (private). All
  development happens here.
- `populationgenomics/themis` — public mirror. Each commit merged to
  `main` in the internal repo is pushed 1:1 to the public repo, preserving
  SHAs.

The mirror is automated; see [`docs/plans/screen-and-mirror-workflow.md`](docs/plans/screen-and-mirror-workflow.md)
for how PRs are screened before merge and how the mirror push works.

## Development

Requirements: Python 3.13+ and [uv](https://docs.astral.sh/uv/).

Dependencies are declared as PEP 735 dependency groups in `pyproject.toml`
and locked in `uv.lock`. Sync the test or lint group before running the
corresponding commands:

    uv sync --group test
    uv run pytest

    uv sync --group lint
    uv run ruff check .
    uv run ruff format --check .
    uv run pyright

Auto-fixers:

    uv run ruff check --fix .
    uv run ruff format .

After editing dependencies in `pyproject.toml`, regenerate the lock and
commit it alongside the manifest change:

    uv lock

Without uv, install the same packages with pip and drop the `uv run`
prefix:

    pip install pytest pyyaml ruff pyright types-PyYAML

Optional: install [pre-commit](https://pre-commit.com/) and enable hooks
to run lint/format checks on every commit:

    uv tool install pre-commit
    pre-commit install

## License

[MIT](LICENSE).
