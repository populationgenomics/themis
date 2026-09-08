"""The spend dashboard reads only the spend metrics, in the units its widgets say, laid out inside its grid."""

from __future__ import annotations

import itertools
import json
import re
from typing import NamedTuple

import pytest

from themis_infra import capture, claude_code_metrics, cost_metrics, cost_prices, cost_promql

_DASHBOARD_CHAIN = ['themis:infra:CostDashboard', 'gcp:monitoring/dashboard:Dashboard']
# A metric name as it can appear in a query: a bare identifier that is not a call, or quoted inside the braces.
_IDENTIFIER = re.compile(r'(?<![A-Za-z0-9_:])[A-Za-z_:][A-Za-z0-9_:]*')
_QUOTED_NAME = re.compile(r'\{"([^"]+)"')
# What a bare identifier can be besides a metric name, once calls are set aside.
_KEYWORDS = frozenset({'and', 'or', 'unless', 'bool', 'offset', 'group_left', 'group_right'})
# Label matchers (a `${variable}` inside them has braces of its own), string literals, grouping lists and ranges:
# identifiers inside them are labels, variables or arguments, not metrics.
_NOT_A_METRIC = re.compile(r'\{(?:[^{}]|\$\{[^}]*\})*\}|"[^"]*"|\b(?:by|on|ignoring|without)\s*\([^)]*\)|\[[^\]]*\]')
# A literal multiplier on a token figure: the price a table row supplies.
_MULTIPLIER = re.compile(r'\) \* (\d+)\b')
# The cents-to-dollars division, as distinct from the per-million-tokens one.
_CENTS_TO_DOLLARS = re.compile(r'/ 100(?!\d)')
# The gauges that carry the agent label.
_SESSION_GAUGES = frozenset(
    {
        cost_metrics.SESSION_LIST_COST_CENTS,
        cost_metrics.SESSION_TOKENS,
        cost_metrics.SESSION_ACTIVE_SECONDS,
        cost_metrics.SESSION_WEB_SEARCH_REQUESTS,
    }
)


class _Tile(NamedTuple):
    title: str
    kind: str
    x: int
    y: int
    width: int
    height: int


class _Query(NamedTuple):
    section: int
    """Which section header the widget sits under, counting from the top."""
    title: str
    unit: str
    """The y-axis label of a chart; a scorecard's title, which carries its unit."""
    promql: str


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return value


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list), value
    return value


def _int(value: object) -> int:
    assert isinstance(value, int), value
    return value


def _layout(dashboard: dict[str, object]) -> tuple[int, list[dict[str, object]]]:
    layout = _mapping(dashboard['mosaicLayout'])
    return _int(layout['columns']), [_mapping(tile) for tile in _sequence(layout['tiles'])]


def _tiles(dashboard: dict[str, object]) -> list[_Tile]:
    _, tiles = _layout(dashboard)
    found = []
    for tile in tiles:
        widget = _mapping(tile['widget'])
        [kind] = [key for key in widget if key != 'title']
        found.append(
            _Tile(
                title=str(widget.get('title', '')),
                kind=kind,
                x=_int(tile['xPos']),
                y=_int(tile['yPos']),
                width=_int(tile['width']),
                height=_int(tile['height']),
            )
        )
    return found


def _queries(dashboard: dict[str, object]) -> list[_Query]:
    """Every `timeSeriesQuery` a widget carries, with the unit the widget states for it and the section it is in."""
    queries = []
    section = -1
    _, tiles = _layout(dashboard)
    for tile in sorted(tiles, key=lambda tile: (_int(tile['yPos']), _int(tile['xPos']))):
        widget = _mapping(tile['widget'])
        title = str(widget.get('title', ''))
        if 'sectionHeader' in widget:
            section += 1
        if 'scorecard' in widget:
            query = _mapping(_mapping(widget['scorecard'])['timeSeriesQuery'])
            assert set(query) == {'prometheusQuery'}, query
            queries.append(_Query(section, title, title, str(query['prometheusQuery'])))
        if 'xyChart' in widget:
            chart = _mapping(widget['xyChart'])
            unit = str(_mapping(chart['yAxis'])['label'])
            for data_set in _sequence(chart['dataSets']):
                query = _mapping(_mapping(data_set)['timeSeriesQuery'])
                assert set(query) == {'prometheusQuery'}, query
                queries.append(_Query(section, title, unit, str(query['prometheusQuery'])))
    return queries


def _metric_names(promql: str) -> set[str]:
    """Every metric name a query reads: the quoted ones, and each bare identifier that is not a call or a keyword."""
    quoted = set(_QUOTED_NAME.findall(promql))
    stripped = _NOT_A_METRIC.sub('', promql)
    bare = set()
    for match in _IDENTIFIER.finditer(stripped):
        is_call = stripped[match.end() :].lstrip().startswith('(')
        if not is_call and match.group() not in _KEYWORDS:
            bare.add(match.group())
    return quoted | bare


@pytest.fixture(scope='module')
def dashboard(program: capture.Capture) -> dict[str, object]:
    [registration] = [r for urn, r in program.resources.items() if capture.urn_type_chain(urn) == _DASHBOARD_CHAIN]
    return _mapping(json.loads(str(registration.state['dashboardJson'])))


def test_every_query_reads_only_the_spend_metrics(dashboard: dict[str, object]) -> None:
    queries = _queries(dashboard)
    assert queries
    for query in queries:
        # The builders select by name, never by an `__name__` matcher the name tokenizer would not see.
        assert '__name__' not in query.promql, query
        names = _metric_names(query.promql)
        assert names, query
        assert names <= cost_promql.METRICS, (query.title, names - cost_promql.METRICS)


def test_a_dotted_name_appears_only_in_its_quoted_form(dashboard: dict[str, object]) -> None:
    for query in _queries(dashboard):
        for name in (claude_code_metrics.TOKEN_USAGE, claude_code_metrics.COST_USAGE):
            bare = query.promql.replace(f'{{"{name}"', '')
            assert name not in bare, query


def test_the_agent_filter_narrows_whole_sections_of_session_gauges(dashboard: dict[str, object]) -> None:
    # The picker narrows a section or leaves it alone: a query in a narrowed section that omits the variable stays
    # unfiltered while the picker says otherwise. A query that carries it reads only the gauges with the label; on a
    # producer's series without it the matcher would blank the widget whenever an agent is picked. A default value
    # would be a selected value, and the wildcard is only what an omitted default resolves to.
    [agent_filter] = [_mapping(f) for f in _sequence(dashboard['dashboardFilters'])]
    assert agent_filter['labelKey'] == cost_metrics.AGENT_LABEL
    assert 'stringValue' not in agent_filter
    variable = '${' + str(agent_filter['templateVariable']) + '}'
    queries = _queries(dashboard)
    narrowed = {query.section for query in queries if variable in query.promql}
    assert narrowed
    for query in queries:
        assert (variable in query.promql) == (query.section in narrowed), query
        if variable in query.promql:
            assert _metric_names(query.promql) <= _SESSION_GAUGES, query


def test_every_dollar_figure_divides_cents_by_100_exactly_once(dashboard: dict[str, object]) -> None:
    # The metrics hold cents; Claude Code's cost counter alone is already USD, and a widget reading only it converts
    # nothing. Every other USD widget converts once, and no other widget converts at all.
    for query in _queries(dashboard):
        native_usd = _metric_names(query.promql) == {claude_code_metrics.COST_USAGE}
        expected = 1 if 'USD' in query.unit and not native_usd else 0
        assert len(_CENTS_TO_DOLLARS.findall(query.promql)) == expected, query


def _table_prices() -> set[str]:
    return {str(cents) for row in cost_prices.LIST_PRICES_CENTS_PER_MTOK.values() for cents in row.values()}


def test_every_convert_multiplier_is_a_table_price(dashboard: dict[str, object]) -> None:
    # The price table is rendered into the convert queries as literals; a figure multiplied by anything else is
    # mispriced. Scoped to the queries reading the token counter alone: a workspace total also carries the
    # cents-per-dollar factor on its CI term.
    convert = [q for q in _queries(dashboard) if _metric_names(q.promql) == {cost_metrics.REQUEST_TOKENS}]
    priced = [q for q in convert if 'USD' in q.unit]
    assert priced
    for query in convert:
        multipliers = set(_MULTIPLIER.findall(query.promql))
        assert multipliers <= _table_prices(), (query.title, multipliers - _table_prices())
        if query in priced:
            assert multipliers, query
            assert f'{cost_metrics.MODEL_LABEL}=' in query.promql, query


def test_the_workspace_totals_only_other_multiplier_is_cents_per_dollar(dashboard: dict[str, object]) -> None:
    totals = [q for q in _queries(dashboard) if _metric_names(q.promql) > {cost_metrics.REQUEST_TOKENS}]
    assert totals
    for query in totals:
        [window] = set(re.findall(r'\[(\w+)\]', query.promql))
        # The CI term is USD scaled to cents; every other multiplier prices tokens.
        ci_in_cents = f'{cost_promql.ci_cents(window)} or vector(0))'
        assert ci_in_cents in query.promql, query
        rest = query.promql.replace(ci_in_cents, '')
        assert set(_MULTIPLIER.findall(rest)) <= _table_prices(), query


def test_every_chart_labels_its_axis_with_a_unit(dashboard: dict[str, object]) -> None:
    charts = [query for query in _queries(dashboard) if query.unit != query.title]
    assert charts
    for query in charts:
        assert query.unit.strip(), query


def test_the_scorecards_are_the_workspace_figure_over_distinct_windows(dashboard: dict[str, object]) -> None:
    # The one figure every producer is summed into (its guards are `cost_promql.workspace_cents`'s), in dollars,
    # over a different window each.
    scorecards = [query for query in _queries(dashboard) if query.unit == query.title]
    assert scorecards
    windows = []
    for query in scorecards:
        [window] = set(re.findall(r'\[(\w+)\]', query.promql))
        windows.append(window)
        assert query.promql == cost_promql.dollars(cost_promql.workspace_cents(window)), query
    assert len(set(windows)) == len(windows)


def test_each_section_opens_with_a_header_and_an_about_tile(dashboard: dict[str, object]) -> None:
    tiles = sorted(_tiles(dashboard), key=lambda tile: (tile.y, tile.x))
    headers = [i for i, tile in enumerate(tiles) if tile.kind == 'sectionHeader']
    assert headers
    assert len(headers) == sum(tile.kind == 'text' for tile in tiles)
    for i in headers:
        assert tiles[i].title
        assert tiles[i + 1].kind == 'text', tiles[i]


def test_tiles_fit_the_grid_without_overlap(dashboard: dict[str, object]) -> None:
    columns, _ = _layout(dashboard)
    tiles = _tiles(dashboard)
    for tile in tiles:
        assert tile.x >= 0, tile
        assert tile.x + tile.width <= columns, tile
        assert tile.width >= 1, tile
        assert tile.height >= 1, tile
    for a, b in itertools.combinations(tiles, 2):
        disjoint_x = a.x + a.width <= b.x or b.x + b.width <= a.x
        disjoint_y = a.y + a.height <= b.y or b.y + b.height <= a.y
        assert disjoint_x or disjoint_y, (a.title, b.title)
