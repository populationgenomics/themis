"""The weekly reference-refresh job: dump-shape contract, ETag skip, retry, PanelApp aggregation.

Upstream HTTP is served by an httpx2 ``MockTransport``; the bucket is an in-memory fake. The raw
GenCC/ClinGen dumps are served from the very fixtures the server's loaders parse, so a produced dump
is asserted by round-tripping it back through those loaders — the parse contract, not a pinned
payload. The PanelApp dump is round-tripped through ``upstreams.panelapp.PanelAppTable``.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import override

import httpx2
import pytest

from themis.services.evidence.gene_disease import backend as gene_disease_backend
from themis.services.evidence.gene_disease.refresh import job
from themis.services.evidence.gene_disease.refresh import object_store as refresh_store
from themis.services.evidence.gene_disease.refresh import panelapp as refresh_panelapp
from themis.services.evidence.upstreams import clingen_dosage, clingen_validity, gencc
from themis.services.evidence.upstreams import panelapp as panelapp_table

_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / 'upstreams' / 'tests' / 'fixtures'
_GENCC_TSV = (_FIXTURES / 'gencc.tsv').read_bytes()
_VALIDITY_CSV = (_FIXTURES / 'clingen_validity.csv').read_bytes()
_DOSAGE_CSV = (_FIXTURES / 'clingen_dosage.csv').read_bytes()

_Handler = Callable[[httpx2.Request], httpx2.Response]


class _FakeStore(refresh_store.ReferenceObjectStore):
    """An in-memory object store recording every write, for asserting what the refresh touched."""

    def __init__(self, seed: Mapping[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(seed) if seed is not None else {}
        self.writes: list[str] = []

    @override
    async def read(self, name: str) -> bytes | None:
        return self.objects.get(name)

    @override
    async def write(self, name: str, data: bytes) -> None:
        self.objects[name] = data
        self.writes.append(name)


def _gene(
    symbol: str, hgnc_id: str, confidence: str, moi: str, mode_of_pathogenicity: str | None, publications: list[str]
) -> dict[str, object]:
    """A raw panel-detail gene entry, the full shape the dump keeps verbatim in ``entries``."""
    return {
        'entity_type': 'gene',
        'entity_name': symbol,
        'confidence_level': confidence,
        'penetrance': None,
        'mode_of_pathogenicity': mode_of_pathogenicity,
        'publications': publications,
        'evidence': ['Expert Review Green'],
        'phenotypes': [f'{symbol} phenotype, MIM# 123456'],
        'mode_of_inheritance': moi,
        'tags': [],
        'gene_data': {'hgnc_id': hgnc_id, 'gene_symbol': symbol, 'gene_name': f'{symbol} gene'},
    }


# BRCA1 sits on both panels (amber on Mendeliome, green on Incidentalome); BRAF only on Mendeliome,
# carrying a gain-of-function veto. The two BRCA1 confidences exercise the max-across-panels rule. BRCA1's
# ``mode_of_pathogenicity`` is JSON null — the shape PanelApp sends for the common no-veto case — so the dump's
# empty-string veto asserts the null->'' handling, not just an already-empty string.
_PANELS: dict[int, dict[str, object]] = {
    137: {
        'name': 'Mendeliome',
        'genes': [
            _gene('BRCA1', 'HGNC:1100', '2', 'MONOALLELIC, autosomal or pseudoautosomal', None, ['111']),
            _gene('BRAF', 'HGNC:1097', '2', 'MONOALLELIC, autosomal or pseudoautosomal', 'gain-of-function', []),
        ],
    },
    126: {
        'name': 'Incidentalome',
        'genes': [_gene('BRCA1', 'HGNC:1100', '3', 'BIALLELIC, autosomal or pseudoautosomal', None, ['222'])],
    },
}

# Per (panel, HGNC-numeric) evaluation comments. The shared BRCA1 comment across both panels tests the
# merge + de-dupe; the BRAF numeric maps to a 404 (gene carries no evaluations resource) -> [].
_SHARED_BRCA1_COMMENT = 'BRCA1 acts through loss of function; truncating variants abolish HR repair.'
_EVALUATIONS: dict[tuple[str, str], list[str]] = {
    ('137', '1100'): [_SHARED_BRCA1_COMMENT],
    ('126', '1100'): [_SHARED_BRCA1_COMMENT, 'Expert review: green on the Incidentalome panel.'],
}
_EVAL_PATH = re.compile(r'^/api/v1/panels/(\d+)/genes/HGNC:(\d+)/evaluations/$')


def _panelapp_response(request: httpx2.Request) -> httpx2.Response:
    path = request.url.path
    evaluation = _EVAL_PATH.match(path)
    if evaluation is not None:
        key = (evaluation.group(1), evaluation.group(2))
        if key not in _EVALUATIONS:
            return httpx2.Response(httpx2.codes.NOT_FOUND, json={'detail': 'not found'})
        comments = [{'created': '2026-01-01', 'comment': text, 'user_name': 'Curator'} for text in _EVALUATIONS[key]]
        return httpx2.Response(
            200, json={'count': 1, 'next': None, 'previous': None, 'results': [{'comments': comments}]}
        )
    panel = re.fullmatch(r'/api/v1/panels/(\d+)/', path)
    if panel is not None:
        return httpx2.Response(200, json=_PANELS[int(panel.group(1))])
    raise AssertionError(f'unexpected PanelApp request {request.url}')


def _handler(*, gencc_etag: str | None = None, gencc_status: Callable[[], int] | None = None) -> _Handler:
    """Route the raw downloads (served from fixtures) and the PanelApp API.

    ``gencc_etag`` makes the GenCC endpoint answer 304 when ``If-None-Match`` matches it. ``gencc_status``,
    when set, is a callable returning the status for each GenCC call (to script a transient failure).
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        host, path = request.url.host, request.url.path
        if host == 'search.thegencc.org':
            if gencc_status is not None:
                status = gencc_status()
                if status != 200:
                    return httpx2.Response(status, json={})
            if gencc_etag is not None and request.headers.get('If-None-Match') == gencc_etag:
                return httpx2.Response(httpx2.codes.NOT_MODIFIED)
            return httpx2.Response(200, content=_GENCC_TSV, headers={'ETag': '"gencc-v1"'})
        if path == '/kb/gene-validity/download':
            return httpx2.Response(200, content=_VALIDITY_CSV, headers={'ETag': '"validity-v1"'})
        if path == '/kb/gene-dosage/download':
            return httpx2.Response(200, content=_DOSAGE_CSV, headers={'ETag': '"dosage-v1"'})
        if host == 'panelapp-aus.org':
            return _panelapp_response(request)
        raise AssertionError(f'unexpected request {request.url}')

    return handler


def _run[T](handler: _Handler, call: Callable[[httpx2.AsyncClient], Awaitable[T]]) -> T:
    async def run() -> T:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await call(client)

    return asyncio.run(run())


def test_run_writes_all_four_dumps_parseable_by_the_server_loaders() -> None:
    store = _FakeStore()
    report = _run(_handler(), lambda client: job.run(store, client=client))

    # Every reference object the server loads at startup is present, under the names the loader itself
    # holds — the job and the loader agree on the dataset prefix or the service starts to a missing dump.
    for name in gene_disease_backend._REFERENCE_OBJECTS:
        assert name in store.objects

    # Raw dumps are stored verbatim: the bytes parse through the exact loaders the server uses.
    assert gencc.GenCC.from_bytes(store.objects[gene_disease_backend._GENCC_OBJECT]).lookup('HGNC:1100') is not None
    assert clingen_validity.ClinGenValidity.from_bytes(store.objects[gene_disease_backend._VALIDITY_OBJECT]).lookup(
        'HGNC:1100'
    )
    assert clingen_dosage.ClinGenDosage.from_bytes(store.objects[gene_disease_backend._DOSAGE_OBJECT]).lookup(
        'HGNC:1100'
    )

    # Each ETag-bearing raw refresh stored a fresh sidecar.
    assert all(outcome.changed and outcome.etag_stored for outcome in report.raw_outcomes)


def test_panelapp_dump_matches_the_table_parse_contract() -> None:
    store = _FakeStore()
    _run(_handler(), lambda client: job.run(store, client=client))
    table = panelapp_table.PanelAppTable.from_bytes(store.objects[gene_disease_backend._PANELAPP_OBJECT])

    result = table.lookup('HGNC:1100')
    assert result is not None
    assert result.gene_symbol == 'BRCA1'
    # entries keep the full per-panel gene JSON verbatim, incl. the publications the agent mines.
    assert any(entry.get('publications') for entry in result.entries)
    assert all('gene_data' in entry for entry in result.entries)
    # Evaluations survive to the dump, merged across panels and de-duped, non-empty only.
    assert _SHARED_BRCA1_COMMENT in result.evaluations
    assert len(result.evaluations) == len(set(result.evaluations))
    assert all(comment.strip() for comment in result.evaluations)


def test_conditional_get_304_leaves_the_dump_unwritten() -> None:
    store = _FakeStore(seed={f'{gene_disease_backend._GENCC_OBJECT}.etag': b'"gencc-v1"'})
    report = _run(_handler(gencc_etag='"gencc-v1"'), lambda client: job.run(store, client=client))

    gencc_outcome = next(o for o in report.raw_outcomes if o.object_name == gene_disease_backend._GENCC_OBJECT)
    assert not gencc_outcome.changed
    assert gene_disease_backend._GENCC_OBJECT not in store.writes  # the unchanged dump was not rewritten
    # The other files (no seeded ETag) and the PanelApp dump are still refreshed.
    assert gene_disease_backend._VALIDITY_OBJECT in store.writes
    assert gene_disease_backend._PANELAPP_OBJECT in store.writes


def test_transient_5xx_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, 'sleep', _instant_sleep)
    calls = iter([503, 200, 200, 200, 200])
    store = _FakeStore()
    _run(_handler(gencc_status=lambda: next(calls)), lambda client: job.run(store, client=client))
    assert gene_disease_backend._GENCC_OBJECT in store.objects  # the retry recovered after the 503


def test_transient_transport_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, 'sleep', _instant_sleep)
    base = _handler()
    blips = iter([httpx2.ConnectError('transient'), None])

    def flaky_gencc(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == 'search.thegencc.org' and (blip := next(blips)) is not None:
            raise blip
        return base(request)

    store = _FakeStore()
    _run(flaky_gencc, lambda client: job.run(store, client=client))
    assert gene_disease_backend._GENCC_OBJECT in store.objects  # the retry recovered after the transport blip


def test_malformed_download_raises_before_writing_the_bucket() -> None:
    base = _handler()

    def comma_gencc(request: httpx2.Request) -> httpx2.Response:
        # GenCC as commas where the loader parses tabs: the header collapses -> the loader raises.
        if request.url.host == 'search.thegencc.org':
            return httpx2.Response(200, content=_GENCC_TSV.replace(b'\t', b','), headers={'ETag': '"gencc-v1"'})
        return base(request)

    store = _FakeStore()
    with pytest.raises(ValueError, match='did not parse through the server loader'):
        _run(comma_gencc, lambda client: job.run(store, client=client))
    assert gene_disease_backend._GENCC_OBJECT not in store.objects  # the unparseable bytes never reached the bucket


def test_build_dump_takes_max_confidence_across_panels() -> None:
    dump = _run(_handler(), lambda client: refresh_panelapp.build_dump(client, refresh_date='2026-07-25'))
    genes = dump['genes']
    assert isinstance(genes, dict)

    brca1 = genes['HGNC:1100']
    assert isinstance(brca1, dict)
    assert brca1['max_confidence'] == 3  # green on Incidentalome wins over amber on Mendeliome
    assert brca1['mode_of_inheritance'] == 'BIALLELIC, autosomal or pseudoautosomal'  # the MOI of the max entry
    assert brca1['mode_of_pathogenicity'] == ''

    braf = genes['HGNC:1097']
    assert isinstance(braf, dict)
    assert braf['max_confidence'] == 2
    assert braf['mode_of_pathogenicity'] == 'gain-of-function'  # the GoF veto is surfaced
    assert braf['evaluations'] == []  # BRAF's evaluations 404 -> empty


def test_build_dump_stamps_the_refresh_as_an_iso_dated_release() -> None:
    dump = _run(_handler(), refresh_panelapp.build_dump)
    versions = dump['dataset_versions']
    assert isinstance(versions, list)
    assert versions  # a dump naming no release would pass the date check vacuously
    assert all(re.fullmatch(r'\d{4}-\d{2}-\d{2}', version) for version in versions)


def test_build_dump_refuses_an_empty_gene_set() -> None:
    def empty_panels(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == 'panelapp-aus.org'
        return httpx2.Response(200, json={'name': 'Empty', 'genes': []})

    # A panel that comes back empty (an upstream glitch) must not blank out a good dump.
    with pytest.raises(ValueError, match='no genes'):
        _run(empty_panels, refresh_panelapp.build_dump)


def _paging_evaluations(next_link: Callable[[httpx2.Request], str]) -> _Handler:
    """`_handler`'s PanelApp routes, with every evaluations page linking on to `next_link`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == 'panelapp-aus.org'
        if _EVAL_PATH.match(request.url.path) is None:
            return _panelapp_response(request)
        return httpx2.Response(200, json={'count': 1, 'next': next_link(request), 'previous': None, 'results': []})

    return handler


def test_an_evaluations_page_linking_off_panelapp_is_not_followed() -> None:
    """`next` is upstream text and the job's client follows redirects, so an off-site link is not a page."""
    handler = _paging_evaluations(lambda _request: 'https://panelapp-aus.org.attacker.example/api/v1/panels/137/')

    with pytest.raises(ValueError, match='not under'):
        _run(handler, refresh_panelapp.build_dump)


def test_evaluations_that_never_stop_paging_are_refused() -> None:
    """A page linking to itself would otherwise hold the weekly job against PanelApp until it timed out."""
    handler = _paging_evaluations(lambda request: str(request.url))

    with pytest.raises(ValueError, match='looping rather than ending'):
        _run(handler, refresh_panelapp.build_dump)


async def _instant_sleep(_seconds: float) -> None:
    return None
