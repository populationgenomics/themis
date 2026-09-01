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


class RetentionUnavailable(SheafError):
    """The backend is not retaining prior generations of the ref document.

    Raised by garbage collection, which needs that history to know which packs a historical ref
    state still needs; unretained is not the same as unreachable.
    """


class RetriesExhausted(SheafError):
    """A transaction lost the race more times than the retry budget allows."""


class UnsupportedFormat(SheafError, ValueError):
    """A ref document was written by a format version this code does not implement.

    Distinct from a corrupt document, which stays an ordinary parse error, so a reader that can
    survive a gap in the history can tell "too new to parse" from "damaged" and skip only the first.
    Nothing skips either today: garbage collection reads the whole history and needs all of it. Also
    a `ValueError`, so a caller that knows only the standard hierarchy still catches it.
    """
