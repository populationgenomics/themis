"""Validate the committed Zod schemas under schema/tests/zod/.

Guards the S0.3 invariants without the JS toolchain: every committed ``.ts``
imports Zod and is already in dependency-first declaration order (proven by the
reorder pass being a no-op on it — if regen emitted a mis-ordered file, this
trips). The authoritative check that the Zod is *valid* TypeScript is ``tsc``
(``bun run smoke:zod``); these substring checks run without it in the ordinary
pytest job. Freshness against the ``.tsp`` sources is the S0.4 gate.

The ``test_features_*`` cases are the Zod half of the round-trip verification: each
construct in the feature-coverage corpus (``schema/tests/fixtures/features/``) projects to
its expected Zod combinator. The proto half lives in ``test_generated_stubs``. Enum and
timestamp projections are the canonical-JSON forms produced by ``tools.schema.zod_canonicalize``
(name-string enums, ``z.iso.datetime()``), not ``typespec-zod``'s raw output (ADR 0004).
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


# Per-feature Zod projections. Substring checks, not a parser: tsc (bun run
# smoke:zod) is the authoritative validity gate; these only confirm each corpus
# construct reached its expected Zod combinator.
@pytest.mark.parametrize(
    'fragment',
    [
        'export const colour = z.enum(["red", "green", "blue"]);',  # int enum -> canonical name strings
        'optional_field: z.string().optional(),',  # optional
        'flagged: z.boolean().optional().default(false),',  # optional-with-default
        'inner: inner,',  # nested model ref
        'tags: z.array(z.string()),',  # array
        'palette: z.array(colour),',  # array of enum refs
        'count: z.number().int().gte(-2147483648).lte(2147483647),',  # int32 range
        'when: z.iso.datetime(),',  # google.protobuf.Timestamp -> RFC-3339 string
        'link: z.string().url(),',  # url scalar format
    ],
)
def test_features_projects_each_construct(fragment: str) -> None:
    source = (_ZOD_DIR / 'features.ts').read_text()
    assert fragment in source
