"""The ref document: a repository's whole mutable state — refs plus the pack manifest — in one object.

Also the ref-name and object-id validators every writer passes through. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import json
import re

from themis.sheaf import errors

FORMAT_VERSION = 1

# Characters and sequences git rejects anywhere in a ref name (refs.c: check_refname_format). `\s`
# covers the two that wedge a repository — space and newline — because `git update-ref --stdin` is
# whitespace-delimited and newline-terminated.
_ILLEGAL_IN_REF = re.compile(r'(\.\.)|([:?\[\\^~\s*\]])|(@\{)|([\x00-\x1f\x7f])')
_REF_PREFIX = 'refs/'
# Matched with `fullmatch`: `$` also matches before a trailing newline, which is the one
# character that wedges `update-ref --stdin` and so the one this must not admit.
_OBJECT_ID = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')


def validate_ref_name(ref: str) -> str:
    """Return `ref` if it is a ref name sheaf will store.

    Accepts a subset of what `git check-ref-format` does: a name has to be fully qualified under
    `refs/`, because a ref outside it would sit in the document where no ordinary ref enumeration
    looks. Nothing else is stricter than git — a name git accepts and this rejects is a caller
    mistake about qualification, never about format.

    Validates the name's format only. Whether the ref exists, and whether the object it is being
    pointed at is in the pack set, cannot be answered without reading packs, so both stay caller
    contracts.

    Raises:
        InvalidRefName: If the name is not fully qualified, or git would reject its format.
    """
    if not ref.startswith(_REF_PREFIX):
        raise errors.InvalidRefName(f'{ref!r} is not fully qualified (must start with {_REF_PREFIX!r})')
    if _ILLEGAL_IN_REF.search(ref):
        raise errors.InvalidRefName(f'{ref!r} is not a valid git ref name')
    # Whole name, not per component: git rejects a trailing `.` only at the end of the refname, so
    # `refs/heads/a./b` is legal.
    if ref.endswith('.'):
        raise errors.InvalidRefName(f'{ref!r} may not end with a dot')
    for part in ref.split('/'):
        if not part:
            raise errors.InvalidRefName(f'{ref!r} has an empty path component')
        # Per component here: git applies both of these to every slash-separated component, so one
        # anchored regex over the whole name would accept `refs/heads/sub/.hidden`.
        if part.startswith('.') or part.endswith('.lock'):
            raise errors.InvalidRefName(f'{ref!r}: component {part!r} is not a valid git ref component')
    return ref


def validate_object_id(oid: str) -> str:
    """Return `oid` if it is a lowercase hexadecimal object id, of either hash length git defines.

    A format check only: both of git's hash lengths are well-formed here, and whether the object
    exists in the pack set cannot be answered without reading packs, so it stays a caller contract.

    Raises:
        InvalidRefName: If it is not. A malformed id wedges `update-ref` exactly as a bad name does.
    """
    if not _OBJECT_ID.fullmatch(oid):
        raise errors.InvalidRefName(f'{oid!r} is not an object id')
    return oid


@dataclasses.dataclass(frozen=True)
class RefDoc:
    """Refs plus the pack manifest, as stored.

    `sequence` counts accepted transitions and stays dense; the backend generation that guards the
    compare-and-swap does not.
    """

    refs: dict[str, str] = dataclasses.field(default_factory=dict)
    packs: tuple[str, ...] = ()
    sequence: int = 0
    updated_by: str = ''
    updated_at: float = 0.0
    format: int = FORMAT_VERSION

    def to_bytes(self) -> bytes:
        """Serialise deterministically, so identical state produces identical bytes."""
        payload: dict[str, object] = {
            'format': self.format,
            'sequence': self.sequence,
            'refs': dict(sorted(self.refs.items())),
            'packs': list(self.packs),
            'updated_by': self.updated_by,
            'updated_at': self.updated_at,
        }
        return json.dumps(payload, sort_keys=True, indent=2).encode() + b'\n'

    @classmethod
    def from_bytes(cls, data: bytes) -> RefDoc:
        """Parse a stored ref document.

        Every field `to_bytes` writes is required, and an absent one is damage rather than a
        default. An empty manifest serialises as `[]`, so a document with no `packs` key is not one
        this code wrote — and defaulting it to empty would make it parse as a repository whose
        history needs no packs, which is what a sweep deletes against.

        Raises:
            UnsupportedFormat: If the document was written by an incompatible format version.
            ValueError: If the bytes are not the JSON object this writes, which means the document
                is damaged rather than merely too new.
        """
        payload = json.loads(data)
        version = int(payload.get('format', 0))
        if version != FORMAT_VERSION:
            raise errors.UnsupportedFormat(f'unsupported ref document format {version}')
        try:
            return cls(
                refs=dict(payload['refs']),
                packs=tuple(payload['packs']),
                sequence=int(payload['sequence']),
                updated_by=str(payload['updated_by']),
                updated_at=float(payload['updated_at']),
                format=version,
            )
        except KeyError as e:
            raise ValueError(f'ref document is missing {e.args[0]!r}') from e

    def advance(
        self,
        *,
        refs: dict[str, str],
        packs: tuple[str, ...],
        updated_by: str,
        updated_at: float,
    ) -> RefDoc:
        """Return the successor document for an accepted transition."""
        return dataclasses.replace(
            self,
            refs=refs,
            packs=packs,
            sequence=self.sequence + 1,
            updated_by=updated_by,
            updated_at=updated_at,
        )
