"""Rolling many small packs into one, and the policy deciding when that is worth doing.

Nothing here deletes, and nothing anywhere does: a superseded pack stays fetchable for good, and
`themis.sheaf.orphans` counts what that costs. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import enum

from themis.sheaf import errors
from themis.sheaf import store as store_mod
from themis.sheaf.wire import bare

DEFAULT_MAX_PACKS = 16
DEFAULT_RATIO = 0.25


@dataclasses.dataclass(frozen=True)
class Policy:
    """When to compact.

    `ratio` is the ceiling on the combined size of the loose packs as a fraction of the largest
    one, which is what bounds write amplification however long the history gets. `max_packs` caps
    the manifest for a long run of tiny appends that stays under the ratio.
    """

    max_packs: int = DEFAULT_MAX_PACKS
    ratio: float = DEFAULT_RATIO


# Frozen and shared: a module-level singleton keeps it out of mutable argument defaults.
DEFAULT_POLICY = Policy()


@dataclasses.dataclass(frozen=True)
class Assessment:
    """Why compaction is or is not due."""

    packs: int
    base_bytes: int
    loose_bytes: int
    due: bool
    reason: str


def assess(store: store_mod.Store, snapshot: store_mod.Snapshot, policy: Policy = DEFAULT_POLICY) -> Assessment:
    """Decide whether `snapshot` is worth compacting.

    Sizes come from one listing rather than a request per pack, so this is cheap enough to run on
    every publish.

    Raises:
        CorruptRepository: If the document names a pack the store does not hold.

    Args:
        store: The repository the pack sizes are listed from.
        snapshot: The manifest being judged.
        policy: The thresholds to judge it against.

    Returns:
        The verdict, carrying the reason whichever way it went.
    """
    live = set(snapshot.packs)
    sizes = {
        info.key.removeprefix(store.pack_prefix).removesuffix('.pack'): info.size
        for info in store.backend.list_immutable(store.pack_prefix)
    }
    missing = live - set(sizes)
    if missing:
        raise errors.CorruptRepository(f'{store.ref_key} names packs the store does not hold: {sorted(missing)}')
    present = [sizes[ident] for ident in live]

    if len(live) <= 1:
        return Assessment(len(live), sum(present), 0, False, 'already one pack')

    base = max(present)
    loose = sum(present) - base
    if len(live) > policy.max_packs:
        return Assessment(len(live), base, loose, True, f'{len(live)} packs exceeds cap of {policy.max_packs}')
    if loose > base * policy.ratio:
        return Assessment(
            len(live), base, loose, True, f'{loose:,} B of loose packs exceeds {policy.ratio:.0%} of {base:,} B base'
        )
    return Assessment(len(live), base, loose, False, f'{loose:,} B loose against a {base:,} B base')


class Outcome(enum.Enum):
    """What a compaction attempt did."""

    NOT_DUE = 'not_due'
    NOTHING_TO_COLLAPSE = 'nothing_to_collapse'
    REPLACED = 'replaced'
    RACED = 'raced'


@dataclasses.dataclass(frozen=True)
class Compaction:
    """The result of one attempt, naming which of the four things happened.

    `snapshot` is the state the caller should treat as current: what the replacement produced, or
    the synced state where there was nothing to collapse. It is None only for `RACED`, where the
    winner's state has not been read, and for `NOT_DUE`, where nothing was synced.
    """

    outcome: Outcome
    snapshot: store_mod.Snapshot | None = None

    @property
    def replaced(self) -> bool:
        """Whether the manifest was actually rewritten."""
        return self.outcome is Outcome.REPLACED


def compact(store: store_mod.Store, mirror: bare.BareRepo) -> Compaction:
    """Replace the pack set with a single repacked pack.

    The compare-and-swap is made against the generation the mirror was synced to, and that is the
    whole correctness argument: a pack set built from an older snapshot would leave the ref document
    naming a commit no pack contains. A lost race is therefore not retried here.

    Args:
        store: The repository to compact.
        mirror: A bare mirror of `store`, synced and repacked in place.

    Returns:
        What happened, and the state to treat as current. A lost race leaves the repacked pack
        behind as an orphan, which is inert.
    """
    base = mirror.sync()
    if len(base.packs) <= 1:
        return Compaction(Outcome.NOTHING_TO_COLLAPSE, base)

    packs = {}
    for path in mirror.repack():
        packs[store_mod.pack_id(path.read_bytes())] = path
    try:
        after = store.replace_packs(base, [path.read_bytes() for path in packs.values()])
    except errors.RaceLost:
        # Somebody published during the repack; the next sync picks their state up. The markers are
        # left alone deliberately: the manifest still names the packs the repack consolidated, and
        # those markers are the only remaining copy of them.
        return Compaction(Outcome.RACED)
    # Winning is what makes the superseded packs unreferenced, so this is where they can go.
    mirror.adopt(packs)
    return Compaction(Outcome.REPLACED, after)


def compact_if_due(
    store: store_mod.Store,
    mirror: bare.BareRepo,
    *,
    policy: Policy = DEFAULT_POLICY,
) -> Compaction:
    """Compact only when `policy` says it is due.

    Args:
        store: The repository to compact.
        mirror: A bare mirror of `store`.
        policy: The thresholds the decision is made against.

    Returns:
        What happened. The four cases are distinct on the result, so a caller metering compaction
        or logging it reads them off directly rather than re-deriving them from `assess`.
    """
    verdict = assess(store, store.read(), policy)
    if not verdict.due:
        return Compaction(Outcome.NOT_DUE)
    return compact(store, mirror)
