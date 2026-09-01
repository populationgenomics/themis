"""Reclaiming packs nothing references — the only operation here that can destroy data.

A pack has to be uploaded before the compare-and-swap that names it, and an upload cannot be
cheaply undone, so a lost race leaves a pack no ref names. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import time

from themis.sheaf import errors
from themis.sheaf import store as store_mod

# Comfortably longer than any publish; the asymmetry is deliberate.
DEFAULT_GRACE_SECONDS = 24 * 60 * 60


@dataclasses.dataclass(frozen=True)
class GcReport:
    """What a sweep found."""

    live: tuple[str, ...]
    orphans: tuple[str, ...]
    protected: tuple[str, ...]
    orphan_bytes: int = 0


def live_packs(store: store_mod.Store) -> set[str]:
    """Pack ids referenced by the current state or by any retained transition.

    Compaction replaces the manifest rather than extending it, so the current pack set is not a
    superset of what history needs: a retained generation of the ref document is the only record of
    which packs an older ref state can be hydrated from.
    """
    live = set(store.read().packs)
    for doc in store.transitions():
        live.update(doc.packs)
    return live


def retention_gap(store: store_mod.Store, snapshot: store_mod.Snapshot) -> str | None:
    """Explain why the ref document's history looks unavailable, or None if it looks fine.

    A bucket without object versioning — or an emulator that does not implement it — retains only
    the live generation, so `Store.transitions` returns one entry however many writes have
    happened, which is indistinguishable from a brand-new repository. Treating unretained as
    unreachable is how a sweep silently makes history unhydratable.

    Args:
        store: The repository being swept.
        snapshot: The state the sweep read.

    Returns:
        A description of the gap, or None where the history looks retained (or where there is too
        little state for the question to arise).
    """
    if len(snapshot.packs) <= 1:
        return None
    if len(store.transitions()) > 1:
        return None
    return (
        f'{len(snapshot.packs)} packs but only one retained generation of {store.ref_key}: '
        'the bucket is probably not versioned, so which packs history needs cannot be determined'
    )


def find_orphans(
    store: store_mod.Store,
    *,
    now: float | None = None,
    grace: float = DEFAULT_GRACE_SECONDS,
    require_retention: bool = True,
) -> GcReport:
    """Classify every pack in the namespace as live, orphaned, or protected by grace.

    Args:
        store: The repository to sweep.
        now: The instant ages are measured against; the wall clock when omitted.
        grace: Minimum age, in seconds, before an unreferenced pack counts as an orphan.
        require_retention: Fail rather than sweep when the ref document's history is not retained.

    Returns:
        The classification. A pack uploaded after the listing is not considered at all; one
        listed but named only after the live set was read is covered by grace — provided this
        upload is when it was created. Content addressing means a publish that reproduces bytes
        already stored writes nothing, so the age on record stays that of the first upload, and a
        pack orphaned long enough to lose its grace does not regain it by being named again.

    Raises:
        RetentionUnavailable: If `require_retention` and `retention_gap` reports a gap.
    """
    now = time.time() if now is None else now
    snapshot = store.read()
    if require_retention and (gap := retention_gap(store, snapshot)) is not None:
        raise errors.RetentionUnavailable(gap)
    listed = list(store.backend.list_immutable(store.pack_prefix))
    live = live_packs(store)

    orphans: list[str] = []
    protected: list[str] = []
    orphan_bytes = 0
    for info in listed:
        ident = info.key.removeprefix(store.pack_prefix).removesuffix('.pack')
        if ident in live:
            continue
        if now - info.created_at < grace:
            protected.append(ident)
        else:
            orphans.append(ident)
            orphan_bytes += info.size
    return GcReport(
        live=tuple(sorted(live)),
        orphans=tuple(sorted(orphans)),
        protected=tuple(sorted(protected)),
        orphan_bytes=orphan_bytes,
    )


def collect(
    store: store_mod.Store,
    *,
    now: float | None = None,
    grace: float = DEFAULT_GRACE_SECONDS,
    dry_run: bool = True,
    require_retention: bool = True,
) -> GcReport:
    """Find orphans and, unless `dry_run`, delete them.

    Args:
        store: The repository to sweep.
        now: The instant ages are measured against; the wall clock when omitted.
        grace: Minimum age, in seconds, before an unreferenced pack counts as an orphan.
        dry_run: Report what would be deleted without deleting it.
        require_retention: Fail rather than sweep when the ref document's history is not retained.

    Returns:
        What the sweep found, whether or not it deleted anything.

    Raises:
        RetentionUnavailable: If `require_retention` and the ref document's history is not retained.
    """
    report = find_orphans(store, now=now, grace=grace, require_retention=require_retention)
    if not dry_run:
        for ident in report.orphans:
            store.backend.delete_immutable(store.pack_key(ident))
    return report
