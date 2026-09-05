"""The publish protocol: upload packs, then compare-and-swap the ref document.

`Store` is the only writer of a repository's mutable state, and its compare-and-swap is the one
serialisation point in the system. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
from collections.abc import Callable, Mapping, Sequence

from themis.sheaf import backend as backend_mod
from themis.sheaf import errors, refdoc

DEFAULT_RETRIES = 8


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """A consistent read of a repository's mutable state.

    `generation` is None when the repository has no ref document yet; publishing against that
    asserts the document must not exist, which is how repository-creation races are resolved.
    """

    doc: refdoc.RefDoc
    generation: backend_mod.Generation | None

    @property
    def refs(self) -> dict[str, str]:
        """Ref name to commit id."""
        return self.doc.refs

    @property
    def packs(self) -> tuple[str, ...]:
        """Pack ids that make up the object database."""
        return self.doc.packs

    def tip(self, ref: str) -> str | None:
        """Return the commit `ref` points at, or None if the ref is absent."""
        return self.doc.refs.get(ref)


@dataclasses.dataclass(frozen=True)
class RefUpdate:
    """A compare-and-swap on a single ref.

    `old` is None to require that the ref be absent. `new` is None to delete it, which the store
    refuses: history here is append-only. The field stays because a push arrives as `<old> <new>`
    lines and a deletion has to be representable to be refused with its name.
    """

    old: str | None
    new: str | None


@dataclasses.dataclass(frozen=True)
class Intent:
    """What one transaction attempt wants to publish.

    `packs` are uploaded by `Store.publish`; `stored_packs` are the ids of packs the writer has
    already put through `Store.put_pack`, so a writer receiving packs one at a time can store each
    as it completes and name them all in one publish. Both end up in the manifest alike; that a
    stored id names a pack the backend holds is the writer's contract, since checking would cost
    a round trip per pack — a manifest naming an absent pack is the one damage this store cannot undo.
    """

    ref_updates: Mapping[str, RefUpdate]
    packs: Sequence[bytes] = dataclasses.field(default_factory=tuple)
    stored_packs: Sequence[str] = dataclasses.field(default_factory=tuple)
    # None carries the document's existing HEAD over. A repository's first publish has none to carry,
    # so it must name one: nothing on a push tells the server which ref the client considers primary,
    # and a mirror left to guess re-guesses on every hydrate.
    head: refdoc.Target | None = None


def pack_id(data: bytes) -> str:
    """Name a packfile by its contents.

    Content addressing means a replayed attempt that produces byte-identical objects re-uploads to
    the same key, and two writers racing on the same pack are not in conflict at all.
    """
    return hashlib.sha256(data).hexdigest()


def moved_refs(ref_updates: Mapping[str, RefUpdate]) -> list[str]:
    """The refs `ref_updates` moves outside sheaf's own namespace, in name order.

    The reflog ref advances on every publish, so it says nothing about which publish a document
    holds; these refs are what a publish is about, and what it is classified by.
    """
    return sorted(ref for ref in ref_updates if not ref.startswith(refdoc.SHEAF_NAMESPACE))


def require_moved_refs(ref_updates: Mapping[str, RefUpdate]) -> list[str]:
    """`moved_refs`, refusing an intent that moves none.

    Not part of `Store.publish`: an in-process writer may legitimately publish only a HEAD, moving
    no ref at all. A publish arriving over the wire may not — nothing legitimate publishes only
    sheaf's bookkeeping — and a classification over no refs would call every stale publish landed.

    Raises:
        BookkeepingOnly: If every ref in `ref_updates` is under `refs/sheaf/`.
    """
    moved = moved_refs(ref_updates)
    if not moved:
        raise errors.BookkeepingOnly(refdoc.SHEAF_NAMESPACE)
    return moved


class Verdict(enum.Enum):
    """What the live document says about a publish built against a generation it has left."""

    # Every moved ref already holds its `new`: this publish landed and only its response was lost.
    LANDED = 'landed'
    # Every moved ref still holds its `old`: an unrelated publish won; rebuild against the live
    # document and publish again.
    LOST_RACE = 'lost_race'
    # A moved ref holds neither: it moved under the caller, whose view of it is behind.
    REF_MOVED = 'ref_moved'


@dataclasses.dataclass(frozen=True)
class Classification:
    """A `Verdict` and the refs it rests on: every moved ref, or for `REF_MOVED` the ones that moved."""

    verdict: Verdict
    refs: tuple[str, ...]


def classify(live_refs: Mapping[str, str], ref_updates: Mapping[str, RefUpdate]) -> Classification:
    """Classify a publish whose base generation the document has moved past.

    Over the refs the intent moves outside `refs/sheaf/`, in a fixed order: landed if every one
    already holds its `new`; a lost race if every one still holds its `old`; otherwise a ref moved
    under the caller. Pure: the caller reads the live document and decides what each verdict means.
    `ref_updates` is taken as already validated — a deletion, whose `new` is None, would otherwise
    read as landed against an absent ref.

    Args:
        live_refs: The current document's refs.
        ref_updates: The intent's updates.

    Raises:
        BookkeepingOnly: If the intent moves no ref outside `refs/sheaf/`.
    """
    moved = tuple(require_moved_refs(ref_updates))
    if all(live_refs.get(ref) == ref_updates[ref].new for ref in moved):
        return Classification(Verdict.LANDED, moved)
    if all(live_refs.get(ref) == ref_updates[ref].old for ref in moved):
        return Classification(Verdict.LOST_RACE, moved)
    return Classification(Verdict.REF_MOVED, tuple(ref for ref in moved if live_refs.get(ref) != ref_updates[ref].old))


def validate_intent(intent: Intent) -> None:
    """Refuse what is wrong with `intent` on its own, before any document is read.

    The checks that need the document — that each `old` matches, that the resulting ref set is one
    git can store — are `Store.plan`'s, which runs this first.

    Raises:
        InvalidRefName: If a ref name or object id, or the HEAD it names, is one git would reject.
        RefDeletionRefused: If an update deletes a ref.
        ReflogRequired: If an update moves a ref outside sheaf's own namespace without also
            advancing the reflog ref.
        InvalidPackId: If a stored pack id is not one this store forms.
    """
    if intent.head is not None:
        refdoc.validate_target(intent.head)
    for ref, update in intent.ref_updates.items():
        refdoc.validate_ref_name(ref)
        if update.new is None:
            raise errors.RefDeletionRefused(ref)
        for oid in (update.old, update.new):
            if oid is not None:
                refdoc.validate_object_id(oid)
    moved = moved_refs(intent.ref_updates)
    if moved and refdoc.REFLOG_REF not in intent.ref_updates:
        raise errors.ReflogRequired(moved)
    for ident in intent.stored_packs:
        refdoc.validate_pack_id(ident)


_BRANCH_PREFIX = 'refs/heads/'
_DEFAULT_BRANCH = 'refs/heads/main'


def _head_for(refs: Mapping[str, str]) -> refdoc.Target:
    """Choose a HEAD from the refs a publish leaves behind.

    Nothing on a push says which ref the client considers primary — receive-pack sees ref updates,
    not the client's HEAD — so the branches that exist are the best evidence available, and `main`
    wins where several do. With no branch at all, HEAD is an unborn `main`, which is what `git init`
    does; a tag is never HEAD, because a clone of one lands detached. The guess is made once and
    recorded, rather than re-derived on every hydrate by whatever build is running, with different
    builds free to disagree about the same repository. A caller that knows better says so through
    `Intent.head`.
    """
    branches = sorted(ref for ref in refs if ref.startswith(_BRANCH_PREFIX))
    if not branches or _DEFAULT_BRANCH in branches:
        return refdoc.SymbolicTarget(_DEFAULT_BRANCH)
    return refdoc.SymbolicTarget(branches[0])


def _carry_head(head: refdoc.Target | None, refs: Mapping[str, str]) -> refdoc.Target | None:
    """Carry `head` over, unless it is unborn and a branch now exists to take it.

    Refs are never deleted, so the only HEAD that does not resolve is the unborn `main` a repository
    with no branch was given. Once a branch exists, a clone should land on it.
    """
    dangling = isinstance(head, refdoc.SymbolicTarget) and head.ref not in refs
    if dangling and any(ref.startswith(_BRANCH_PREFIX) for ref in refs):
        return None
    return head


class Store:
    """A single sheaf repository living under one key prefix."""

    def __init__(self, backend: backend_mod.Backend, repo: str) -> None:
        self.backend = backend
        self.repo = repo.strip('/')

    @property
    def ref_key(self) -> str:
        """Key of the one mutable object."""
        return f'{self.repo}/refs.pb'

    def pack_key(self, ident: str) -> str:
        """Key of a packfile."""
        return f'{self.repo}/packs/{ident}.pack'

    @property
    def pack_prefix(self) -> str:
        """Key prefix holding every packfile, live and orphaned."""
        return f'{self.repo}/packs/'

    def read(self) -> Snapshot:
        """Read the ref document, or an empty snapshot if the repository does not exist yet.

        Raises:
            CorruptRepository: If the stored document is structurally not one this code wrote — no
                HEAD, a target naming neither arm, a ref that is symbolic. Names and ids are
                validated at publish, not here (`docs/design/sheaf.md`, Consequences).
        """
        try:
            blob = self.backend.get_mutable(self.ref_key)
        except errors.NotFound:
            return Snapshot(doc=refdoc.RefDoc(), generation=None)
        return Snapshot(doc=self._decode(blob.data), generation=blob.generation)

    def _decode(self, data: bytes) -> refdoc.RefDoc:
        try:
            doc = refdoc.RefDoc.from_bytes(data)
            _ = doc.refs
        except ValueError as exc:
            raise errors.CorruptRepository(f'{self.ref_key}: {exc}') from exc
        return doc

    def fetch_pack(self, ident: str) -> bytes:
        """Download one packfile.

        Raises:
            NotFound: If the pack is absent.
        """
        return self.backend.get_immutable(self.pack_key(ident))

    def transitions(self) -> list[refdoc.RefDoc]:
        """Return every retained ref document, newest first.

        Where the backend retains prior generations this is the durable reflog. Git's own reflog
        lives in a per-session host store and dies with it, so this is the only copy that survives.

        Returns:
            Every retained document, newest first.

        Raises:
            CorruptRepository: If any retained generation cannot be read. Not skipped: a reader of
                the audit log that could tolerate a gap catches it for itself, and one that quietly
                dropped a generation would misreport what the refs were.
        """
        return [self._decode(blob.data) for blob in self.backend.history_mutable(self.ref_key)]

    def plan(self, base: Snapshot, intent: Intent) -> refdoc.RefDoc:
        """The document `publish` would write for `intent` against `base`, or the refusal it would raise.

        Nothing is uploaded and nothing is written: this is `publish`'s validation, exposed so a
        caller can refuse an intent — and measure the document it would leave — before it holds
        the packs.

        Raises:
            InvalidRefName: If a ref name or object id in `intent`, or the HEAD it names, is one git
                would reject, or two names in the resulting ref set cannot coexist.
            RefDeletionRefused: If an update deletes a ref. Whether an update rewrites one is not
                checkable here — the store holds no objects — so that is the writer's contract: the
                hook checks ancestry, and a direct writer builds its commit on the tip it publishes
                against, which is a fast-forward by construction.
            ReflogRequired: If an update moves a ref outside sheaf's own namespace without also
                advancing the reflog ref. That the entry's parents include the new tips is the
                writer's contract, like fast-forwardness; that an entry exists is checked here.
            InvalidPackId: If a stored pack id is not one this store forms.
            RefConflict: If a ref being updated does not hold its expected value in `base`. Compared
                against `base` and not the live document, so a writer deriving every `old` from the
                snapshot it publishes against — the reflog's included — never sees this, and a stale
                snapshot surfaces as `RaceLost` from the compare-and-swap instead.
        """
        validate_intent(intent)
        refs = dict(base.doc.refs)
        for ref, update in intent.ref_updates.items():
            actual = refs.get(ref)
            if actual != update.old:
                raise errors.RefConflict(ref, update.old, actual)
            if update.new is not None:  # validate_intent refused None; the check narrows the type
                refs[ref] = update.new
        refdoc.validate_ref_set(refs)
        head = intent.head or _carry_head(base.doc.head, refs) or _head_for(refs)
        packs = (*base.doc.packs, *(pack_id(data) for data in intent.packs), *intent.stored_packs)
        return base.doc.advance(refs=refs, packs=packs, head=head)

    def put_pack(self, data: bytes) -> str:
        """Upload one packfile under its content id and return that id.

        For a writer that receives packs one at a time: store each as it completes, then name them
        all through `Intent.stored_packs`. A pack no document names yet is inert litter, counted by
        `themis.sheaf.orphans`, so a publish that never follows costs bytes and nothing else.
        """
        ident = pack_id(data)
        self.backend.put_immutable(self.pack_key(ident), data)
        return ident

    def publish(self, base: Snapshot, intent: Intent) -> Snapshot:
        """Attempt one publish against the state in `base`.

        Raises:
            InvalidRefName: As `plan`. Checked before anything is uploaded.
            RefDeletionRefused: As `plan`.
            ReflogRequired: As `plan`.
            InvalidPackId: As `plan`.
            RefConflict: As `plan`.
            RaceLost: If the ref document advanced since `base` was read.
        """
        doc = self.plan(base, intent)
        # Objects before refs, always. A pack no ref names is inert litter, counted by `themis.sheaf.orphans`.
        for data in intent.packs:
            self.put_pack(data)
        try:
            generation = self.backend.cas_mutable(self.ref_key, doc.to_bytes(), base.generation)
        except errors.PreconditionFailed as exc:
            raise errors.RaceLost(str(exc)) from exc
        return Snapshot(doc=doc, generation=generation)

    def replace_packs(self, base: Snapshot, packs: Sequence[bytes]) -> Snapshot:
        """Publish a pack set that replaces the manifest rather than extending it.

        Compaction's write half. Refs are carried over untouched, and the compare-and-swap is made
        against `base`: a pack set built from an older snapshot would leave the ref document naming
        a commit that no pack contains. Superseded packs stay where they are — nothing is ever deleted
        — so a reader mid-hydrate against the old manifest is unaffected.

        Raises:
            ValueError: If the document names no HEAD, which a compaction cannot supply.
            RaceLost: If the ref document advanced since `base` was read.
        """
        head = base.doc.head
        if head is None:
            raise ValueError(f'{self.ref_key}: cannot compact a document that names no HEAD')
        idents = []
        for data in packs:
            ident = pack_id(data)
            self.backend.put_immutable(self.pack_key(ident), data)
            idents.append(ident)

        doc = base.doc.advance(refs=dict(base.doc.refs), packs=tuple(idents), head=head)
        try:
            generation = self.backend.cas_mutable(self.ref_key, doc.to_bytes(), base.generation)
        except errors.PreconditionFailed as exc:
            raise errors.RaceLost(str(exc)) from exc
        return Snapshot(doc=doc, generation=generation)

    def transact(
        self,
        build: Callable[[Snapshot], Intent | None],
        *,
        retries: int = DEFAULT_RETRIES,
    ) -> Snapshot:
        """Read, build an intent, publish; replay on a lost race.

        Args:
            build: Called afresh on every attempt and expected to derive its intent from the
                snapshot it is handed. A builder that does so can never see a `RefConflict`,
                because it recomputes against whichever writer won; a builder that hard-codes an
                expected old value surfaces one instead. Returning None means there is nothing to
                do, and the current snapshot is returned.
            retries: How many lost races to replay before giving up.

        Returns:
            The snapshot the accepted publish produced, or the snapshot `build` declined to act on.

        Raises:
            RetriesExhausted: If the race is lost `retries` times.
            RefConflict: Propagated from `publish`, never retried.
            InvalidRefName: Propagated from `publish`, never retried.
        """
        for _attempt in range(retries):
            base = self.read()
            intent = build(base)
            if intent is None:
                return base
            try:
                return self.publish(base, intent)
            except errors.RaceLost:
                continue
        raise errors.RetriesExhausted(f'{self.ref_key}: lost the race {retries} times')
