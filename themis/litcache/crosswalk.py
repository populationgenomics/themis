"""The crosswalk: a Cloud SQL mint table mapping external ids to `doc_id`s.

Each paper has a single random `doc_id` (uuid4) that names its cache directory.
External ids (DOI, PMID, PMCID, …) are minted against a shared
`crosswalk(external_id PRIMARY KEY, doc_id)` table so concurrent ingestion
workers agree on one `doc_id` per paper without coordinating out of band.

`mint` claims *all* of a paper's ids in one transaction:

- no id collides → a fresh uuid4 is minted and every id inserted under it;
- exactly one incumbent `doc_id` → that paper is already cached; adopt it and
  insert any ids not yet recorded;
- two or more distinct incumbents → the paper's ids bridge previously-separate
  works (the late-binding cross-paper link). Adopt the canonical `doc_id` (the
  lowest in the class) and surface the full incumbent set so the caller writes
  the equivalence edge into the involved manifests — equivalence lives in the
  manifests, never DB-only.

The table is the mint lock, not the system of record: it holds no irreplaceable
state (rebuildable from manifests by inverting `external_ids` + `equivalence`).
The manifest write is the commit; an orphaned crosswalk row (claimed, manifest
never written) is harmless — the re-run reuses the claimed `doc_id`.

litcache's tables live in a dedicated `litcache` Postgres schema, namespaced off
the shared `themis` database's other tenants; every reference is schema-qualified,
so no `search_path` is assumed. The schema is the `0003_litcache_crosswalk` migration,
applied by the migrate runner at deploy; this module assumes the table exists. Ownership
and grants are the migration's job, not this module's: the migrator (deploy SA) owns the
schema and the ingestion SA gets schema-scoped `SELECT, INSERT` there — neither expressible
without the deployed identities, so they attach in the migration.
"""

from __future__ import annotations

import contextlib
import dataclasses
import uuid
from collections.abc import Iterable

import pg8000.dbapi
import pg8000.exceptions

# The Cloud SQL connection type litcache holds (connector + pg8000, IAM), aliased so the
# fully-qualified name doesn't repeat across every signature.
Connection = pg8000.dbapi.Connection

# A mint that loses every insert race still converges: on retry it sees the
# incumbent and adopts (no further insert). The bound only guards against a
# pathological livelock — exceeding it is a bug, so fail loud rather than spin.
_MAX_MINT_RETRIES = 16

# Postgres SQLSTATE for unique_violation. pg8000 carries it in the error's payload
# dict under key 'C'; it does not map SQLSTATEs to DBAPI exception subclasses.
_UNIQUE_VIOLATION = '23505'


def _is_unique_violation(error: pg8000.exceptions.DatabaseError) -> bool:
    detail = error.args[0] if error.args else None
    return isinstance(detail, dict) and detail.get('C') == _UNIQUE_VIOLATION


@dataclasses.dataclass(frozen=True)
class MintResult:
    """The outcome of minting a paper's external ids.

    Attributes:
        doc_id: The `doc_id` the paper now claims. The canonical (lowest)
            `doc_id` when a cross-paper link was detected.
        minted: True if a fresh uuid4 was created; False if an incumbent was
            adopted.
        linked_doc_ids: The distinct incumbent `doc_id`s when the ids bridged
            two or more previously-separate works (sorted, includes `doc_id`);
            empty otherwise. Non-empty means the caller must record an
            equivalence edge across these `doc_id`s.
    """

    doc_id: str
    minted: bool
    linked_doc_ids: tuple[str, ...]


def mint(conn: Connection, external_ids: Iterable[str]) -> MintResult:
    """Claim a paper's external ids, minting or adopting a `doc_id` atomically.

    Runs as one transaction on `conn` (committed on success, rolled back on
    conflict). Under concurrency two workers claiming a shared id converge on a
    single `doc_id`: the loser of the insert race hits the `UNIQUE` constraint,
    rolls back, retries, and adopts the winner.

    Args:
        conn: A Postgres connection in its default (non-autocommit) mode.
        external_ids: The paper's external ids, e.g. `{"doi:10.…", "pmid:…"}`.
            Order-insensitive; duplicates collapse.

    Returns:
        The `MintResult` for the claim.

    Raises:
        ValueError: If `external_ids` is empty.
        RuntimeError: If the insert race fails to converge within the retry
            bound (a bug, not an expected outcome).
    """
    ids = sorted(set(external_ids))
    if not ids:
        raise ValueError('mint requires at least one external id')

    for _ in range(_MAX_MINT_RETRIES):
        try:
            return _mint_once(conn, ids)
        except pg8000.exceptions.DatabaseError as e:
            if not _is_unique_violation(e):
                raise
            # A concurrent worker claimed one of our ids between the read and the
            # insert, so our INSERT hit the crosswalk's unique index. Roll back and
            # retry; the retry sees the incumbent and adopts.
            conn.rollback()
    raise RuntimeError(f'crosswalk mint did not converge after {_MAX_MINT_RETRIES} retries for {ids}')


def _mint_once(conn: Connection, ids: list[str]) -> MintResult:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id, doc_id FROM litcache.crosswalk WHERE external_id = ANY(%s)', (ids,))
        existing: dict[str, str] = dict(cur.fetchall())
        incumbents = sorted(set(existing.values()))

        if len(incumbents) >= 2:
            doc_id, linked, minted = incumbents[0], tuple(incumbents), False
        elif incumbents:
            doc_id, linked, minted = incumbents[0], (), False
        else:
            doc_id, linked, minted = str(uuid.uuid4()), (), True

        missing = [i for i in ids if i not in existing]
        if missing:
            # A genuine cross-paper link leaves each incumbent id on its own
            # doc_id (the join is the manifest edge, not a row rewrite); only the
            # paper's not-yet-recorded ids attach to the canonical doc_id.
            cur.executemany(
                'INSERT INTO litcache.crosswalk (external_id, doc_id) VALUES (%s, %s)',
                [(i, doc_id) for i in missing],
            )
    conn.commit()
    return MintResult(doc_id=doc_id, minted=minted, linked_doc_ids=linked)
