"""LiveBackend composition: the exon-relevance signal assembly, and the located exon table.

Every upstream client function is replaced with a canned Result — the structure leg over the recorded
NF1 exon table — so no test here touches the network. The focus is what the backend adds over the
clients: the pext profile, the transcript inventory, and the census stamped beside the P/LP density.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
from collections.abc import Awaitable, Callable, Sequence

import httpx
import pytest

from themis.rpc import transcript_pb2
from themis.services.evidence import errors, hgvs
from themis.services.evidence.transcript import backend as transcript_backend
from themis.services.evidence.upstreams import clinvar, gnomad, gtex, transcript_structure

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_NF1_TRANSCRIPT = 'NM_001042492.3'
# The assessed exon every inventory test is written against: exon 10 of the queried transcript.
_ASSESSED = (100, 200)


def _returns[T](value: T) -> Callable[..., Awaitable[T]]:
    """An async stand-in for an upstream client function that ignores its args and returns `value`."""

    async def fake(*_args: object, **_kwargs: object) -> T:
        return value

    return fake


def _run[T](call: Callable[[transcript_backend.LiveBackend], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx.AsyncClient() as client:
            return await call(transcript_backend.LiveBackend(client))

    return asyncio.run(run())


def _clinvar_record(clinvar_id: str, title: str) -> clinvar.ClinvarRecordData:
    """One pool record, its coding span read off ``title`` exactly as the adapter reads it."""
    return clinvar.ClinvarRecordData(
        clinvar_id=clinvar_id,
        hgvs=title,
        classification='Pathogenic',
        review_stars=2,
        review_status='criteria provided, multiple submitters, no conflicts',
        conditions=['condition'],
        coding_span=hgvs.coding_span(title),
        submissions=[],
    )


def _gene_pool(records: list[clinvar.ClinvarRecordData], *, total: int, considered: int) -> clinvar.ClinvarGenePool:
    return clinvar.ClinvarGenePool(
        records=records,
        total=total,
        considered=considered,
        source='NCBI ClinVar (E-utilities)',
        dataset_versions=('clinvar',),
        query='q',
    )


def _fake_gtex(captured: dict[str, object]) -> Callable[..., Awaitable[gtex.GtexResult]]:
    """A GTEx stand-in recording the symbol and tissue filter it was called with."""

    async def fake(gene_symbol: str, *, tissues: Sequence[str] = (), **_kwargs: object) -> gtex.GtexResult:
        captured['gene_symbol'] = gene_symbol
        captured['tissues'] = list(tissues)
        return gtex.GtexResult(
            transcript_ids=['ENST00000352993.7'],
            medians=[gtex.TissueMedian('ENST00000352993.7', 'Liver', 0.67)],
            tissues_without_rows=['Nerve_Tibial'] if tissues else [],
            rows=[
                {'transcriptId': 'ENST00000352993.7', 'tissueSiteDetailId': 'Liver', 'median': 0.67},
                {'transcriptId': 'ENST00000352993.7', 'tissueSiteDetailId': 'Lung', 'median': 0.02},
            ],
            source='GTEx',
            dataset_versions=('gtex_v10',),
            query='q',
        )

    return fake


def _gene_signals(*regions: gnomad.PextRegion) -> gnomad.GnomadGeneResult:
    """A gnomAD gene stand-in; without regions, the two the composition test's exon straddles."""
    return gnomad.GnomadGeneResult(
        loeuf=0.25,
        mane_select=gnomad.ManeSelectPair(refseq='NM_007294.4', ensembl='ENST00000357654.9'),
        pext_regions=list(regions)
        or [gnomad.PextRegion(100, 200, 0.9, tissues={}), gnomad.PextRegion(300, 400, 0.4, tissues={})],
        raw={'symbol': 'BRCA1', 'gene_id': 'ENSG00000012048'},
        source='gnomAD GraphQL',
        dataset_versions=('gnomad_r4',),
        query='q',
    )


def _fake_structure(*exons: tuple[int, int, int]) -> Callable[..., Awaitable[object]]:
    """A transcript-structure stand-in carrying just the exon genomic spans pext selection reads."""
    return _returns(
        transcript_structure.TranscriptStructureResult(
            transcript='NM_000000.1',
            gene='BRCA1',
            chromosome_accession='NC_000017.11',
            strand=1,
            mane_select=True,
            mane_plus_clinical=False,
            transcript_length=1000,
            cds_transcript_start=1,
            cds_transcript_end=999,
            exons=[
                transcript_pb2.Exon(number=number, genomic_start=start, genomic_end=stop)
                for number, start, stop in exons
            ],
            exon_cigars={},
            raw={},
            source='VariantValidator gene2transcripts',
            dataset_versions=('vvta_2025_02',),
            query='q',
        )
    )


def _gene_transcript(
    accession: str,
    *exons: tuple[int, int],
    mane_select: bool = False,
    coding: bool = True,
    chromosome_accession: str = 'NC_000017.11',
) -> transcript_structure.GeneTranscript:
    """One inventory record: its identity and the genomic spans a membership verdict is read off."""
    return transcript_structure.GeneTranscript(
        accession=accession,
        mane_select=mane_select,
        mane_plus_clinical=False,
        coding=coding,
        alignments={chromosome_accession: [transcript_structure.ExonSpan(start, end) for start, end in exons]},
    )


def _annotation_set(
    annotation_set: str,
    transcripts: Sequence[transcript_structure.GeneTranscript],
    *,
    unreadable: Sequence[str] = (),
) -> transcript_structure.GeneTranscriptsResult:
    return transcript_structure.GeneTranscriptsResult(
        gene='BRCA1',
        annotation_set=annotation_set,
        transcripts=list(transcripts),
        unreadable=list(unreadable),
        source='VariantValidator gene2transcripts',
        dataset_versions=('vvta_2025_02',),
        query=f'gene2transcripts_v2/BRCA1/all/{annotation_set}/GRCh38',
    )


def _fake_gene_transcripts(
    *annotation_sets: transcript_structure.GeneTranscriptsResult,
) -> Callable[..., Awaitable[list[transcript_structure.GeneTranscriptsResult]]]:
    """A gene-transcripts stand-in; without arguments, one transcript per set carrying exon 100-200."""
    return _returns(
        list(annotation_sets)
        or [
            _annotation_set('refseq', [_gene_transcript('NM_000000.1', _ASSESSED, mane_select=True)]),
            _annotation_set('ensembl', [_gene_transcript('ENST00000352993.7', _ASSESSED)]),
        ]
    )


def _seed_exon_relevance_upstreams(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stand in for the gnomAD and ClinVar legs of exon-relevance; return the GTEx call record."""
    monkeypatch.setattr(
        gnomad,
        'fetch_gnomad_gene',
        _returns(
            gnomad.GnomadGeneResult(
                loeuf=0.25,
                mane_select=None,
                pext_regions=[],
                raw={},
                source='gnomAD GraphQL',
                dataset_versions=('gnomad_r4',),
                query='q',
            )
        ),
    )
    monkeypatch.setattr(clinvar, 'fetch_gene_pool', _returns(_gene_pool([], total=0, considered=0)))
    monkeypatch.setattr(transcript_structure, 'fetch_transcript_structure', _fake_structure((10, *_ASSESSED)))
    monkeypatch.setattr(transcript_structure, 'fetch_gene_transcripts', _fake_gene_transcripts())
    captured: dict[str, object] = {}
    monkeypatch.setattr(gtex, 'fetch_gtex_by_symbol', _fake_gtex(captured))
    return captured


def test_exon_relevance_composes_gnomad_clinvar_and_gtex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gnomad, 'fetch_gnomad_gene', _returns(_gene_signals()))
    monkeypatch.setattr(transcript_structure, 'fetch_transcript_structure', _fake_structure((10, 150, 350)))
    monkeypatch.setattr(transcript_structure, 'fetch_gene_transcripts', _fake_gene_transcripts())
    captured: dict[str, object] = {}

    async def fake_gene_pool(gene: str, **_kwargs: object) -> clinvar.ClinvarGenePool:
        captured['pool_gene'] = gene
        return _gene_pool(
            [_clinvar_record(f'VCV{i}', f'NM_007294.4(BRCA1):c.{i + 1}G>A') for i in range(3)],
            total=9,
            considered=5,
        )

    monkeypatch.setattr(clinvar, 'fetch_gene_pool', fake_gene_pool)
    monkeypatch.setattr(gtex, 'fetch_gtex_by_symbol', _fake_gtex(captured))

    signals = _run(
        lambda be: be.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(
                gene='BRCA1', transcript='NM_007294.4', exon=10, in_mane_select=True
            )
        )
    )
    assert captured['gene_symbol'] == 'BRCA1'
    # The density is a gene-level pool, so the pool lookup takes the gene — never the transcript.
    assert captured['pool_gene'] == 'BRCA1'
    assert captured['tissues'] == []
    assert signals.in_mane_select
    assert not signals.in_mane_plus_clinical
    assert signals.HasField('loeuf')
    assert signals.loeuf == 0.25
    assert signals.clinvar_plp_density == 3
    # One per contributing source, and one VariantValidator call per exon table read: the assessed
    # transcript's, then a gene-wide one per annotation set.
    assert [p.source for p in signals.provenance] == [
        'gnomAD GraphQL',
        'NCBI ClinVar (E-utilities)',
        'GTEx',
        'VariantValidator gene2transcripts',
        'VariantValidator gene2transcripts',
        'VariantValidator gene2transcripts',
    ]
    assert [p.query for p in signals.provenance[-2:]] == [
        'gene2transcripts_v2/BRCA1/all/refseq/GRCh38',
        'gene2transcripts_v2/BRCA1/all/ensembl/GRCh38',
    ]
    # The MANE Select the pext profile is computed against, in both namespaces, as gnomAD states it.
    assert signals.pext_mane_select.refseq == 'NM_007294.4'
    assert signals.pext_mane_select.ensembl == 'ENST00000357654.9'
    # pext is the covering regions' values weighted by coverage: exon 150-350 takes 51 nt from each
    # region, so the two means average.
    assert [entry.exon for entry in signals.pext] == [10]
    assert signals.pext[0].HasField('mean')
    assert signals.pext[0].mean == pytest.approx((0.9 + 0.4) / 2)
    # No tissue was requested, so the cross-tissue mean is all there is — and nothing is reported
    # missing, which would read as "gnomAD has no such tissue".
    assert not signals.pext[0].tissues
    assert not signals.tissues_without_pext
    assert signals.raw['clinvar_plp_density_scope'] == 'gene'
    # A capped pool makes the density a lower bound; the census says so rather than leaving it implied.
    assert signals.raw['clinvar_total_in_gene'] == 9
    assert signals.raw['clinvar_considered_in_gene'] == 5
    assert signals.raw['clinvar_pool_truncated'] is True
    assert len(signals.raw.fields['pext_regions'].list_value.values) == 2
    assert [(e.transcript, e.tissue, e.median_tpm) for e in signals.gtex_expression] == [
        ('ENST00000352993.7', 'Liver', 0.67)
    ]


@pytest.mark.parametrize(
    'failure',
    [
        errors.UnknownVariantError('ClinVar holds no record'),
        errors.InvalidRequestError('ClinVar rejected the term'),
        ValueError('ClinVar esearch returned no count'),
        httpx.HTTPStatusError('502', request=httpx.Request('GET', 'https://e.utils'), response=httpx.Response(502)),
    ],
)
def test_exon_relevance_never_reports_a_density_it_could_not_compute(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """`clinvar_plp_density` carries no presence, so 0 has to be a count and cannot mean anything else.

    That holds only while every way the pool lookup can fail takes the rpc down with it: absorbing
    one would ship a 0 the caller is obliged to read as "no star-passing P/LP record in the gene".
    """
    _seed_exon_relevance_upstreams(monkeypatch)

    async def failing_gene_pool(*_args: object, **_kwargs: object) -> clinvar.ClinvarGenePool:
        raise failure

    monkeypatch.setattr(clinvar, 'fetch_gene_pool', failing_gene_pool)

    with pytest.raises(type(failure)):
        _run(
            lambda be: be.assess_exon_relevance(
                transcript_pb2.AssessExonRelevanceRequest(gene='BRCA1', transcript='NM_007294.4', exon=10)
            )
        )


# The ANO5 shape, at the resolution the profile has to preserve: a gene expressed alike everywhere,
# and one exon that drops out across tissues while holding up in skeletal muscle.
_MUSCLE = 'Muscle_Skeletal'
_BRAIN = 'Brain_Cortex'
_GENE_WIDE = gnomad.PextRegion(100, 200, 0.83, tissues={'muscle_skeletal': 0.94, 'brain_cortex': 0.81})
_EXCLUDED = gnomad.PextRegion(300, 400, 0.03, tissues={'muscle_skeletal': 0.59, 'brain_cortex': 0.0})


def _exon_relevance_over_pext(
    monkeypatch: pytest.MonkeyPatch, *, tissues: list[str]
) -> transcript_pb2.AssessExonRelevanceResponse:
    """Exon-relevance over a three-exon transcript whose middle exon sits in the depressed region."""
    _seed_exon_relevance_upstreams(monkeypatch)
    monkeypatch.setattr(gnomad, 'fetch_gnomad_gene', _returns(_gene_signals(_GENE_WIDE, _EXCLUDED)))
    monkeypatch.setattr(
        transcript_structure, 'fetch_transcript_structure', _fake_structure((1, 100, 150), (2, 300, 400), (3, 151, 200))
    )
    return _run(
        lambda be: be.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(gene='ANO5', transcript='NM_007294.4', exon=2, tissues=tissues)
        )
    )


def test_exon_relevance_returns_the_whole_gene_pext_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The queried exon's value means nothing on its own.

    SM18 asks whether the exon is excluded *relative to* the rest of the gene, and a gene depressed
    throughout reads like an excluded exon without that comparison.
    """
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[])

    profile = {entry.exon: entry.mean for entry in signals.pext}
    assert profile.keys() == {1, 2, 3}  # every exon of the queried transcript, not just exon 2
    assert profile[2] < profile[1] == profile[3]


def test_exon_relevance_reports_pext_per_requested_tissue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cross-tissue mean condemns an exon that holds up in the disease-relevant tissue."""
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[_MUSCLE, _BRAIN])

    by_exon = {entry.exon: {value.tissue: value.value for value in entry.tissues} for entry in signals.pext}
    queried, elsewhere = by_exon[2], by_exon[1]
    # Across tissues the exon looks excluded; in muscle it holds most of the gene's own level.
    assert signals.pext[1].mean / signals.pext[0].mean < 0.1
    assert queried[_MUSCLE] / elsewhere[_MUSCLE] > 0.5
    assert queried[_BRAIN] < elsewhere[_BRAIN]
    assert not signals.tissues_without_pext


def test_exon_relevance_names_a_requested_tissue_gnomad_has_no_pext_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """GTEx samples 54 tissues, gnomAD's pext carries 49: the gap is stated, never a silent 0."""
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[_MUSCLE, 'Bladder'])

    assert [value.tissue for value in signals.pext[1].tissues] == [_MUSCLE]
    assert list(signals.tissues_without_pext) == ['Bladder']


def test_exon_relevance_says_how_much_of_each_exon_pext_covers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pext is published over coding bases, so a UTR-bearing exon is measured on part of itself.

    Without the span the profile compares a terminal exon read over a fraction of its length against
    a fully covered one as if the two measurements were alike.
    """
    signals = _exon_relevance_over_pext(monkeypatch, tissues=[])

    covered = {entry.exon: (entry.covered_bases, entry.exon_bases) for entry in signals.pext}
    # Exon 1 (100-150) sits inside the 100-200 region; exon 2 (300-400) fills the 300-400 one.
    assert covered[1] == (51, 51)
    assert covered[2] == (101, 101)


def test_an_exon_no_region_covers_has_no_pext_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reporting 0.0 there would read as "not expressed"; the exon still appears in the profile."""
    _seed_exon_relevance_upstreams(monkeypatch)
    monkeypatch.setattr(gnomad, 'fetch_gnomad_gene', _returns(_gene_signals(_GENE_WIDE)))
    monkeypatch.setattr(transcript_structure, 'fetch_transcript_structure', _fake_structure((1, 9000, 9100)))
    signals = _run(
        lambda be: be.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(gene='ANO5', transcript='NM_007294.4', exon=1)
        )
    )
    assert [entry.exon for entry in signals.pext] == [1]
    assert not signals.pext[0].HasField('mean')
    assert signals.pext[0].covered_bases == 0
    assert signals.pext[0].exon_bases == 101


def test_a_gene_without_pext_reports_no_missing_tissue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gene gnomAD holds no pext for is every exon's unset mean, not 49 tissues gone missing."""
    _seed_exon_relevance_upstreams(monkeypatch)  # seeds a gene with no pext regions
    signals = _run(
        lambda be: be.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(
                gene='BRCA1', transcript='NM_007294.4', exon=10, tissues=[_MUSCLE, 'Bladder']
            )
        )
    )
    assert not signals.pext[0].HasField('mean')
    assert not signals.tissues_without_pext


def test_exon_relevance_rejects_an_exon_the_transcript_does_not_have(monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_exon_relevance_upstreams(monkeypatch)
    with pytest.raises(errors.InvalidRequestError, match='asked for exon 99'):
        _run(
            lambda be: be.assess_exon_relevance(
                transcript_pb2.AssessExonRelevanceRequest(gene='BRCA1', transcript='NM_007294.4', exon=99)
            )
        )


def test_exon_relevance_withholds_the_gtex_grid_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default response states what it left out, and keeps the signal the omitted rows carried."""
    _seed_exon_relevance_upstreams(monkeypatch)
    signals = _run(
        lambda be: be.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(gene='BRCA1', transcript='NM_007294.4', exon=10)
        )
    )
    isoforms = signals.raw.fields['gtex_isoforms'].struct_value
    assert 'rows' not in isoforms.fields
    assert isoforms.fields['rows_withheld'].number_value == 2
    assert [e.tissue for e in signals.gtex_expression] == ['Liver']


def test_exon_relevance_passes_the_tissue_filter_and_serves_the_grid_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _seed_exon_relevance_upstreams(monkeypatch)
    signals = _run(
        lambda be: be.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(
                gene='BRCA1', transcript='NM_007294.4', exon=10, tissues=['Liver'], include_gtex_detail=True
            )
        )
    )
    assert captured['tissues'] == ['Liver']
    isoforms = signals.raw.fields['gtex_isoforms'].struct_value
    assert len(isoforms.fields['rows'].list_value.values) == 2
    # A requested tissue GTEx returned nothing for is named on the typed surface, where a caller
    # reading `gtex_expression` will see it, rather than only in raw where it can read as "not
    # expressed" — the same treatment the pext side of the request gets.
    assert list(signals.tissues_without_expression) == ['Nerve_Tibial']
    empty = [value.string_value for value in isoforms.fields['tissues_without_rows'].list_value.values]
    assert empty == ['Nerve_Tibial']


def _inventory(
    monkeypatch: pytest.MonkeyPatch,
    refseq: Sequence[transcript_structure.GeneTranscript],
    ensembl: Sequence[transcript_structure.GeneTranscript],
    *,
    unreadable: Sequence[str] = (),
    isoforms: gtex.GtexResult | None = None,
) -> transcript_pb2.AssessExonRelevanceResponse:
    """Exon-relevance over an inventory the test states, against exon 10 at `_ASSESSED`."""
    _seed_exon_relevance_upstreams(monkeypatch)
    monkeypatch.setattr(
        transcript_structure,
        'fetch_gene_transcripts',
        _fake_gene_transcripts(
            _annotation_set('refseq', refseq, unreadable=unreadable), _annotation_set('ensembl', ensembl)
        ),
    )
    if isoforms is not None:
        monkeypatch.setattr(gtex, 'fetch_gtex_by_symbol', _returns(isoforms))
    return _run(
        lambda be: be.assess_exon_relevance(
            transcript_pb2.AssessExonRelevanceRequest(
                gene='BRCA1', transcript='NM_000000.1', exon=10, in_mane_select=True
            )
        )
    )


def _by_membership(
    signals: transcript_pb2.AssessExonRelevanceResponse,
) -> dict[transcript_pb2.ExonMembership, list[str]]:
    grouped: dict[transcript_pb2.ExonMembership, list[str]] = {}
    for entry in signals.transcript_inventory:
        grouped.setdefault(entry.membership, []).append(entry.transcript)
    return grouped


def _gtex(*rows: tuple[str, str, float]) -> gtex.GtexResult:
    return gtex.GtexResult(
        transcript_ids=list(dict.fromkeys(transcript for transcript, _, _ in rows)),
        medians=[gtex.TissueMedian(transcript, tissue, median) for transcript, tissue, median in rows],
        tissues_without_rows=[],
        rows=[],
        source='GTEx',
        dataset_versions=('gtex_v10',),
        query='q',
    )


def test_an_inventory_with_no_omitting_transcript_states_its_own_denominator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape that leaves "Most" nothing to be measured against, read off typed fields alone.

    Without the denominator beside it, "no transcript omits the exon" and "no transcript was
    classified" are the same response.
    """
    signals = _inventory(
        monkeypatch,
        [
            _gene_transcript('NM_000000.1', (50, 60), _ASSESSED, (400, 500), mane_select=True),
            _gene_transcript('NM_000001.1', _ASSESSED, (400, 500)),
            _gene_transcript('NM_000002.2', (50, 60), _ASSESSED),
        ],
        [_gene_transcript('ENST00000352993.7', (50, 60), _ASSESSED, (400, 500))],
    )

    assert set(_by_membership(signals)) == {transcript_pb2.EXON_MEMBERSHIP_CARRIES_THE_EXON}
    assert [(d.namespace, d.transcripts_considered) for d in signals.inventory_denominators] == [
        (transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ, 3),
        (transcript_pb2.TRANSCRIPT_NAMESPACE_ENSEMBL, 1),
    ]
    # Each entry carries the namespace its denominator is stated under: the proto tells the caller to
    # group on it, so a mis-stamped entry mis-sizes the group it is read against.
    assert {e.transcript: e.namespace for e in signals.transcript_inventory} == {
        'NM_000000.1': transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ,
        'NM_000001.1': transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ,
        'NM_000002.2': transcript_pb2.TRANSCRIPT_NAMESPACE_REFSEQ,
        'ENST00000352993.7': transcript_pb2.TRANSCRIPT_NAMESPACE_ENSEMBL,
    }
    # The requested accession's own entry is marked, so its verdict is checkable against the rest.
    assessed = [entry.transcript for entry in signals.transcript_inventory if entry.assessed_transcript]
    assert assessed == ['NM_000000.1']


def test_the_three_ways_of_lacking_the_exon_stay_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only one of them is a transcript omitting an exon it reaches.

    Collapsing them into a carries/omits pair would report an alternative acceptor and a transcript
    that ends short of the locus as evidence of exclusion, which is the reading that decides the
    tier.
    """
    signals = _inventory(
        monkeypatch,
        [
            _gene_transcript('NM_000000.1', (50, 60), _ASSESSED, (400, 500), mane_select=True),
            # Spans the locus, splices past it: the omission "All" is defined against.
            _gene_transcript('NM_000001.1', (50, 60), (400, 500)),
            # An alternative acceptor: overlapping, but not the exon as annotated.
            _gene_transcript('NM_000002.2', (50, 60), (150, 200), (400, 500)),
            # An exon strictly containing the assessed interval — an alternative acceptor AND donor.
            _gene_transcript('NM_000003.3', (50, 60), (80, 260)),
            # Two exons of its own across the assessed interval.
            _gene_transcript('NM_000004.4', (90, 140), (160, 210)),
            # Ends before the locus, so it never had the chance to splice past it.
            _gene_transcript('NM_000005.5', (50, 60), (70, 80)),
            # Begins after it — the other half of the same outcome.
            _gene_transcript('NM_000006.6', (400, 500), (600, 700)),
        ],
        [_gene_transcript('ENST00000352993.7', _ASSESSED)],
    )

    assert _by_membership(signals) == {
        transcript_pb2.EXON_MEMBERSHIP_CARRIES_THE_EXON: ['NM_000000.1', 'ENST00000352993.7'],
        transcript_pb2.EXON_MEMBERSHIP_SPANS_BUT_SKIPS: ['NM_000001.1'],
        transcript_pb2.EXON_MEMBERSHIP_CARRIES_A_DIFFERENT_INTERVAL: ['NM_000002.2', 'NM_000003.3', 'NM_000004.4'],
        transcript_pb2.EXON_MEMBERSHIP_DOES_NOT_REACH: ['NM_000005.5', 'NM_000006.6'],
    }


def test_a_different_interval_reports_how_much_of_the_exon_the_transcript_keeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One bucket spans a 3-nt alternative acceptor and a transcript keeping one base of the exon.

    Which of those is still "functionally equivalent" is SM18's judgement, so the interval is
    returned and the verdict is left un-graded.
    """
    signals = _inventory(
        monkeypatch,
        [
            _gene_transcript('NM_000000.1', _ASSESSED, mane_select=True),
            _gene_transcript('NM_000001.1', (103, 200)),
            _gene_transcript('NM_000002.2', (90, 100)),
            _gene_transcript('NM_000003.3', (90, 140), (160, 210)),
        ],
        [],
    )

    overlaps = {e.transcript: [(s.start, s.end) for s in e.overlapping_exons] for e in signals.transcript_inventory}
    assert overlaps['NM_000001.1'] == [(103, 200)]  # loses 3 nt at the acceptor
    assert overlaps['NM_000002.2'] == [(90, 100)]  # keeps one base of it
    assert overlaps['NM_000003.3'] == [(90, 140), (160, 210)]  # splits it in two
    # Only the different-interval bucket carries them: the exact carrier's interval is the assessed
    # one, and the other two buckets overlap it nowhere.
    assert overlaps['NM_000000.1'] == []


def test_no_mane_pair_is_an_absence_not_an_empty_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pext_mane_select` is the only stated RefSeq/Ensembl route, so its absence has to read as one."""
    signals = _inventory(monkeypatch, [_gene_transcript('NM_000000.1', _ASSESSED, mane_select=True)], [])

    assert not signals.HasField('pext_mane_select')


def test_expression_joins_a_transcript_the_two_releases_version_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GTEx's GENCODE snapshot and the alignment release disagree about versions on real transcripts.

    An exact-string join drops those, which reads as "GTEx measures no expression for this isoform" —
    an absence, over a transcript that is in fact measured. So the join runs on the unversioned base
    and both versions stay on the response rather than one being reconciled away.
    """
    signals = _inventory(
        monkeypatch,
        [_gene_transcript('NM_000000.1', _ASSESSED, mane_select=True)],
        [_gene_transcript('ENST00000352993.9', _ASSESSED)],
        isoforms=_gtex(('ENST00000352993.7', 'Liver', 0.67)),
    )

    joined = next(e for e in signals.transcript_inventory if e.accession_base == 'ENST00000352993')
    assert joined.transcript == 'ENST00000352993.9'  # the version the exon spans were read on
    assert [(row.transcript, row.tissue, row.median_tpm) for row in joined.expression] == [
        ('ENST00000352993.7', 'Liver', 0.67)  # the version GTEx measured
    ]
    assert [row.transcript_base for row in joined.expression] == ['ENST00000352993']
    assert not signals.transcripts_without_structure
    assert not signals.transcripts_without_expression


def test_one_measurement_is_not_reported_against_two_versions_of_its_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row belongs to one transcript; attaching it to each record sharing the base would double it."""
    signals = _inventory(
        monkeypatch,
        [_gene_transcript('NM_000000.1', _ASSESSED, mane_select=True)],
        [
            _gene_transcript('ENST00000352993.7', _ASSESSED),
            _gene_transcript('ENST00000352993.9', (400, 500)),
        ],
        isoforms=_gtex(('ENST00000352993.7', 'Liver', 0.67)),
    )

    carrying = {e.transcript: [row.median_tpm for row in e.expression] for e in signals.transcript_inventory}
    assert carrying['ENST00000352993.7'] == [0.67]
    assert carrying['ENST00000352993.9'] == []
    assert list(signals.transcripts_without_expression) == ['ENST00000352993.9']


def test_a_transcript_only_one_side_holds_is_named_on_the_side_that_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coverage is partial in both directions, and neither gap may read as a measurement."""
    signals = _inventory(
        monkeypatch,
        [_gene_transcript('NM_000000.1', _ASSESSED, mane_select=True)],
        [_gene_transcript('ENST00000352993.7', _ASSESSED), _gene_transcript('ENST00000999999.1', _ASSESSED)],
    )

    # GTEx measures ENST…993 alone, so the other Ensembl model's abundance is unmeasured, not zero.
    # The RefSeq entry is absent from the list by namespace, not by having a row: GTEx keys no
    # RefSeq accession, so there is no row of its own for it to be missing.
    assert list(signals.transcripts_without_expression) == ['ENST00000999999.1']
    assert not signals.transcripts_without_structure


def test_expression_over_a_transcript_no_entry_covers_stands_against_no_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals = _inventory(
        monkeypatch,
        [_gene_transcript('NM_000000.1', _ASSESSED, mane_select=True)],
        [],
        isoforms=_gtex(('ENST00000683148.1', 'Liver', 4.2)),
    )

    assert list(signals.transcripts_without_structure) == ['ENST00000683148.1']
    assert [e.transcript for e in signals.transcript_inventory] == ['NM_000000.1']
    # The abundance is still returned gene-wide; what it lacks is a membership verdict to sit beside.
    assert [e.transcript for e in signals.gtex_expression] == ['ENST00000683148.1']


def test_a_transcript_whose_record_was_not_classified_is_not_reported_as_unheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`transcripts_without_structure` says no entry covers it, not that no record exists."""
    signals = _inventory(
        monkeypatch,
        [_gene_transcript('NM_000000.1', _ASSESSED, mane_select=True)],
        [_gene_transcript('ENST00000352993.7', _ASSESSED, chromosome_accession='NC_000001.11')],
        isoforms=_gtex(('ENST00000352993.7', 'Liver', 0.67)),
    )

    assert list(signals.transcripts_without_structure) == ['ENST00000352993.7']
    ensembl = signals.inventory_denominators[1]
    assert ensembl.transcripts_considered == 0
    assert list(ensembl.transcripts_not_classified) == ['ENST00000352993.7']


def test_a_record_with_no_comparable_alignment_gets_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transcript with no interval to test is not a transcript that lacks the exon.

    Classifying it either way invents a fact; leaving it out of a denominator that does not mention
    it hides one.
    """
    signals = _inventory(
        monkeypatch,
        [
            _gene_transcript('NM_000000.1', _ASSESSED, mane_select=True),
            _gene_transcript('NM_000004.4', _ASSESSED, chromosome_accession='NC_000001.11'),
        ],
        [_gene_transcript('ENST00000352993.7', _ASSESSED)],
        unreadable=['NR_000005.1'],
    )

    assert [e.transcript for e in signals.transcript_inventory] == ['NM_000000.1', 'ENST00000352993.7']
    refseq = signals.inventory_denominators[0]
    assert refseq.transcripts_considered == 1
    assert set(refseq.transcripts_not_classified) == {'NR_000005.1', 'NM_000004.4'}


def test_a_gene_and_transcript_from_different_genes_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answering would return the named gene's transcripts, none of which reaches the assessed exon."""
    _seed_exon_relevance_upstreams(monkeypatch)
    monkeypatch.setattr(
        transcript_structure,
        'fetch_gene_transcripts',
        _returns(
            [
                dataclasses.replace(_annotation_set('refseq', []), gene='OTHER'),
                dataclasses.replace(_annotation_set('ensembl', []), gene='OTHER'),
            ]
        ),
    )

    with pytest.raises(errors.InvalidRequestError, match='OTHER'):
        _run(
            lambda be: be.assess_exon_relevance(
                transcript_pb2.AssessExonRelevanceRequest(gene='OTHER', transcript='NM_000000.1', exon=10)
            )
        )


def test_a_non_coding_model_is_reported_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whether a model counts toward "the gene's transcripts" is a curator's call, so it needs the fact."""
    signals = _inventory(
        monkeypatch,
        [
            _gene_transcript('NM_000000.1', _ASSESSED, mane_select=True),
            _gene_transcript('NR_000006.1', _ASSESSED, coding=False),
        ],
        [],
    )

    assert {e.transcript: e.coding for e in signals.transcript_inventory} == {'NM_000000.1': True, 'NR_000006.1': False}
    assert {e.transcript: e.mane_select for e in signals.transcript_inventory} == {
        'NM_000000.1': True,
        'NR_000006.1': False,
    }


def test_an_assessed_transcript_the_set_has_superseded_still_matches_on_its_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gene arms carry the CURRENT set; the request may name a version it no longer holds."""
    signals = _inventory(
        monkeypatch,
        [_gene_transcript('NM_000000.4', _ASSESSED, mane_select=True)],
        [],
    )

    assessed = [e for e in signals.transcript_inventory if e.assessed_transcript]
    assert [e.transcript for e in assessed] == ['NM_000000.4']  # requested .1, the set holds .4


def _nf1_structure() -> transcript_structure.TranscriptStructureResult:
    return transcript_structure.parse_transcript_structure(
        json.loads((_FIXTURES / 'transcript_structure.json').read_text()),
        transcript=_NF1_TRANSCRIPT,
        genome_build='GRCh38',
        dataset_versions=('vvta_2025_02',),
        query='gene2transcripts',
    )


def test_get_structure_locates_the_requested_position(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_structure, 'fetch_transcript_structure', _returns(_nf1_structure()))
    resp = _run(
        lambda be: be.get_structure(
            transcript_pb2.GetStructureRequest(transcript=_NF1_TRANSCRIPT, genome_build='GRCh38', cds_position=3496)
        )
    )
    assert resp.gene == 'NF1'
    assert resp.mane_select
    assert resp.chromosome_accession == 'NC_000017.11'
    assert len(resp.exons) == 58
    assert resp.position.exon == 26
    assert resp.position.nt_to_exon_end == 1
    assert resp.provenance[0].source == 'VariantValidator gene2transcripts'
    assert resp.provenance[0].retrieved_at.seconds > 0


def test_get_structure_omits_the_position_when_none_was_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_structure, 'fetch_transcript_structure', _returns(_nf1_structure()))
    resp = _run(
        lambda be: be.get_structure(
            transcript_pb2.GetStructureRequest(transcript=_NF1_TRANSCRIPT, genome_build='GRCh38')
        )
    )
    assert not resp.HasField('position')
