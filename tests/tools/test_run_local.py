"""Tests for the local ingestion driver's argument guards (`tools.litcache.run_local`).

Argument parsing only — a real pass needs GCS, Postgres and the DirectRunner. The guard
worth pinning is the one on `--pg-host`: the crosswalk rebuild it enables runs an
unconditional `DELETE FROM litcache.crosswalk`, and the tool cannot tell a throwaway
instance from the real one behind a proxy on localhost. Nothing else stands between the
two, so a later refactor of `_parse_args` must not be able to drop it silently.
"""

from __future__ import annotations

import pytest

from tools.litcache import run_local

_REQUIRED = ['--scratch-bucket', 'scratch', '--limit', '1']
_PG = ['--pg-host', 'localhost', '--pg-user', 'u', '--pg-database', 'd']


def test_pg_host_requires_the_destructive_rebuild_opt_in() -> None:
    with pytest.raises(SystemExit):
        run_local._parse_args([*_REQUIRED, *_PG])


def test_pg_host_with_the_opt_in_is_accepted() -> None:
    args = run_local._parse_args([*_REQUIRED, *_PG, '--rebuild-crosswalk'])
    assert args.pg_host == 'localhost'
    assert args.rebuild_crosswalk


def test_the_throwaway_path_needs_no_opt_in() -> None:
    # No --pg-host: the container is created and dropped by this run, so replacing its
    # crosswalk destroys nothing the operator owns.
    args = run_local._parse_args(_REQUIRED)
    assert args.pg_host is None


@pytest.mark.parametrize(
    'bad',
    [
        ['--limit', '0'],
        ['--direct-num-workers', '0'],
        ['--max-dead-letters', '-1'],
    ],
)
def test_rejects_out_of_range_values(bad: list[str]) -> None:
    with pytest.raises(SystemExit):
        run_local._parse_args(['--scratch-bucket', 'scratch', '--limit', '1', *bad])
