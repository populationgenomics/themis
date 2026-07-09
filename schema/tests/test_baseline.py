"""Test the baseline git-ref helpers (``tools.schema.baseline``)."""

from __future__ import annotations

import pytest

from tools.schema import baseline


def test_require_ref_raises_on_unresolvable_ref() -> None:
    # A bad baseline ref must fail loud, not degrade to a silent "no baseline" pass.
    with pytest.raises(SystemExit):
        baseline.require_ref('definitely-not-a-real-ref-zzz')


def test_require_ref_accepts_a_resolvable_ref() -> None:
    baseline.require_ref('HEAD')  # resolves in this repo; must not raise
