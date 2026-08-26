"""Tests for the VariantValidator adapter, against a recorded fixture via a mocked transport."""

from __future__ import annotations

import asyncio
import json
import pathlib

import httpx
import pytest

from themis.services.evidence import errors
from themis.services.evidence.upstreams import variant_validator

_FIXTURE = json.loads((pathlib.Path(__file__).parent / 'fixtures' / 'variant_validator.json').read_text())


def _fetch(handler: httpx.MockTransport) -> variant_validator.VariantValidatorResult:
    async def run() -> variant_validator.VariantValidatorResult:
        async with httpx.AsyncClient(transport=handler) as client:
            return await variant_validator.fetch_variant_validator(
                'GRCh38', 'NM_000546.6:c.524G>A', 'mane', http_client=client
            )

    return asyncio.run(run())


def test_happy_path_parses_projection_and_both_build_loci() -> None:
    result = _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json=_FIXTURE)))

    assert result.gene == 'TP53'
    assert len(result.transcripts) == 1
    projection = result.transcripts[0]
    assert projection.transcript == 'NM_000546.6'
    assert projection.hgvs_c == 'NM_000546.6:c.524G>A'
    assert projection.hgvs_p == 'NP_000537.3:p.(Arg175His)'
    assert projection.mane_select
    assert not projection.mane_plus_clinical
    assert result.grch38_vcf is not None
    assert (result.grch38_vcf.chrom, result.grch38_vcf.pos, result.grch38_vcf.ref, result.grch38_vcf.alt) == (
        '17',
        '7675088',
        'C',
        'T',
    )
    assert result.grch37_vcf is not None
    assert result.grch37_vcf.pos == '7578406'
    assert result.dataset_versions == ('4.0.1.dev7+gbdab9c72f', 'vvdb_2025_3')


def test_non_2xx_raises_http_status_error() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _fetch(httpx.MockTransport(lambda _: httpx.Response(500, json={})))


def test_response_without_transcript_variant_is_an_absent_record() -> None:
    with pytest.raises(errors.UnknownVariantError, match='no transcript variant'):
        _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json={'flag': 'warning', 'metadata': {}})))


def test_validation_failure_surfaces_the_reason_it_gave() -> None:
    """The warning lines carry why validation failed; an error without them cannot be acted on."""
    payload = {
        'flag': 'warning',
        'metadata': {},
        'validation_warning_1': {
            'hgvs_transcript_variant': '',
            'validation_warnings': ['InvalidFieldError: ENST00000358273.9 is not in the RefSeq data set'],
        },
    }
    with pytest.raises(errors.UnknownVariantError, match='not in the RefSeq data set'):
        _fetch(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
