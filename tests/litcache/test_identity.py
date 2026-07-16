"""Tests for `themis.litcache.identity` over the S2 mixed-id fixtures.

Each `ids/` fixture is named with the bucket-style encoded key (the thing under
test) and carries a Docling `origin.filename` as a second id; the expectations
below mirror the fixture README's resolution table.
"""

from __future__ import annotations

import pathlib

import pytest

from themis.litcache import identity

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'


def _origin(fixture: str) -> identity.DoclingOrigin:
    return identity.read_docling_origin((_FIXTURES / fixture).read_bytes())


@pytest.mark.parametrize(
    ('bucket_key', 'fixture', 'mint_keys', 'claim_key', 'content_addressed'),
    [
        (
            '10.1234%2Fsynthetic.fixture.001.json',
            'ids/10.1234%2Fsynthetic.fixture.001.json',
            ('doi:10.1234/synthetic.fixture.001', 'pmid:30000001'),
            'doi:10.1234/synthetic.fixture.001',
            False,
        ),
        (
            '30000002.json',
            'ids/30000002.json',
            ('pmid:30000002',),
            'pmid:30000002',
            False,
        ),
        (
            '1-s2.0-S0000000000000001-main.json',
            'ids/1-s2.0-S0000000000000001-main.json',
            ('pii:S0000000000000001',),
            'pii:S0000000000000001',
            False,
        ),
        # Double-encoded DOI (%252F): must decode twice to the same DOI.
        (
            '10.1234%252Fsynthetic.fixture.002.json',
            'ids/10.1234%252Fsynthetic.fixture.002.json',
            ('doi:10.1234/synthetic.fixture.002', 'pmid:30000003'),
            'doi:10.1234/synthetic.fixture.002',
            False,
        ),
        # Opaque key and opaque origin filename → content-hash fallthrough.
        (
            'qims-synthetic-001.json',
            'ids/qims-synthetic-001.json',
            ('binhash:2000000000000000005',),
            'binhash:2000000000000000005',
            True,
        ),
    ],
)
def test_determine_identity_over_mixed_id_keys(
    bucket_key: str,
    fixture: str,
    mint_keys: tuple[str, ...],
    claim_key: str,
    content_addressed: bool,
) -> None:
    result = identity.determine_identity(bucket_key, _origin(fixture))
    assert set(result.mint_keys) == set(mint_keys)
    assert result.claim_key == claim_key
    assert result.content_addressed is content_addressed


def test_oa_origin_filename_is_a_re_encoded_doi() -> None:
    # The OA paper's origin.filename is itself a URL-encoded DOI, so the second
    # id collapses onto the same `doi:` mint key as the bucket key.
    result = identity.determine_identity('10.1186%2Fs13073-017-0482-5.json', _origin('oa/docling.json'))
    assert result.mint_keys == ('doi:10.1186/s13073-017-0482-5',)
    assert result.claim_key == 'doi:10.1186/s13073-017-0482-5'
    assert result.binary_hash == '6670231034580264005'
    assert result.content_addressed is False


def test_content_hash_fallthrough_uses_binary_hash() -> None:
    origin = identity.DoclingOrigin(filename='opaque-name.pdf', binary_hash='42')
    result = identity.determine_identity('opaque-key.json', origin)
    assert result.mint_keys == ('binhash:42',)
    assert result.content_addressed is True


def test_no_external_id_and_no_binary_hash_fails_loud() -> None:
    origin = identity.DoclingOrigin(filename='opaque-name.pdf', binary_hash=None)
    with pytest.raises(ValueError, match='cannot determine identity'):
        identity.determine_identity('opaque-key.json', origin)


def test_pmcid_classification() -> None:
    result = identity.determine_identity('PMC5664429.json', identity.DoclingOrigin(filename=None, binary_hash=None))
    assert result.mint_keys == ('pmcid:PMC5664429',)
    assert result.claim_key == 'pmcid:PMC5664429'


def test_read_docling_origin_harvests_filename_and_hash() -> None:
    origin = _origin('ids/10.1234%2Fsynthetic.fixture.001.json')
    assert origin.filename == '30000001.pdf'
    assert origin.binary_hash == '2000000000000000001'


def test_read_docling_origin_missing_origin_is_none() -> None:
    origin = identity.read_docling_origin(b'{"name": "no-origin"}')
    assert origin == identity.DoclingOrigin(filename=None, binary_hash=None)


def test_read_docling_origin_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match='not valid JSON'):
        identity.read_docling_origin(b'{not json')
