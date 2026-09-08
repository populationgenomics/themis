"""One mocked run of the program per test session: Pulumi's mock runtime is process-global."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from themis_infra import capture


@pytest.fixture(scope='session')
def program() -> capture.Capture:
    return capture.capture_program()


def _top_level_terms(expression: str) -> list[str]:
    """The operands of a parenthesized top-level `+` chain, as `cost_promql.summed` emits one."""
    assert expression.startswith('('), expression
    assert expression.endswith(')'), expression
    inner = expression[1:-1]
    terms, depth, start = [], 0, 0
    for i, char in enumerate(inner):
        depth += (char == '(') - (char == ')')
        if depth == 0 and inner.startswith(' + ', i):
            terms.append(inner[start:i])
            start = i + 3
    terms.append(inner[start:])
    return terms


@pytest.fixture(scope='session')
def top_level_terms() -> Callable[[str], list[str]]:
    """Splits a PromQL sum into its top-level operands, for the tests that read the cost queries' shape."""
    return _top_level_terms
