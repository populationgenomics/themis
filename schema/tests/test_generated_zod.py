"""Validate the committed Zod schemas under schema/tests/zod/.

Guards the S0.3 invariants without the Node toolchain: every committed ``.ts``
imports Zod and is already in dependency-first declaration order (proven by the
reorder pass being a no-op on it — if regen emitted a mis-ordered file, this
trips). The authoritative check that the Zod is *valid* TypeScript is ``tsc``
(``npm run smoke:zod``), which needs Node and runs in CI (S0.4); these run in
the ordinary pytest job. Freshness against the ``.tsp`` sources is the same S0.4
gate.
"""

from __future__ import annotations

import pathlib

import pytest

from tools.schema import zod_reorder

_ZOD_DIR = pathlib.Path(__file__).resolve().parent / 'zod'


def _committed_zod() -> list[pathlib.Path]:
    return sorted(_ZOD_DIR.glob('*.ts'))


def test_at_least_one_zod_committed() -> None:
    assert _committed_zod(), f'no *.ts under {_ZOD_DIR}'


@pytest.mark.parametrize('zod_path', _committed_zod(), ids=lambda p: p.name)
def test_imports_zod(zod_path: pathlib.Path) -> None:
    assert 'import { z } from "zod";' in zod_path.read_text()


@pytest.mark.parametrize('zod_path', _committed_zod(), ids=lambda p: p.name)
def test_is_dependency_ordered(zod_path: pathlib.Path) -> None:
    # The committed file is what regen wrote (reorder already applied); running
    # the pass again must change nothing. A non-idempotent result means a schema
    # is declared before one it references — the TS2448 the pass exists to fix.
    source = zod_path.read_text()
    assert zod_reorder.reorder(source) == source
