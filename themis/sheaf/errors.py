"""Typed errors, one per failure a caller has to handle differently.

Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations


class SheafError(Exception):
    """Base class for every error this package raises."""


class NotFound(SheafError):
    """A key does not exist in the backend."""


class PreconditionFailed(SheafError):
    """A compare-and-swap write was rejected because the generation did not match.

    Raised by the backend layer. `Store` translates it into `RaceLost`.
    """


class RaceLost(SheafError):
    """Another writer advanced the ref document between the caller's read and its write.

    Retrying is safe: re-read, rebuild, publish again.
    """


class RefConflict(SheafError):
    """A ref does not hold the value the caller expected.

    The storage-layer equivalent of git's `! [rejected] ... (fetch first)`. Resolving it takes a
    merge or a rebase by whoever is pushing, not a retry.
    """

    def __init__(self, ref: str, expected: str | None, actual: str | None) -> None:
        super().__init__(f'{ref}: expected {expected or "(absent)"}, found {actual or "(absent)"}')
        self.ref = ref
        self.expected = expected
        self.actual = actual


class InvalidRefName(SheafError):
    """A ref name or object id that git could not accept.

    Raised at publish rather than on read: `git update-ref --stdin` is whitespace-delimited and
    newline-terminated, so a name containing a space or a newline makes every subsequent sync of
    that repository fail, and the repository cannot be cloned or pushed to again until the bad
    entry is compare-and-swapped back out of the ref document.
    """


class InvalidPackId(SheafError):
    """A pack id that is not sixty-four lowercase hex digits, so not a key this store will form."""


class BookkeepingOnly(SheafError):
    """A publish that moves no ref outside sheaf's own namespace.

    Nothing legitimate publishes only sheaf's bookkeeping, and the classification of a publish
    against a moved document is over exactly the refs outside it, so one moving none has nothing
    to be classified by.
    """

    def __init__(self, namespace: str) -> None:
        super().__init__(f'a publish must move a ref outside {namespace}')


class RefDeletionRefused(SheafError):
    """A publish tried to delete a ref. History in a sheaf repository is append-only."""

    def __init__(self, ref: str) -> None:
        super().__init__(f'{ref} may not be deleted: history here is append-only')
        self.ref = ref


class ReflogRequired(SheafError):
    """A publish moved a ref without advancing the reflog ref alongside it.

    The reflog is what says which commit was current when; a publish that forgets it leaves a gap
    nobody notices until the log is read, so the store refuses rather than trusting every writer to
    remember.
    """

    def __init__(self, refs: list[str]) -> None:
        super().__init__(f'a publish moving {", ".join(refs)} must also advance the reflog ref')
        self.refs = refs


class RetriesExhausted(SheafError):
    """A transaction lost the race more times than the retry budget allows."""


class CorruptRepository(SheafError):
    """The stored state cannot be served, and retrying cannot help.

    Either the ref document is not one this code wrote — no HEAD, a target naming neither arm, a ref
    that is symbolic — or it names a pack that does not exist. The second is distinct from losing a
    race with a compaction, which looks the same to a fetch and is benign: a reader that meets a
    missing pack re-reads the document, and only an *unmoved* document makes the absence a fact about
    the store rather than about timing.
    """
