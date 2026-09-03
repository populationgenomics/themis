"""Measuring the packs nothing references. Nothing here deletes anything.

A pack has to be uploaded before the compare-and-swap that names it, so a lost race leaves a pack no
manifest names; compaction leaves behind the packs it collapsed. Both are inert — no reader asks for
them — and reclaiming them safely against a publish in flight costs more than the bytes do, so they
stay. What this module gives an operator is the bill. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import re

from themis.sheaf import errors
from themis.sheaf import store as store_mod

_PACK_ID = re.compile(r'[0-9a-f]{64}')


@dataclasses.dataclass(frozen=True)
class Report:
    """What a measurement found: the manifest's packs, and everything else under the prefix."""

    live: tuple[str, ...]
    orphans: tuple[str, ...]
    live_bytes: int
    orphan_bytes: int


def measure(store: store_mod.Store) -> Report:
    """Classify every pack under the repository's prefix as named by the manifest or not.

    The manifest is read before the packs are listed. Every pack a manifest names was uploaded before
    the swap that named it, and nothing is ever deleted, so a listing taken afterwards holds all of
    them; the other order would report a publish landing in between as corruption. A pack that
    publish uploaded is counted as an orphan until the next measurement, which is what a meter is
    for. Objects under the prefix that are not packs are ignored.

    Raises:
        CorruptRepository: If the manifest names a pack the listing does not hold.
    """
    snapshot = store.read()
    listed = {info.key: info for info in store.backend.list_immutable(store.pack_prefix)}
    live = set(snapshot.packs)
    missing = sorted(ident for ident in live if store.pack_key(ident) not in listed)
    if missing:
        raise errors.CorruptRepository(f'{store.ref_key} names packs the store does not hold: {missing}')

    orphans = []
    orphan_bytes = 0
    for key, info in listed.items():
        ident = key.rsplit('/', 1)[-1].removesuffix('.pack')
        if not _PACK_ID.fullmatch(ident) or store.pack_key(ident) != key or ident in live:
            continue
        orphans.append(ident)
        orphan_bytes += info.size
    return Report(
        live=tuple(sorted(live)),
        orphans=tuple(sorted(orphans)),
        live_bytes=sum(listed[store.pack_key(ident)].size for ident in live),
        orphan_bytes=orphan_bytes,
    )
