"""Tests for the seed parser every evidence interface's fixture backend is built through."""

from __future__ import annotations

import json

import pytest

from themis.rpc import gnomad_pb2
from themis.services.evidence import errors, fixtures

_SECTIONS = frozenset({'describe_variant'})
_VAR = 'THEMIS_GNOMAD_FIXTURE'


def _parsed(raw: str) -> dict[str, gnomad_pb2.DescribeVariantResponse]:
    seeds = fixtures.sections_from_json(raw, var_name=_VAR, sections=_SECTIONS)
    return fixtures.table(seeds, 'describe_variant', gnomad_pb2.DescribeVariantResponse, var_name=_VAR)


def test_a_section_parses_onto_its_message() -> None:
    table = _parsed(
        json.dumps({'describe_variant': {'1-100-A-T': {'raw': {'af': 0.001}, 'provenance': [{'source': 'gnomAD'}]}}})
    )
    resp = fixtures.lookup(table, '1-100-A-T', kind='gnomad')
    assert resp.provenance[0].source == 'gnomAD'
    assert resp.raw['af'] == 0.001


def test_an_unseeded_key_raises_rather_than_answering_empty() -> None:
    # The distinction the fixture exists to keep: "nothing was seeded for this" is not "this variant
    # has no record", and an empty response would be scored as the second.
    with pytest.raises(errors.UnknownVariantError):
        fixtures.lookup(_parsed('{}'), 'nope', kind='gnomad')


def test_an_explicit_empty_store_parses() -> None:
    assert _parsed('{}') == {}


@pytest.mark.parametrize(
    'raw',
    [
        pytest.param(None, id='unset'),
        pytest.param('not json', id='malformed'),
        pytest.param('[]', id='not-an-object'),
        pytest.param(json.dumps({'describe_varyant': {}}), id='unknown-section'),
    ],
)
def test_an_unusable_seed_exits(raw: str | None) -> None:
    with pytest.raises(SystemExit):
        fixtures.sections_from_json(raw, var_name=_VAR, sections=_SECTIONS)


@pytest.mark.parametrize(
    'section',
    [
        pytest.param([], id='section-not-an-object'),
        pytest.param({'1-100-A-T': []}, id='entry-not-an-object'),
        pytest.param({'1-100-A-T': {'no_such_field': 1}}, id='entry-not-the-message'),
    ],
)
def test_an_unusable_section_exits(section: object) -> None:
    with pytest.raises(SystemExit):
        _parsed(json.dumps({'describe_variant': section}))
