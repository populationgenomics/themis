"""A name or label classic PromQL cannot parse takes the quoted form; a classic one stays bare."""

from __future__ import annotations

from themis_infra import promql


def test_a_dotted_name_moves_inside_the_braces_quoted() -> None:
    assert promql.selector('rpc.server.call.duration_count') == '{"rpc.server.call.duration_count"}'
    assert promql.selector('themis_x', promql.equals('reason', 'invalid')) == 'themis_x{reason="invalid"}'


def test_a_dotted_label_is_quoted_where_it_is_matched() -> None:
    matcher = promql.among('rpc.response.status_code', ['UNKNOWN', 'INTERNAL'])
    assert matcher == '"rpc.response.status_code"=~"^(INTERNAL|UNKNOWN)$"'
    assert promql.selector('rpc.server.call.duration_count', matcher) == (
        '{"rpc.server.call.duration_count", "rpc.response.status_code"=~"^(INTERNAL|UNKNOWN)$"}'
    )
