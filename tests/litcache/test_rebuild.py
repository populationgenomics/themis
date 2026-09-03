"""Tests for `themis.litcache.rebuild` — reconstructing the crosswalk from manifests.

The "matches a freshly-minted table" property is the load-bearing one, so it runs against
the same throwaway Postgres as the crosswalk mint tests, plus a fake-gcs-server bucket for
the manifests (both Docker-gated via the shared `docker_daemon`). The inversion and
equivalence cross-checks are pure and exercised directly.
"""

from __future__ import annotations

import contextlib
import uuid

import pg8000.dbapi
import pytest
from google.cloud import storage as gcs

from themis.litcache import crosswalk, rebuild
from themis.litcache.models import litcache_pb2


def _write_manifest(
    bucket: gcs.Bucket,
    doc_id: str,
    external_ids: dict[str, str],
    *,
    edges: list[str] | None = None,
    canonical: str | None = None,
) -> None:
    """Write a minimal manifest — only the fields `rebuild` reads (doc_id, external_ids, equivalence)."""
    manifest = litcache_pb2.Manifest(
        doc_id=doc_id,
        external_ids=litcache_pb2.ExternalIds(**external_ids),
        equivalence=litcache_pb2.Equivalence(edges=edges or [], canonical_doc_id=canonical or doc_id),
    )
    if external_ids:
        scheme, value = next(iter(external_ids.items()))
        manifest.claim_key = f'{scheme}:{value}'
    bucket.blob(f'papers/{doc_id}/manifest.pb').upload_from_string(manifest.SerializeToString())


def _doc_ids(conn: pg8000.dbapi.Connection) -> dict[str, str]:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id, doc_id FROM litcache.crosswalk')
        return dict(cur.fetchall())


def test_rebuild_inverts_external_ids(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    _write_manifest(gcs_bucket, 'doc-a', {'doi': '10.1/a', 'pmid': '1'})
    _write_manifest(gcs_bucket, 'doc-b', {'doi': '10.1/b'})

    result = rebuild.rebuild(conn, gcs_bucket)

    assert result.papers == 2
    assert result.external_ids == 3
    assert _doc_ids(conn) == {'doi:10.1/a': 'doc-a', 'pmid:1': 'doc-a', 'doi:10.1/b': 'doc-b'}


def test_every_external_id_field_is_an_optional_string_scheme() -> None:
    # The rebuild (and the producer) read `ExternalIds`' field names as `{scheme}:` prefixes and test
    # presence with `HasField`; a field of any other shape would be inverted as garbage or not at all.
    for field in litcache_pb2.ExternalIds.DESCRIPTOR.fields:
        assert field.type == field.TYPE_STRING, field.name
        assert field.has_presence, field.name


def test_rebuild_inverts_every_external_id_field(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    # Every id the manifest models reaches the crosswalk — the property the rebuild's "claimed iff in the
    # manifest" invariant rests on — for whatever fields the proto declares.
    ids = {field.name: str(n) for n, field in enumerate(litcache_pb2.ExternalIds.DESCRIPTOR.fields, start=1)}
    _write_manifest(gcs_bucket, 'doc-a', ids)

    result = rebuild.rebuild(conn, gcs_bucket)

    assert result.external_ids == len(ids)
    assert _doc_ids(conn) == {f'{scheme}:{value}': 'doc-a' for scheme, value in ids.items()}


def test_rebuild_matches_a_freshly_minted_table(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    # Mint two papers and a cross-paper link, capture the table, then rebuild
    # from manifests that reflect that minted state and compare.
    first = crosswalk.mint(conn, ['doi:10.1/x'])
    second = crosswalk.mint(conn, ['pmid:111'])
    linked = crosswalk.mint(conn, ['doi:10.1/x', 'pmid:111'])
    assert linked.linked_doc_ids == tuple(sorted([first.doc_id, second.doc_id]))
    minted = _doc_ids(conn)

    canonical = min(first.doc_id, second.doc_id)
    _write_manifest(gcs_bucket, first.doc_id, {'doi': '10.1/x'}, edges=[second.doc_id], canonical=canonical)
    _write_manifest(gcs_bucket, second.doc_id, {'pmid': '111'}, edges=[first.doc_id], canonical=canonical)

    result = rebuild.rebuild(conn, gcs_bucket)

    assert _doc_ids(conn) == minted
    assert result.canonical_doc_ids == {first.doc_id: canonical, second.doc_id: canonical}


def test_rebuild_folds_a_manifests_case_variant_key(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    # A manifest holds its DOI as harvested, so rebuild must fold on the way in — otherwise the
    # rebuilt table keys a row the mint path could never have written, and the two disagree.
    _write_manifest(
        gcs_bucket, 'doc-a', {'doi': '10.1016/S0140-6736(20)30183-5', 'pmcid': 'pmc12345', 'bookid': 'nbk900001'}
    )

    rebuild.rebuild(conn, gcs_bucket)

    assert _doc_ids(conn) == {
        'doi:10.1016/s0140-6736(20)30183-5': 'doc-a',
        'pmcid:PMC12345': 'doc-a',
        'bookid:NBK900001': 'doc-a',
    }


def test_rebuild_rejects_two_manifests_whose_dois_differ_only_in_case(
    conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket
) -> None:
    # Folding makes these one identifier on two doc_ids, which is the duplicate-paper fault rebuild
    # already refuses to invert. Before the fold it produced two rows and succeeded.
    _write_manifest(gcs_bucket, 'doc-a', {'doi': '10.1/A'})
    _write_manifest(gcs_bucket, 'doc-b', {'doi': '10.1/a'})

    with pytest.raises(ValueError, match='claimed by two doc_ids'):
        rebuild.rebuild(conn, gcs_bucket)


def test_rebuild_is_idempotent(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    _write_manifest(gcs_bucket, 'doc-a', {'doi': '10.1/a', 'pmid': '1'})

    rebuild.rebuild(conn, gcs_bucket)
    first = _doc_ids(conn)
    rebuild.rebuild(conn, gcs_bucket)

    assert _doc_ids(conn) == first


def test_rebuild_assigns_lowest_doc_id_as_canonical(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    # Three-member class linked only by direct edges to one hub; transitive
    # closure must still collapse them to a single lowest canonical.
    canonical = 'doc-001'
    _write_manifest(gcs_bucket, 'doc-003', {'doi': '10.1/c'}, edges=['doc-002'], canonical=canonical)
    _write_manifest(gcs_bucket, 'doc-002', {'pmid': '2'}, edges=['doc-001'], canonical=canonical)
    _write_manifest(gcs_bucket, 'doc-001', {'pmcid': 'PMC1'}, edges=['doc-002'], canonical=canonical)

    result = rebuild.rebuild(conn, gcs_bucket)

    assert result.canonical_doc_ids == {'doc-001': canonical, 'doc-002': canonical, 'doc-003': canonical}


def test_rebuild_rejects_an_external_id_claimed_twice(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    _write_manifest(gcs_bucket, 'doc-a', {'doi': '10.1/shared'})
    _write_manifest(gcs_bucket, 'doc-b', {'doi': '10.1/shared'})

    with pytest.raises(ValueError, match='claimed by two doc_ids'):
        rebuild.rebuild(conn, gcs_bucket)


def test_rebuild_rejects_a_dangling_equivalence_edge(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    _write_manifest(gcs_bucket, 'doc-a', {'doi': '10.1/a'}, edges=['doc-ghost'], canonical='doc-a')

    with pytest.raises(ValueError, match='no manifest'):
        rebuild.rebuild(conn, gcs_bucket)


def test_rebuild_rejects_a_disagreeing_canonical(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    # The class {doc-a, doc-b} has canonical doc-a, but the manifests record doc-b.
    _write_manifest(gcs_bucket, 'doc-a', {'doi': '10.1/a'}, edges=['doc-b'], canonical='doc-b')
    _write_manifest(gcs_bucket, 'doc-b', {'pmid': '1'}, edges=['doc-a'], canonical='doc-b')

    with pytest.raises(ValueError, match='records canonical'):
        rebuild.rebuild(conn, gcs_bucket)


def test_rebuild_rejects_a_manifest_with_no_external_ids(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    manifest = litcache_pb2.Manifest(
        doc_id='doc-a',
        claim_key='sha256:deadbeef',
        equivalence=litcache_pb2.Equivalence(canonical_doc_id='doc-a'),
    )
    gcs_bucket.blob('papers/doc-a/manifest.pb').upload_from_string(manifest.SerializeToString())

    with pytest.raises(ValueError, match='no external ids'):
        rebuild.rebuild(conn, gcs_bucket)


def test_rebuild_ignores_non_manifest_keys(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    _write_manifest(gcs_bucket, 'doc-a', {'doi': '10.1/a'})
    gcs_bucket.blob('papers/doc-a/metadata.pb').upload_from_string(b'\x00')
    gcs_bucket.blob('papers/doc-a/renderings/0.md').upload_from_string(b'# x')

    result = rebuild.rebuild(conn, gcs_bucket)

    assert result.papers == 1
    assert _doc_ids(conn) == {'doi:10.1/a': 'doc-a'}


def test_rebuild_of_an_empty_bucket_clears_the_table(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('INSERT INTO litcache.crosswalk (external_id, doc_id) VALUES (%s, %s)', ('doi:stale', 'doc-stale'))
    conn.commit()

    result = rebuild.rebuild(conn, gcs_bucket)

    assert result.papers == 0
    assert _doc_ids(conn) == {}


def test_rebuild_smoke_uses_real_uuids(conn: pg8000.dbapi.Connection, gcs_bucket: gcs.Bucket) -> None:
    doc_id = str(uuid.uuid4())
    _write_manifest(gcs_bucket, doc_id, {'doi': '10.1/a'})

    rebuild.rebuild(conn, gcs_bucket)

    assert _doc_ids(conn) == {'doi:10.1/a': doc_id}
