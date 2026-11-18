"""Cross-worker image-build locking and per-test crosswalk truncation."""

from __future__ import annotations

import concurrent.futures
import contextlib
import multiprocessing
import pathlib
from collections.abc import Callable

import pg8000.dbapi
import pytest

from themis.litcache import crosswalk
from themis.testing import postgres


def _worker_build(lock_dir: pathlib.Path, testrun_uid: str, worker: str, log_path: pathlib.Path) -> None:
    def record() -> None:
        with log_path.open('a', encoding='utf-8') as f:
            f.write(f'{worker}\n')

    postgres.build_image_once(record, lock_dir=lock_dir, testrun_uid=testrun_uid, worker=worker)


def test_workers_sharing_a_testrun_build_the_image_once(tmp_path: pathlib.Path) -> None:
    log_path = tmp_path / 'builds.log'
    ctx = multiprocessing.get_context('spawn')
    with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=ctx) as pool:
        futures = [pool.submit(_worker_build, tmp_path, 'run-1', f'gw{idx}', log_path) for idx in range(4)]
        for future in futures:
            future.result()

    assert len(log_path.read_text('utf-8').splitlines()) == 1


def test_a_new_testrun_rebuilds_the_image(tmp_path: pathlib.Path) -> None:
    calls: list[str] = []
    postgres.build_image_once(lambda: calls.append('first'), lock_dir=tmp_path, testrun_uid='run-1', worker='gw0')
    postgres.build_image_once(lambda: calls.append('same-run'), lock_dir=tmp_path, testrun_uid='run-1', worker='gw1')
    postgres.build_image_once(lambda: calls.append('second'), lock_dir=tmp_path, testrun_uid='run-2', worker='gw0')

    assert calls == ['first', 'second']


@pytest.mark.parametrize(
    ('testrun_uid', 'worker'),
    [(None, None), ('stale-inherited-uid', None), (None, 'gw0')],
)
def test_outside_an_xdist_worker_the_image_builds_directly(
    tmp_path: pathlib.Path, testrun_uid: str | None, worker: str | None
) -> None:
    calls: list[int] = []
    postgres.build_image_once(lambda: calls.append(1), lock_dir=tmp_path, testrun_uid=testrun_uid, worker=worker)
    postgres.build_image_once(lambda: calls.append(2), lock_dir=tmp_path, testrun_uid=testrun_uid, worker=worker)

    assert calls == [1, 2]
    assert list(tmp_path.iterdir()) == []


def test_crosswalk_connect_opens_multiple_connections_without_retruncating_within_a_test(
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> None:
    with contextlib.closing(crosswalk_connect()) as first:
        minted = crosswalk.mint(first, ['doi:10.1/within-test'])
    with (
        contextlib.closing(crosswalk_connect()) as second,
        contextlib.closing(second.cursor()) as cur,
    ):
        cur.execute('SELECT doc_id FROM litcache.crosswalk WHERE external_id = %s', ('doi:10.1/within-test',))
        assert [row[0] for row in cur.fetchall()] == [minted.doc_id]
