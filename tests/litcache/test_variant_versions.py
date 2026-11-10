"""Moving a change from an older version of its accession to the newest, where the two are the same around it."""

from __future__ import annotations

import dataclasses
from typing import cast

import pytest
import weaver

from themis.litcache import variant_fit, variant_reference, variant_versions
from themis.litcache.models import variants_pb2

# Two protein versions differing at residue 30 alone; two transcript versions whose 5' UTR grew by five bases, so one
# coding sequence sits at c.1 = base 11 on the older and base 16 on the newer.
_CODING = 'ATG' + 'GCTAGCATCG' * 12
_SEQUENCES = {
    'NP_1.1': 'M' + 'ACDEFGHIKL' * 2 + 'MNPQRSTVW' + 'Y' + 'ACDEFGHIKL' * 3,
    'NP_1.2': 'M' + 'ACDEFGHIKL' * 2 + 'MNPQRSTVW' + 'A' + 'ACDEFGHIKL' * 3,
    'NM_1.1': 'GGGGGGGGGG' + _CODING,
    'NM_1.2': 'TTTTTGGGGGGGGGG' + _CODING,
}
_CDS_START = {'NM_1.1': 11, 'NM_1.2': 16}


@dataclasses.dataclass
class _Mapper:
    projections: dict[str, str]

    def vrs_id(self, variant: object) -> str:
        expression = variant.format()  # type: ignore[attr-defined]
        if expression not in self.projections:
            raise KeyError(expression)
        return self.projections[expression]


class _Stores:
    """Versions .1 and .2 of each accession, or .0 to .2, where .0 is the same as .1."""

    def __init__(self, projections: dict[str, str] | None = None, *, versions: int = 2) -> None:
        self._mapper = _Mapper(projections or {})
        self._first = 3 - versions

    def held_versions(self, accession: str) -> list[str]:
        base = accession.partition('.')[0]
        return [f'{base}.{v}' for v in range(self._first, 3)]

    def cds_start(self, accession: str) -> int:
        return _CDS_START[accession]

    def sequence(self, accession: str, start: int, end: int) -> str:
        return _SEQUENCES[accession.replace('.0', '.1')][start - 1 : end]

    def mapper(self, _accession: str) -> _Mapper:
        return self._mapper


def _updated(stores: _Stores, *hgvs: str) -> list[str]:
    mention = variants_pb2.Mention(verbatim='x', candidates=[variants_pb2.Candidate(hgvs=h) for h in hgvs])
    out = variant_versions.update(
        variants_pb2.MentionSet(mentions=[mention]), cast(variant_reference.References, stores)
    )
    return [c.hgvs for c in out.mentions[0].candidates]


def test_a_protein_change_far_from_the_versions_difference_moves_to_the_newest() -> None:
    assert _updated(_Stores(), 'NP_1.1:p.Glu4Lys') == ['NP_1.1:p.Glu4Lys', 'NP_1.2:p.Glu4Lys']


def test_a_protein_change_near_the_versions_difference_stays() -> None:
    assert _updated(_Stores(), 'NP_1.1:p.Glu24Lys') == ['NP_1.1:p.Glu24Lys']


def test_a_coding_change_is_compared_in_coding_coordinates() -> None:
    assert _updated(_Stores(), 'NM_1.1:c.4G>A') == ['NM_1.1:c.4G>A', 'NM_1.2:c.4G>A']


def test_placed_versions_are_compared_by_their_projection() -> None:
    same = _Stores({'NM_1.1:c.4G>A': 'ga4gh:VA.x', 'NM_1.2:c.4G>A': 'ga4gh:VA.x'})
    differ = _Stores({'NM_1.1:c.4G>A': 'ga4gh:VA.x', 'NM_1.2:c.4G>A': 'ga4gh:VA.y'})
    assert _updated(same, 'NM_1.1:c.4G>A') == ['NM_1.1:c.4G>A', 'NM_1.2:c.4G>A']
    assert _updated(differ, 'NM_1.1:c.4G>A') == ['NM_1.1:c.4G>A']


def test_an_intronic_change_needs_a_projection() -> None:
    assert _updated(_Stores(), 'NM_1.1:c.4+1G>A') == ['NM_1.1:c.4+1G>A']


def test_nothing_is_added_on_the_newest_or_beside_it() -> None:
    assert _updated(_Stores(), 'NP_1.2:p.Glu4Lys') == ['NP_1.2:p.Glu4Lys']
    assert _updated(_Stores(), 'NP_1.1:p.Glu4Lys', 'NP_1.2:p.Glu4Lys') == ['NP_1.1:p.Glu4Lys', 'NP_1.2:p.Glu4Lys']


def test_the_moved_change_keeps_its_rewrites_and_is_dropped_as_derived() -> None:
    candidate = variants_pb2.Candidate(hgvs='NP_1.1:p.Glu4Lys')
    candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_NOTATION)
    mention_set = variants_pb2.MentionSet(mentions=[variants_pb2.Mention(verbatim='E4K', candidates=[candidate])])
    out = variant_versions.update(mention_set, cast(variant_reference.References, _Stores()))
    moved = out.mentions[0].candidates[1]
    assert [r.kind for r in moved.rewrites] == [
        variants_pb2.REWRITE_KIND_NOTATION,
        variants_pb2.REWRITE_KIND_VERSION_UPDATE,
    ]
    assert moved.rewrites[1].source == 'NP_1.1:p.Glu4Lys'
    assert variant_fit.without_derived(out) == mention_set


def test_a_change_without_a_position_stays() -> None:
    assert _updated(_Stores(), 'NP_1.1:p.(?)') == ['NP_1.1:p.(?)']


def test_a_change_past_the_end_of_both_versions_stays() -> None:
    assert _updated(_Stores(), 'NP_1.1:p.Glu900Lys') == ['NP_1.1:p.Glu900Lys']


def test_a_change_near_the_end_of_versions_that_end_alike_moves() -> None:
    assert _updated(_Stores(), 'NP_1.1:p.Lys60Pro') == ['NP_1.1:p.Lys60Pro', 'NP_1.2:p.Lys60Pro']


def test_two_older_versions_of_one_change_gain_one_update() -> None:
    assert _updated(_Stores(versions=3), 'NP_1.0:p.Glu4Lys', 'NP_1.1:p.Glu4Lys') == [
        'NP_1.0:p.Glu4Lys',
        'NP_1.1:p.Glu4Lys',
        'NP_1.2:p.Glu4Lys',
    ]


def test_an_allele_of_several_changes_stays() -> None:
    assert _updated(_Stores(), 'NP_1.1:p.[Glu4Lys;Lys9Glu]') == ['NP_1.1:p.[Glu4Lys;Lys9Glu]']


def test_a_coding_change_on_a_protein_stays() -> None:
    stores = _Stores()
    stores.cds_start = _no_cds  # type: ignore[method-assign]
    assert _updated(stores, 'NP_1.1:c.4G>A') == ['NP_1.1:c.4G>A']


def test_a_utr_position_the_other_version_lacks_is_not_equivalent() -> None:
    references = cast(variant_reference.References, _Stores())
    assert variant_versions.equivalent('NM_1.2', 'NM_1.1', 'c.-5G>A', references)
    assert not variant_versions.equivalent('NM_1.2', 'NM_1.1', 'c.-14T>C', references)


def test_a_derived_candidate_is_refused() -> None:
    candidate = variants_pb2.Candidate(hgvs='NP_1.1:p.Glu4Lys')
    candidate.rewrites.add(kind=variants_pb2.REWRITE_KIND_TRANSCRIPT_FIT)
    mention_set = variants_pb2.MentionSet(mentions=[variants_pb2.Mention(verbatim='E4K', candidates=[candidate])])
    with pytest.raises(ValueError, match='derived'):
        variant_versions.update(mention_set, cast(variant_reference.References, _Stores()))


def _no_cds(accession: str) -> int:
    raise weaver.DataProviderError(f'{accession} is a protein')
