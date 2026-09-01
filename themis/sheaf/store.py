"""The publish protocol: upload packs, then compare-and-swap the ref document.

`Store` is the only writer of a repository's mutable state, and its compare-and-swap is the one
serialisation point in the system. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
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

    def head(self, ref: str) -> str | None:
        """Return the commit `ref` points at, or None if the ref is absent."""
        return self.doc.refs.get(ref)


@dataclasses.dataclass(frozen=True)
class RefUpdate:
    """A compare-and-swap on a single ref.

    `old` is None to require that the ref be absent; `new` is None to delete it.
    """

    old: str | None
    new: str | None


@dataclasses.dataclass(frozen=True)
class Intent:
    """What one transaction attempt wants to publish."""

    ref_updates: Mapping[str, RefUpdate]
    packs: Sequence[bytes] = dataclasses.field(default_factory=tuple)
    author: str = 'sheaf'


def pack_id(data: bytes) -> str:
    """Name a packfile by its contents.

    Content addressing means a replayed attempt that produces byte-identical objects re-uploads to
    the same key, and two writers racing on the same pack are not in conflict at all.
    """
    return hashlib.sha256(data).hexdigest()


class Store:
    """A single sheaf repository living under one key prefix."""

    def __init__(self, backend: backend_mod.Backend, repo: str, *, clock: Callable[[], float] = time.time) -> None:
        self.backend = backend
        self.repo = repo.strip('/')
        self._clock = clock

    @property
    def ref_key(self) -> str:
        """Key of the one mutable object."""
        return f'{self.repo}/refs.json'

    def pack_key(self, ident: str) -> str:
        """Key of a packfile."""
        return f'{self.repo}/packs/{ident}.pack'

    @property
    def pack_prefix(self) -> str:
        """Key prefix holding every packfile, live and orphaned."""
        return f'{self.repo}/packs/'

    def read(self) -> Snapshot:
        """Read the ref document, or an empty snapshot if the repository does not exist yet."""
        try:
            blob = self.backend.get_mutable(self.ref_key)
        except errors.NotFound:
            return Snapshot(doc=refdoc.RefDoc(), generation=None)
        return Snapshot(doc=refdoc.RefDoc.from_bytes(blob.data), generation=blob.generation)

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
            ValueError: If any retained generation cannot be parsed, whether it is damaged or was
                written by a format this code does not implement. Neither is skipped: garbage
                collection treats every retained document as naming live packs, so dropping one is
                how a sweep concludes history needs nothing and deletes a pack an older ref state
                cannot be hydrated without. A reader that can survive a gap — displaying the audit
                log, say — catches `UnsupportedFormat` for itself.
        """
        return [refdoc.RefDoc.from_bytes(blob.data) for blob in self.backend.history_mutable(self.ref_key)]

    def publish(self, base: Snapshot, intent: Intent) -> Snapshot:
        """Attempt one publish against the state in `base`.

        Raises:
            InvalidRefName: If a ref name or object id in `intent` is one git would reject.
                Checked before anything is uploaded.
            RefConflict: If a ref being updated does not hold its expected value in `base`.
            RaceLost: If the ref document advanced since `base` was read.
        """
        refs = dict(base.doc.refs)
        for ref, update in intent.ref_updates.items():
            refdoc.validate_ref_name(ref)
            for oid in (update.old, update.new):
                if oid is not None:
                    refdoc.validate_object_id(oid)
            actual = refs.get(ref)
            if actual != update.old:
                raise errors.RefConflict(ref, update.old, actual)

        # Objects before refs, always. A pack no ref names is litter `themis.sheaf.gc` reclaims.
        new_packs: list[str] = []
        for data in intent.packs:
            ident = pack_id(data)
            self.backend.put_immutable(self.pack_key(ident), data)
            if ident not in base.doc.packs and ident not in new_packs:
                new_packs.append(ident)

        for ref, update in intent.ref_updates.items():
            if update.new is None:
                refs.pop(ref, None)
            else:
                refs[ref] = update.new

        doc = base.doc.advance(
            refs=refs,
            packs=(*base.doc.packs, *new_packs),
            updated_by=intent.author,
            updated_at=self._clock(),
        )
        try:
            generation = self.backend.cas_mutable(self.ref_key, doc.to_bytes(), base.generation)
        except errors.PreconditionFailed as exc:
            raise errors.RaceLost(str(exc)) from exc
        return Snapshot(doc=doc, generation=generation)

    def replace_packs(self, base: Snapshot, packs: Sequence[bytes], *, author: str = 'compaction') -> Snapshot:
        """Publish a pack set that replaces the manifest rather than extending it.

        Compaction's write half. Refs are carried over untouched, and the compare-and-swap is made
        against `base`: a pack set built from an older snapshot would leave the ref document naming
        a commit that no pack contains. Superseded packs are left in place, so a reader mid-hydrate
        against the old manifest is unaffected.

        Raises:
            RaceLost: If the ref document advanced since `base` was read.
        """
        idents = []
        for data in packs:
            ident = pack_id(data)
            self.backend.put_immutable(self.pack_key(ident), data)
            idents.append(ident)

        doc = base.doc.advance(
            refs=dict(base.doc.refs),
            packs=tuple(idents),
            updated_by=author,
            updated_at=self._clock(),
        )
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
