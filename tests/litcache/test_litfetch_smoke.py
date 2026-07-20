"""Smoke: litfetch/litdown import and their public API is usable in themis.

litcache's OA branch consumes litfetch as a black box — this proves the
dependencies resolve and the things litcache needs work: the file-set listing,
the body fetch, the raw licence + basis (litfetch), and JATS→markdown conversion
(litdown, called directly). litfetch's own ladder is tested in litfetch; here we
drive its public seams with in-memory backends so the offline suite stays
deterministic. A live OA fetch is integration-gated on `LITFETCH_LIVE_TEST_PMCID`
(suggested value: PMC5664429, the OA fixture paper).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from collections.abc import Mapping

import litdown
import litfetch
import pytest
from litfetch import artifacts, ids, source_metadata

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_OA_JATS = _FIXTURES / 'oa' / 'fulltext.xml'


def _jats_blob() -> artifacts.Blob:
    """A body Blob carrying the OA fixture's real JATS bytes."""
    content = _OA_JATS.read_bytes()
    file = artifacts.File(kind=artifacts.FileKind.BODY, source='fixture', media_type=artifacts.JATS_XML)
    return artifacts.Blob(file=file, content=content)


class _BlobFetcher:
    """A Fetcher that serves one in-memory body Blob, bypassing the network."""

    name = 'fixture'
    requires: frozenset[str] = frozenset()

    def __init__(self, blob: artifacts.Blob) -> None:
        self._blob = blob

    async def fetch(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: litfetch.Http,
    ) -> artifacts.Blob | None:
        del article_ids, credentials, http  # litfetch's Fetcher signature; this double ignores them
        return self._blob


class _StaticFileSource:
    """A FileSource that lists a fixed file-set without fetching anything."""

    name = 'fixture'

    def __init__(self, files: tuple[artifacts.File, ...]) -> None:
        self._files = files

    async def list_files(
        self,
        article_ids: ids.ArticleIds,
        *,
        credentials: Mapping[str, object] | None = None,
        http: litfetch.Http,
    ) -> tuple[artifacts.File, ...]:
        del article_ids, credentials, http  # litfetch's FileSource signature; this double ignores them
        return self._files

    async def fetch_file(
        self,
        file: artifacts.File,
        *,
        credentials: Mapping[str, object] | None = None,
        http: litfetch.Http,
    ) -> artifacts.Blob | None:
        del file, credentials, http  # litfetch's FileSource signature; this double ignores them
        return None


def test_litfetch_and_litdown_import() -> None:
    assert litfetch.__version__
    assert litdown.__version__


def test_raw_licence_and_basis_from_artifact() -> None:
    meta = source_metadata.extract_source_metadata(_jats_blob())
    # Raw, as litfetch returns it (the fixture's CC-BY URL lives in the licence
    # text) — litcache, not litfetch, maps this to an SPDX id.
    assert meta.licence is not None
    assert 'creativecommons.org/licenses/by/4.0' in meta.licence
    assert meta.basis == 'artifact'


def test_jats_converts_to_markdown_via_litdown() -> None:
    markdown = litdown.convert(_OA_JATS.read_bytes())
    assert markdown.strip()


def test_fetch_body_serves_the_ladder_blob() -> None:
    blob = asyncio.run(litfetch.fetch_body(ids.ArticleIds(pmcid='PMC5664429'), fetchers=[_BlobFetcher(_jats_blob())]))
    assert blob is not None
    assert blob.file.media_type == artifacts.JATS_XML
    assert blob.content


def test_list_files_returns_the_file_set() -> None:
    body = artifacts.File(kind=artifacts.FileKind.BODY, source='fixture', media_type=artifacts.JATS_XML)
    supp = artifacts.File(kind=artifacts.FileKind.SUPPLEMENTARY, source='fixture', filename='table1.xlsx')
    files = asyncio.run(
        litfetch.list_files(ids.ArticleIds(pmcid='PMC5664429'), sources=[_StaticFileSource((body, supp))])
    )
    assert {f.kind for f in files} == {artifacts.FileKind.BODY, artifacts.FileKind.SUPPLEMENTARY}


def test_live_oa_fetch() -> None:
    pmcid = os.environ.get('LITFETCH_LIVE_TEST_PMCID')
    if not pmcid:
        pytest.skip('set LITFETCH_LIVE_TEST_PMCID (e.g. PMC5664429) to run the live OA fetch')

    article = ids.ArticleIds(pmcid=pmcid)

    async def _run() -> tuple[artifacts.Blob | None, tuple[artifacts.File, ...]]:
        blob = await litfetch.fetch_body(article)
        files = await litfetch.list_files(article)
        return blob, files

    blob, files = asyncio.run(_run())
    assert blob is not None
    assert files  # the file-set
    meta = source_metadata.extract_source_metadata(blob)
    assert meta.basis is not None  # raw licence + basis present from the live bytes
