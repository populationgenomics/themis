"""Where each upstream's HTTP failures land on the evidence taxonomy.

The property spans the adapters rather than sitting in any one of them, and it is what the guest's
retry helper keys on. ``errors.InvalidRequestError`` (INVALID_ARGUMENT), ``errors.UnknownVariantError``
(NOT_FOUND) and ``errors.InconsistentSourcesError`` (FAILED_PRECONDITION) are settled and are never
retried; ``httpx2.HTTPStatusError`` surfaces as UNKNOWN, which is reissued four times with backoff. A
failure in the wrong bucket therefore either burns four calls on a verdict that cannot change, or
states a settled fact about a fault a single retry would have cleared. That every one of those types
reaches a status of its own is ``tests/test_serving.py``'s.

Only one half is universal: every upstream must keep 429 and 5xx retryable. What a 4xx means is a
per-upstream fact, so each adapter sits in exactly one of three buckets — refuses on any 4xx, refuses
on one specific status, or has no refusing status at all. Two tests keep that honest: one that the
buckets partition the exercised entry points, and one that those entry points are *all* the client
fetchers the adapter modules expose. The shared rule itself is tested in ``tests/test_errors.py``;
this file is about which adapter is wired to it, and how.

One status can also be neither a refusal nor a fault but an *answer*: CSpec's gene table replies to a
symbol it has no entry for with a 404, and that reply is the rpc's coverage finding. Such a status is
carved out of its adapter's bucket in ``_ANSWERED_4XX``, and asserted to return rather than raise —
reading it as a refusal would report a symbol the registry simply does not carry as a bad request.

An upstream that reports a variant-level verdict inside a 200 is not covered here at all — that is a
payload question, and it lives with the adapter that reads the payload (``test_gnomad``,
``test_spliceai``).
"""

from __future__ import annotations

import asyncio
import inspect
import types
from collections.abc import Awaitable, Callable

import httpx2
import pytest

from themis.rpc import cspec_pb2
from themis.services.evidence import errors
from themis.services.evidence.upstreams import (
    allele_registry,
    clingen_dosage,
    clingen_validity,
    clinvar,
    cspec,
    europe_pmc,
    gencc,
    gnomad,
    gtex,
    litvar,
    mavedb,
    panelapp,
    spliceai,
    transcript_sequence,
    transcript_structure,
    variant_validator,
    vep,
)

# The reference-table modules (clingen_dosage, clingen_validity, gencc, panelapp) parse bucket bytes
# and take no client, so they contribute nothing to `_client_fetchers`. Listed anyway: a live fetch
# reintroduced into one lands in the derived set and has to be classified.
_ADAPTERS = (
    allele_registry,
    clingen_dosage,
    clingen_validity,
    clinvar,
    cspec,
    europe_pmc,
    gencc,
    gnomad,
    gtex,
    litvar,
    mavedb,
    panelapp,
    spliceai,
    transcript_sequence,
    transcript_structure,
    variant_validator,
    vep,
)

_VARIANT_HGVS = 'NM_001042492.3:c.3496G>C'
_TRANSCRIPT = 'NM_001042492.3'
_GNOMAD_ID = '17-31232881-G-C'

_ENTRY_POINTS: dict[str, Callable[[httpx2.AsyncClient], Awaitable[object]]] = {
    'allele_registry': lambda c: allele_registry.fetch_allele_registry(_VARIANT_HGVS, http_client=c),
    'allele_registry_ids': lambda c: allele_registry.fetch_clingen_allele_ids(_VARIANT_HGVS, http_client=c),
    'clinvar_archive': lambda c: clinvar.fetch_variant_archive('VCV001731988', http_client=c),
    'clinvar_pool': lambda c: clinvar.fetch_gene_pool('NF1', http_client=c, review_status_floor=1, limit=500),
    'clinvar_span': lambda c: clinvar.fetch_span_records('NF1', 31232881, 31232931, http_client=c, limit=500),
    'cspec': lambda c: cspec.fetch_criteria_specifications('NF1', http_client=c),
    'europe_pmc_search': lambda c: europe_pmc.search('NF1 variant', 10, http_client=c),
    'europe_pmc_records': lambda c: europe_pmc.records_by_pmid(['24789688'], http_client=c),
    'gnomad_variant': lambda c: gnomad.fetch_gnomad(_GNOMAD_ID, 'gnomad_r4', http_client=c),
    'gnomad_gene': lambda c: gnomad.fetch_gnomad_gene('NF1', http_client=c),
    'gtex': lambda c: gtex.fetch_gtex('ENSG00000196712.18', http_client=c),
    'gtex_by_symbol': lambda c: gtex.fetch_gtex_by_symbol('NF1', http_client=c),
    'litvar_autocomplete': lambda c: litvar.autocomplete_entity_ids('rs2229707', http_client=c),
    'litvar_entity': lambda c: litvar.entity_labels('litvar@rs2229707##', http_client=c),
    'litvar_search': lambda c: litvar.search_pmids('litvar@rs2229707##', 10, http_client=c),
    'litvar_gene': lambda c: litvar.gene_entities('NF1', http_client=c),
    'mavedb': lambda c: mavedb.fetch_mavedb(['CA398989536'], http_client=c),
    'spliceai': lambda c: spliceai.fetch_splice(_GNOMAD_ID, 'GRCh38', http_client=c),
    'transcript_sequence': lambda c: transcript_sequence.fetch_transcript_sequence(_TRANSCRIPT, http_client=c),
    'transcript_structure': lambda c: transcript_structure.fetch_transcript_structure(
        _TRANSCRIPT, 'GRCh38', http_client=c
    ),
    'gene_transcripts': lambda c: transcript_structure.fetch_gene_transcripts('NF1', 'GRCh38', http_client=c),
    'variant_validator': lambda c: variant_validator.fetch_variant_validator(
        'GRCh38', _VARIANT_HGVS, 'mane', http_client=c
    ),
    'vep': lambda c: vep.fetch_vep(_VARIANT_HGVS, [], 'GRCh38', http_client=c),
}

# Every 4xx a caller can provoke is the source refusing the request, and reads as INVALID_ARGUMENT.
_REFUSES_ANY_4XX = (
    'allele_registry',
    'allele_registry_ids',
    'clinvar_archive',
    'clinvar_pool',
    'clinvar_span',
    'cspec',
    'europe_pmc_search',
    'europe_pmc_records',
    'gtex',
    'gtex_by_symbol',
    'litvar_autocomplete',
    'litvar_entity',
    'litvar_search',
    'litvar_gene',
    'mavedb',
    'transcript_structure',
    'gene_transcripts',
    'variant_validator',
    'vep',
)

# NCBI efetch spells a refusal as "no record", and only on one status: it answers an accession it
# holds no sequence for with a 400, while its other 4xx are about the client (403 blocked, 429
# throttled) and stay retryable — asserted in test_transcript_sequence.py. The ClinVar adapter reads
# that same spelling off the refusal's own ERROR envelope as well as off the status, so a 400 alone
# does not classify it: `clinvar_archive` stays in `_REFUSES_ANY_4XX` because the body every call
# here carries is not one it recognises, and the branch that reads it — where the accession the
# crosswalk named resolves to nothing — is asserted in test_clinvar.py.
_STATUS_SPECIFIC: dict[str, dict[int, type[Exception]]] = {
    'transcript_sequence': {400: errors.UnknownVariantError},
}

# The statuses an adapter answers rather than refuses, carved out of its bucket above, each with what
# the answer has to be. CSpec's gene table holds every HGNC gene, so its 404 says the symbol is not
# one the registry carries — an answer about the registry's snapshot, and not the finding that no
# expert panel has specified the gene. LitVar2 spells "no such entity" as a 400, so reading it as a
# refusal would turn a fact about the index into an INVALID_ARGUMENT no caller can act on.
_ANSWERED_4XX: dict[str, dict[int, Callable[[object], bool]]] = {
    'cspec': {
        404: lambda answered: getattr(answered, 'coverage', None) == cspec_pb2.SPECIFICATION_COVERAGE_GENE_ABSENT
    },
    'litvar_entity': {400: lambda answered: answered is None},
}

# No status here means "the caller's request is unacceptable" — see the design doc's taxonomy note.
_EXEMPT = ('gnomad_variant', 'gnomad_gene', 'spliceai')


def _client_fetchers() -> set[str]:
    """Every public coroutine an adapter module exposes that issues a request on the shared client."""
    found: set[str] = set()
    for module in _ADAPTERS:
        name = module.__name__.rsplit('.', 1)[-1]
        for attribute, value in vars(module).items():
            if attribute.startswith('_') or isinstance(value, types.ModuleType):
                continue
            candidates = (
                [(f'{attribute}.{n}', m) for n, m in vars(value).items() if not n.startswith('_')]
                if inspect.isclass(value)
                else [(attribute, value)]
            )
            for qualname, candidate in candidates:
                function = candidate.__func__ if isinstance(candidate, classmethod) else candidate
                if not inspect.iscoroutinefunction(function):
                    continue
                if 'http_client' in inspect.signature(function).parameters:
                    found.add(f'{name}.{qualname}')
    return found


# The 4xx a caller can plausibly provoke, short of 429 (which is about the rate, never the request).
_CLIENT_ERRORS = (400, 403, 404, 422)


def _call(entry_point: str, status: int) -> object:
    """Drive one upstream entry point against a transport that answers every request with ``status``."""

    async def run() -> object:
        transport = httpx2.MockTransport(lambda _request: httpx2.Response(status, text='upstream said no'))
        async with httpx2.AsyncClient(transport=transport) as client:
            return await _ENTRY_POINTS[entry_point](client)

    return asyncio.run(run())


def test_every_entry_point_is_classified() -> None:
    """An adapter in none of the three buckets is silently untested for everything but 429/5xx."""
    assert set(_REFUSES_ANY_4XX) | set(_STATUS_SPECIFIC) | set(_EXEMPT) == set(_ENTRY_POINTS)


def test_every_client_fetcher_is_exercised_here() -> None:
    """Partitioning `_ENTRY_POINTS` proves nothing on its own — nothing forces an adapter into it.

    So the set is derived from the modules instead: a new fetcher that takes the shared client lands
    in neither table and fails here, rather than silently acquiring no coverage at all.
    """
    exercised = {
        'allele_registry.fetch_allele_registry',
        'allele_registry.fetch_clingen_allele_ids',
        'clinvar.fetch_gene_pool',
        'clinvar.fetch_span_records',
        'clinvar.fetch_variant_archive',
        'cspec.fetch_criteria_specifications',
        'europe_pmc.records_by_pmid',
        'europe_pmc.search',
        'gnomad.fetch_gnomad',
        'gnomad.fetch_gnomad_gene',
        'gtex.fetch_gtex',
        'gtex.fetch_gtex_by_symbol',
        'litvar.autocomplete_entity_ids',
        'litvar.entity_labels',
        'litvar.gene_entities',
        'litvar.search_pmids',
        'mavedb.fetch_mavedb',
        'spliceai.fetch_splice',
        'transcript_sequence.fetch_transcript_sequence',
        'transcript_structure.fetch_gene_transcripts',
        'transcript_structure.fetch_transcript_structure',
        'variant_validator.fetch_variant_validator',
        'vep.fetch_vep',
    }
    assert _client_fetchers() == exercised


@pytest.mark.parametrize('entry_point', _REFUSES_ANY_4XX)
@pytest.mark.parametrize('status', _CLIENT_ERRORS)
def test_a_refusal_reaches_the_caller_as_a_settled_answer(entry_point: str, status: int) -> None:
    if status in _ANSWERED_4XX.get(entry_point, {}):
        pytest.skip(f'{entry_point} answers {status} rather than refusing it')
    with pytest.raises(errors.InvalidRequestError):
        _call(entry_point, status)


@pytest.mark.parametrize(
    ('entry_point', 'status'),
    [(name, status) for name, statuses in _ANSWERED_4XX.items() for status in sorted(statuses)],
)
def test_an_answered_status_returns_rather_than_raising(entry_point: str, status: int) -> None:
    """Refusing it would report a subject the source simply does not carry as a bad request."""
    assert _ANSWERED_4XX[entry_point][status](_call(entry_point, status))


@pytest.mark.parametrize(
    ('entry_point', 'status', 'settled'),
    [(name, status, settled) for name, by_status in _STATUS_SPECIFIC.items() for status, settled in by_status.items()],
)
def test_a_status_specific_refusal_reaches_the_caller_as_its_own_shape(
    entry_point: str, status: int, settled: type[Exception]
) -> None:
    with pytest.raises(settled):
        _call(entry_point, status)


@pytest.mark.parametrize('entry_point', list(_ENTRY_POINTS))
@pytest.mark.parametrize('status', [429, 500, 502, 503])
def test_a_transient_failure_stays_retryable(entry_point: str, status: int) -> None:
    """429 is the one 4xx a retry can clear — reading it as an answer states a fact about the rate."""
    with pytest.raises(httpx2.HTTPStatusError):
        _call(entry_point, status)


@pytest.mark.parametrize('entry_point', _EXEMPT)
@pytest.mark.parametrize('status', _CLIENT_ERRORS)
def test_an_exempt_transport_keeps_every_4xx_retryable(entry_point: str, status: int) -> None:
    """Mapping these would be worse than the defect the mapping fixes.

    gnomAD's 400 covers its rate limiter, which is precisely the failure a retry clears; the Broad
    hosts' would name the caller's variant for a fault in a hard-coded URL, on every call, forever.
    """
    with pytest.raises(httpx2.HTTPStatusError):
        _call(entry_point, status)
