"""Rebuild the crosswalk mint table from the cache's manifests.

The crosswalk (`litcache.crosswalk`) is a derived index, not the system of
record: it is the inversion of every manifest's `external_ids`, over the
equivalence graph linking `doc_id`s. This module reconstructs it from the
bucket so the table can be dropped and rebuilt at will — the manifest write is
the commit, the table is only the mint lock.

The rebuild is a full reconstruction: it replaces the table contents in one
transaction, so it is idempotent and yields the rows a fresh sequence of
`crosswalk.mint` calls would have produced for the same papers. It fails loud on
any manifest inconsistency the table can't represent — a shared external id, a
dangling equivalence edge, or a recorded canonical that disagrees with the
recomputed class.
"""

from __future__ import annotations

import contextlib
import dataclasses

import pg8000.dbapi
from google.cloud import storage as gcs

from themis.litcache.models import litcache_pb2

# The Cloud SQL connection type litcache holds (connector + pg8000, IAM), aliased so the
# fully-qualified name doesn't repeat across every signature.
Connection = pg8000.dbapi.Connection


@dataclasses.dataclass(frozen=True)
class RebuildResult:
    """The outcome of a crosswalk rebuild.

    Attributes:
        papers: Number of manifests scanned.
        external_ids: Number of crosswalk rows written (distinct external ids).
        canonical_doc_ids: Every `doc_id` mapped to its equivalence class's
            canonical (lexicographically lowest) `doc_id`. A paper with no links
            maps to itself.
    """

    papers: int
    external_ids: int
    canonical_doc_ids: dict[str, str]


def rebuild(conn: Connection, bucket: gcs.Bucket, *, papers_prefix: str = 'papers/') -> RebuildResult:
    """Reconstruct the crosswalk table from the manifests under `papers_prefix`.

    Replaces the table's contents in one transaction with the inversion of every
    manifest's `external_ids` (the table itself is the `0003_litcache_crosswalk`
    migration). The equivalence graph is recomputed and cross-checked against each
    manifest's recorded canonical.

    Args:
        conn: A Postgres connection in its default (non-autocommit) mode.
        bucket: The cache bucket; paper directories live under `papers_prefix`.
        papers_prefix: The key prefix the paper directories live under.

    Returns:
        The `RebuildResult`.

    Raises:
        ValueError: On a manifest inconsistency — a paper with no external ids,
            an external id claimed by two `doc_id`s, an equivalence edge to a
            `doc_id` with no manifest, or a recorded `canonical_doc_id` that
            disagrees with the recomputed equivalence class.
    """
    manifests = _load_manifests(bucket, papers_prefix)
    rows = _invert(manifests)
    canonical = _canonical_doc_ids(manifests)
    _write_rows(conn, rows)
    return RebuildResult(papers=len(manifests), external_ids=len(rows), canonical_doc_ids=canonical)


def _load_manifests(bucket: gcs.Bucket, prefix: str) -> list[litcache_pb2.Manifest]:
    manifests: list[litcache_pb2.Manifest] = []
    for blob in bucket.list_blobs(prefix=prefix):
        rel = blob.name[len(prefix) :]
        # Match papers/{doc_id}/manifest.pb exactly, not nested files.
        if rel.endswith('/manifest.pb') and rel.count('/') == 1:
            manifests.append(litcache_pb2.Manifest.FromString(blob.download_as_bytes()))
    return manifests


def _external_id_keys(external_ids: litcache_pb2.ExternalIds) -> list[str]:
    """Map an `ExternalIds` to its `{scheme}:{value}` crosswalk keys (only the set fields).

    The scheme names are the proto field names, so presence is `HasField` — proto3
    `optional string` distinguishes an unset id from an empty one.
    """
    schemes = ('doi', 'pmid', 'pmcid', 'arxiv', 'biorxiv')
    return [f'{scheme}:{getattr(external_ids, scheme)}' for scheme in schemes if external_ids.HasField(scheme)]


def _invert(manifests: list[litcache_pb2.Manifest]) -> dict[str, str]:
    rows: dict[str, str] = {}
    for manifest in manifests:
        keys = _external_id_keys(manifest.external_ids)
        if not keys:
            raise ValueError(f'manifest {manifest.doc_id} has no external ids; it cannot be placed in the crosswalk')
        for key in keys:
            incumbent = rows.get(key)
            if incumbent is not None and incumbent != manifest.doc_id:
                raise ValueError(f'external id {key!r} is claimed by two doc_ids: {incumbent} and {manifest.doc_id}')
            rows[key] = manifest.doc_id
    return rows


def _canonical_doc_ids(manifests: list[litcache_pb2.Manifest]) -> dict[str, str]:
    """Recompute the canonical `doc_id` per equivalence class, cross-checking manifests.

    Builds the equivalence graph (each `doc_id` plus its edges) by union-find,
    assigns each class the lowest `doc_id` as canonical, and verifies every
    manifest's recorded `canonical_doc_id` agrees.
    """
    doc_ids = {manifest.doc_id for manifest in manifests}
    parent: dict[str, str] = {doc_id: doc_id for doc_id in doc_ids}

    def find(node: str) -> str:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    for manifest in manifests:
        for edge in manifest.equivalence.edges:
            if edge not in doc_ids:
                raise ValueError(f'manifest {manifest.doc_id} has an equivalence edge to {edge!r} with no manifest')
            parent[find(edge)] = find(manifest.doc_id)

    classes: dict[str, list[str]] = {}
    for doc_id in doc_ids:
        classes.setdefault(find(doc_id), []).append(doc_id)
    canonical = {doc_id: min(members) for members in classes.values() for doc_id in members}

    for manifest in manifests:
        recorded, expected = manifest.equivalence.canonical_doc_id, canonical[manifest.doc_id]
        if recorded != expected:
            raise ValueError(
                f'manifest {manifest.doc_id} records canonical {recorded} but its equivalence class is {expected}'
            )
    return canonical


def _write_rows(conn: Connection, rows: dict[str, str]) -> None:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('DELETE FROM litcache.crosswalk')
        if rows:
            cur.executemany(
                'INSERT INTO litcache.crosswalk (external_id, doc_id) VALUES (%s, %s)',
                list(rows.items()),
            )
    conn.commit()
