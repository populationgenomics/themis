"""Tests for on-demand ingestion by external id (`themis.litcache.ingest`).

Two real stores, both Docker-gated: the crosswalk mints against a throwaway Postgres (the
UNIQUE-in-one-transaction semantics are what makes the two-store sequence safe, so they are never
mocked) and the manifest commits into a fake-gcs-server bucket. Only the upstream metadata ladder is
faked, so no test reaches PubMed or Crossref.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import typing
from collections.abc import Iterable

import httpx2
import pg8000.dbapi
import pytest
from google.cloud import storage as gcs
from pubmed_proto import pubmed_pb2

from themis.litcache import crosswalk, identity, ingest, outcome, paper_metadata, rebuild, resolve, writer
from themis.litcache.models import litcache_pb2

_PMID = identity.ExternalId(scheme='pmid', value='29089047')
_DOI = identity.ExternalId(scheme='doi', value='10.1038/s41586-020-2649-2')
_PMCID = identity.ExternalId(scheme='pmcid', value='PMC7759461')

# `ingest_unknown_paper` hands the client straight to the injected ladder, which never uses it.
_NO_HTTP = typing.cast('httpx2.AsyncClient', None)


def _metadata(title: str) -> bytes:
    """The `metadata.pb` bytes a PubMed rung answers with: a record inside its envelope.

    The envelope, not the bare record — `writer.write_paper` keeps nothing else, so a bare
    `PubmedArticle` here would exercise a shape no rung of the ladder produces.
    """
    article = pubmed_pb2.PubmedArticle()
    article.medline_citation.article.article_title.value = title
    return paper_metadata.to_canonical_bytes(
        litcache_pb2.PaperMetadata(pubmed=litcache_pb2.PubmedRecord(article=article))
    )


def _resolving(external_ids: litcache_pb2.ExternalIds, *, title: str = 'A resolved paper') -> ingest.MetadataResolver:
    """A ladder that resolves every id to one record carrying `external_ids` as its cross-ids."""

    async def resolve_metadata(**_kwargs: object) -> resolve.ResolvedPaper:
        return resolve.ResolvedPaper(metadata=_metadata(title), external_ids=external_ids, publisher=None)

    return resolve_metadata


def _unresolvable() -> ingest.MetadataResolver:
    """A ladder that answers a clean miss — upstream holds no such paper."""

    async def resolve_metadata(*, pmid: str | None, doi: str | None, **_kwargs: object) -> resolve.ResolvedPaper:
        raise resolve.MetadataUnresolvedError(pmid=pmid, doi=doi)

    return resolve_metadata


def _failing(error: Exception) -> ingest.MetadataResolver:
    """A ladder whose upstream did not answer — transient, so the error propagates."""

    async def resolve_metadata(**_kwargs: object) -> resolve.ResolvedPaper:
        raise error

    return resolve_metadata


def _ingest(
    bucket: gcs.Bucket,
    conn: pg8000.dbapi.Connection,
    external_id: identity.ExternalId,
    resolve_metadata: ingest.MetadataResolver,
) -> ingest.IngestResult | None:
    return asyncio.run(
        ingest.ingest_unknown_paper(
            bucket,
            functools.partial(crosswalk.lookup, conn),
            functools.partial(crosswalk.mint, conn),
            external_id,
            http_client=_NO_HTTP,
            resolve_metadata=resolve_metadata,
        )
    )


def _manifest(bucket: gcs.Bucket, doc_id: str) -> litcache_pb2.Manifest | None:
    blob = bucket.blob(writer.manifest_path(doc_id))
    if not blob.exists():
        return None
    return litcache_pb2.Manifest.FromString(blob.download_as_bytes())


def _rows(conn: pg8000.dbapi.Connection) -> dict[str, str]:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id, doc_id FROM litcache.crosswalk')
        return dict(cur.fetchall())


def test_an_id_that_resolves_upstream_is_minted_and_its_manifest_committed(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    result = _ingest(gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value)))

    assert result is not None
    assert result.minted
    assert result.written
    assert _rows(conn) == {_PMID.key: result.doc_id}
    manifest = _manifest(gcs_bucket, result.doc_id)
    assert manifest is not None
    assert manifest.external_ids.pmid == _PMID.value
    assert manifest.claim_key == _PMID.key
    # The whole point of an id-only manifest: no bytes were fetched, and the ladder still has its
    # input — `produce_full_text` builds the OA bundle from `external_ids` alone.
    assert not manifest.sources
    assert not manifest.renderings


def test_the_ingested_paper_reads_pending_so_the_conversion_lane_picks_it_up(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # A manifest with no rendering and no terminal marker is PENDING, which is what makes the enqueue
    # fire. A paper that read READY or terminal here would be ingested and then never produced.
    result = _ingest(gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value)))
    assert result is not None
    assert outcome.read_readiness(gcs_bucket, result.doc_id) is outcome.Readiness.PENDING


def test_the_manifest_carries_the_bibliographic_record_the_ladder_resolved(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    result = _ingest(
        gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value), title='Rare variant curation')
    )
    assert result is not None
    metadata = gcs_bucket.blob(f'{writer.paper_dir(result.doc_id)}/metadata.pb').download_as_bytes()
    envelope = paper_metadata.parse(metadata)
    assert envelope.pubmed.article.medline_citation.article.article_title.value == 'Rare variant curation'


def test_an_id_upstream_does_not_know_claims_nothing_and_writes_nothing(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # The reason the resolution comes first: a mint is permanent, so a fabricated id must leave no
    # crosswalk row and no manifest behind.
    assert _ingest(gcs_bucket, conn, _DOI, _unresolvable()) is None
    assert _rows(conn) == {}
    assert not list(gcs_bucket.list_blobs(prefix='papers/'))


def test_an_upstream_that_did_not_answer_claims_nothing_and_propagates(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # A transient failure must not read as "no such paper": it propagates, so the caller retries,
    # rather than settling the id as absent.
    request = httpx2.Request('GET', 'https://api.crossref.org/works/x')
    error = httpx2.HTTPStatusError('boom', request=request, response=httpx2.Response(503, request=request))
    with pytest.raises(httpx2.HTTPStatusError):
        _ingest(gcs_bucket, conn, _DOI, _failing(error))
    assert _rows(conn) == {}


def test_the_cross_ids_resolution_harvested_are_minted_alongside_the_requested_one(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    resolved = litcache_pb2.ExternalIds(pmid=_PMID.value, doi=_DOI.value, pmcid=_PMCID.value)
    result = _ingest(gcs_bucket, conn, _PMID, _resolving(resolved))

    assert result is not None
    assert _rows(conn) == dict.fromkeys((_PMID.key, crosswalk.normalise_key(_DOI.key), _PMCID.key), result.doc_id)
    manifest = _manifest(gcs_bucket, result.doc_id)
    assert manifest is not None
    assert (manifest.external_ids.doi, manifest.external_ids.pmcid) == (_DOI.value, _PMCID.value)
    # DOI beats PMID in the precedence order, so the claim key follows the harvested id.
    assert manifest.claim_key == _DOI.key


def test_a_re_run_after_a_crash_before_the_manifest_completes_under_the_claimed_doc_id(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # The recovery property the two-store sequence rests on: the claim survives, so the re-run adopts
    # it and commits the manifest that was missing rather than minting a second doc_id.
    claimed = crosswalk.mint(conn, [_PMID.key])
    assert _manifest(gcs_bucket, claimed.doc_id) is None

    result = _ingest(gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value)))

    assert result is not None
    assert result.doc_id == claimed.doc_id
    assert not result.minted
    assert result.written
    assert _manifest(gcs_bucket, claimed.doc_id) is not None


def test_an_id_whose_scheme_the_ladder_cannot_resolve_fails_loud(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # The metadata ladder keys on a PMID or a DOI. A PMCID silently reading as "no such paper" would
    # be a real paper written off, so the precondition raises and the caller filters instead.
    with pytest.raises(ValueError, match='resolvable schemes'):
        _ingest(gcs_bucket, conn, _PMCID, _resolving(litcache_pb2.ExternalIds(pmcid=_PMCID.value)))
    assert _rows(conn) == {}


def test_a_paper_the_corpus_already_holds_under_another_id_is_adopted_and_claims_nothing(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # The rebuildability rule: this path never claims an id it is not also going to name in a manifest
    # it writes. The paper is committed already, so the requested id claims nothing — the caller still
    # gets the doc_id, and the crosswalk does not gain a row `rebuild` would drop.
    committed = _ingest(gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value)))
    assert committed is not None
    resolved = litcache_pb2.ExternalIds(pmid=_PMID.value, doi=_DOI.value)

    adopted = _ingest(gcs_bucket, conn, _DOI, _resolving(resolved))

    assert adopted is not None
    assert adopted.doc_id == committed.doc_id
    assert not adopted.minted
    assert not adopted.written
    assert _rows(conn) == {_PMID.key: committed.doc_id}, 'the DOI is not claimed against a manifest that omits it'


def test_ids_bridging_two_separately_held_papers_adopt_the_canonical_one(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # A work the corpus holds twice is an inconsistency this path discovers, not one it created.
    # Repairing it means rewriting a committed manifest, which this path never does, so it serves the
    # canonical doc_id and claims nothing.
    first = _ingest(gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value)))
    second = _ingest(gcs_bucket, conn, _DOI, _resolving(litcache_pb2.ExternalIds(doi=_DOI.value)))
    assert first is not None
    assert second is not None
    assert first.doc_id != second.doc_id
    before = _rows(conn)

    bridged = _ingest(gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value, doi=_DOI.value)))

    assert bridged is not None
    assert bridged.doc_id == min((first.doc_id, second.doc_id))
    assert _rows(conn) == before


def test_ids_bridging_an_orphan_and_a_committed_paper_adopt_the_committed_one(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # An orphaned claim — a run that crashed before its manifest — can sort below a committed paper.
    # Adopting the lowest would commit a second manifest naming the id the committed one names, and
    # `rebuild` refuses an id claimed by two doc_ids. The committed paper is the one adopted.
    committed = _ingest(gcs_bucket, conn, _PMID, _resolving(litcache_pb2.ExternalIds(pmid=_PMID.value)))
    assert committed is not None
    orphan = '00000000-0000-4000-8000-000000000000'  # below any uuid4 the committed paper took
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute(
            'INSERT INTO litcache.crosswalk (external_id, doc_id) VALUES (%s, %s)',
            (crosswalk.normalise_key(_DOI.key), orphan),
        )
    conn.commit()
    before = _rows(conn)

    bridged = _ingest(gcs_bucket, conn, _DOI, _resolving(litcache_pb2.ExternalIds(doi=_DOI.value, pmid=_PMID.value)))

    assert bridged is not None
    assert bridged.doc_id == committed.doc_id
    assert not bridged.written
    assert _manifest(gcs_bucket, orphan) is None, "no second manifest names the committed paper's ids"
    assert _rows(conn) == before
    rebuild.rebuild(conn, gcs_bucket)  # the inconsistency the lowest-only check produced raised here


def test_only_one_id_per_scheme_is_claimed_so_the_manifest_can_name_every_claim(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # Crossref answers a DOI alias with the work's primary DOI, and `ExternalIds` holds one DOI. The
    # requested spelling wins, because that is the one the caller will ask by again; the primary is not
    # claimed, since no manifest could name it.
    alias = identity.ExternalId(scheme='doi', value='10.1/alias')
    result = _ingest(gcs_bucket, conn, alias, _resolving(litcache_pb2.ExternalIds(doi=_DOI.value)))

    assert result is not None
    assert _rows(conn) == {alias.key: result.doc_id}
    manifest = _manifest(gcs_bucket, result.doc_id)
    assert manifest is not None
    assert manifest.external_ids.doi == alias.value


@pytest.mark.parametrize(
    'resolved',
    [
        litcache_pb2.ExternalIds(pmid=_PMID.value),
        litcache_pb2.ExternalIds(pmid=_PMID.value, doi=_DOI.value, pmcid=_PMCID.value),
        litcache_pb2.ExternalIds(doi='10.1/primary'),  # upstream answers with a different DOI
    ],
)
def test_every_claim_this_path_makes_is_named_by_the_manifest_it_writes(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket, resolved: litcache_pb2.ExternalIds
) -> None:
    # The invariant that keeps the crosswalk rebuildable: `rebuild` replaces the table with the
    # inversion of the manifests, so a claim no manifest names is a row the next rebuild deletes —
    # after the caller was told the id resolves.
    result = _ingest(gcs_bucket, conn, _DOI, _resolving(resolved))
    assert result is not None
    manifest = _manifest(gcs_bucket, result.doc_id)
    assert manifest is not None
    advertised = {
        crosswalk.normalise_key(f'{scheme}:{getattr(manifest.external_ids, scheme)}')
        for scheme in ('doi', 'pmid', 'pmcid')
        if manifest.external_ids.HasField(scheme)
    }
    assert advertised == set(_rows(conn))


def test_manifest_external_ids_drops_an_id_the_manifest_has_no_field_for() -> None:
    # `binhash` (identity's content-hash fallthrough) is a crosswalk scheme with no manifest field, so
    # a paper carrying one advertises the rest and not it.
    ids: Iterable[identity.ExternalId] = (_DOI, _PMID, identity.ExternalId(scheme='binhash', value='deadbeef'))
    external_ids = ingest.manifest_external_ids(tuple(ids))
    assert (external_ids.doi, external_ids.pmid) == (_DOI.value, _PMID.value)
    assert not external_ids.HasField('pmcid')
