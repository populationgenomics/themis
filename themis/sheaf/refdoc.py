"""The ref document: a repository's whole mutable state — refs plus the pack manifest — in one object.

Stored as binary proto, and carried around as the parsed message rather than a projection of it, so
a field this build does not model survives being read and written back (`docs/design/proto.md`,
"Read-modify-write and integrity"). Every accepted transition rewrites the document and the backend
retains the prior generations, so a reader meets documents from every version that has ever run and
cannot rewrite them.

Also the ref-name and object-id validators every writer passes through. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Mapping
from typing import override

from google.protobuf import message as message_mod
from google.protobuf import unknown_fields

from themis.sheaf import errors
from themis.sheaf.models import refdoc_pb2

# Characters and sequences git rejects anywhere in a ref name (refs.c: check_refname_format). `\s`
# covers the two that wedge a repository — space and newline — because `git update-ref --stdin` is
# whitespace-delimited and newline-terminated.
# `re.ASCII`: git treats only ASCII whitespace as illegal; unicode `\s` would also refuse NBSP.
_ILLEGAL_IN_REF = re.compile(r'(\.\.)|([:?\[\\^~\s*])|(@\{)|([\x00-\x1f\x7f])', re.ASCII)
_REF_PREFIX = 'refs/'
# Refs sheaf writes for itself. A publish that moves any ref outside this namespace must also advance
# the reflog ref, and nothing but sheaf may write under it.
SHEAF_NAMESPACE = 'refs/sheaf/'
REFLOG_REF = SHEAF_NAMESPACE + 'reflog'
# `check-ref-format` accepts any component length, but the files backend writes `<name>.lock` and
# NAME_MAX is 255 on every filesystem git is deployed on.
_MAX_COMPONENT_BYTES = 255 - len('.lock')
# Matched with `fullmatch`: `$` also matches before a trailing newline, which is the one
# character that wedges `update-ref --stdin` and so the one this must not admit.
_OBJECT_ID = re.compile(r'[0-9a-f]{40}')
# Git's marker for "no object" — the `old` of a ref being created, the `new` of one being deleted.
ZERO_OBJECT_ID = '0' * 40
# A pack's content hash, and so its key: lowercase hex SHA-256.
_PACK_ID = re.compile(r'[0-9a-f]{64}')


def validate_ref_name(ref: str) -> str:
    """Return `ref` if it is a ref name sheaf will store.

    Accepts a subset of what `git check-ref-format` does. A name has to be fully qualified under
    `refs/`, because a ref outside it would sit in the document where no ordinary ref enumeration
    looks; and a component has to fit a filesystem name once git appends `.lock`, which
    `check-ref-format` does not check but `update-ref` cannot get past.

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
        if len(part.encode()) > _MAX_COMPONENT_BYTES:
            raise errors.InvalidRefName(f'{ref!r}: component {part[:20]!r}... is too long for a lock file')
    return ref


def validate_ref_set(refs: Iterable[str]) -> None:
    """Raise if two names in `refs` cannot coexist in one repository.

    Git stores refs as files under directories, so `refs/heads/a` and `refs/heads/a/b` cannot both
    exist. Each name passes `validate_ref_name` on its own; only the set reveals the conflict. Git
    detects it in the ref transaction, which runs *after* the pre-receive hook, so a publish that
    admitted both would already be committed by the time git refused the push.

    Raises:
        InvalidRefName: If one name is a path prefix of another.
    """
    names = set(refs)
    for name in names:
        parts = name.split('/')
        for depth in range(1, len(parts)):
            parent = '/'.join(parts[:depth])
            if parent in names:
                raise errors.InvalidRefName(
                    f'{name!r} and {parent!r} cannot both exist: one is a directory of the other'
                )


def validate_object_id(oid: str) -> str:
    """Return `oid` if it is a lowercase hexadecimal SHA-1 object id.

    SHA-1 only: the bare mirror uses git's default object format, and a 64-hex id fed to it is
    refused by `update-ref`. A format check only — whether the object exists in the pack set cannot
    be answered without reading packs, so that stays a caller contract.

    Raises:
        InvalidRefName: If it is not, or it is the zero id. A malformed id wedges `update-ref`
            exactly as a bad name does; the zero id is git's "absent" marker, not an object it can
            hold, and a writer relaying a push maps it to None before the store sees it.
    """
    if not _OBJECT_ID.fullmatch(oid):
        raise errors.InvalidRefName(f'{oid!r} is not an object id')
    if oid == ZERO_OBJECT_ID:
        raise errors.InvalidRefName('the zero id marks an absent ref; it is not an object id')
    return oid


def validate_pack_id(ident: str) -> str:
    """Return `ident` if it is a pack id sheaf will store: sixty-four lowercase hex digits.

    A pack id becomes part of an object key and an entry in the manifest, so its form is fixed here
    rather than left to whichever backend receives it.

    Raises:
        InvalidPackId: If it is anything else.
    """
    if not _PACK_ID.fullmatch(ident):
        raise errors.InvalidPackId(f'{ident!r} is not a pack id (sixty-four lowercase hex digits)')
    return ident


@dataclasses.dataclass(frozen=True)
class DirectTarget:
    """A ref naming an object."""

    oid: str


@dataclasses.dataclass(frozen=True)
class SymbolicTarget:
    """A ref naming another ref.

    The named ref need not currently resolve. HEAD on a branch with no commits is git's own state
    after `init`, and protocol v2 carries it so a clone of an empty repository checks out the
    intended branch name rather than the server's default.
    """

    ref: str


Target = DirectTarget | SymbolicTarget


def validate_target(target: Target) -> Target:
    """Return `target` if git would accept what it names.

    Raises:
        InvalidRefName: If a symbolic target does not name a ref sheaf stores, or a direct one is
            not an object id git would accept.
    """
    if isinstance(target, SymbolicTarget):
        validate_ref_name(target.ref)
    else:
        validate_object_id(target.oid)
    return target


def _carries_unknown(message: message_mod.Message) -> bool:
    """Whether `message` or anything nested in it holds a field this build does not model.

    Recursive because the interesting place for a later build to put one is inside a ref's target,
    and an unknown-field set belongs to the message that carried it — a top-level read sees nothing
    of a map value's.
    """
    # `UnknownFieldSet`, not `Message.UnknownFields()`: the latter is unimplemented under upb.
    if len(unknown_fields.UnknownFieldSet(message)):
        return True
    for field, value in message.ListFields():
        if field.message_type is None:
            continue
        if field.message_type.GetOptions().map_entry:
            nested = value.values() if field.message_type.fields_by_name['value'].message_type else ()
        elif field.is_repeated:
            nested = value
        else:
            nested = (value,)
        if any(_carries_unknown(item) for item in nested):
            return True
    return False


def read_target(message: refdoc_pb2.RefTarget) -> Target:
    """Decode a target, stored or received.

    Raises:
        ValueError: If neither arm is set — a target this code did not write, or a caller's that
            names nothing.
    """
    which = message.WhichOneof('target')
    if which == 'oid':
        return DirectTarget(message.oid)
    if which == 'ref':
        return SymbolicTarget(message.ref)
    raise ValueError('a ref target names neither an object nor a ref')


def _write_target(message: refdoc_pb2.RefTarget, target: Target) -> None:
    """Encode a target into `message`."""
    if isinstance(target, SymbolicTarget):
        message.ref = target.ref
    else:
        message.oid = target.oid


class RefDoc:
    """Refs, the pack manifest, and where a clone starts, as stored.

    Wraps the stored message rather than copying its fields out, because the fields this build does
    not know about are the ones worth keeping: they live in the message's unknown-field set and are
    re-serialised untouched. A projection would drop them, and an older writer would then quietly
    delete a newer one's state on its next publish.
    """

    def __init__(self, message: refdoc_pb2.RefDoc | None = None) -> None:
        self._message = refdoc_pb2.RefDoc()
        if message is not None:
            self._message.CopyFrom(message)
            if self._message.HasField('head'):
                read_target(self._message.head)

    @property
    def refs(self) -> dict[str, str]:
        """Ref name to the object id it names.

        Raises:
            ValueError: If a ref names another ref. Git allows it and the encoding admits it, but no
                writer here produces one and no reader here resolves one — so a document carrying
                one was written by a build that understands something this one does not, and
                answering with a partial ref set would be worse than refusing.
        """
        refs = {}
        for name, target in self._message.refs.items():
            decoded = read_target(target)
            if isinstance(decoded, SymbolicTarget):
                raise ValueError(f'{name} names another ref, which this build does not resolve')
            refs[name] = decoded.oid
        return refs

    @property
    def packs(self) -> tuple[str, ...]:
        """Every packfile the object database is made of, by content hash."""
        return tuple(self._message.packs)

    @property
    def head(self) -> Target | None:
        """Where a clone should start, or None on the synthesised document of a repository with none."""
        return read_target(self._message.head) if self._message.HasField('head') else None

    @property
    def carries_unmodelled_state(self) -> bool:
        """Whether the document holds a field this build does not know about.

        Preservation keeps such a field's bytes through a publish, but a reader still cannot act on
        one — so anything deciding from the document's *contents* has to know it is working from a
        partial view. Collection is the case that matters: it deletes on the basis of what it did
        not find, so any new place a reference might live is a new input to it.
        """
        return _carries_unknown(self._message)

    @override
    def __eq__(self, other: object) -> bool:
        return isinstance(other, RefDoc) and self._message == other._message

    @override
    def __hash__(self) -> int:
        # Over the modelled fields, not the bytes: equality compares the unknown-field set as a set
        # while serialisation emits it in parse order, so two equal documents carrying the same
        # unmodelled fields in different orders would hash apart.
        return hash((tuple(sorted(self._message.refs)), self.packs, self.head))

    @override
    def __repr__(self) -> str:
        return f'RefDoc(refs={sorted(self._message.refs)}, packs={self.packs}, head={self.head})'

    def to_bytes(self) -> bytes:
        """Serialise for storage, unknown fields included."""
        return self._message.SerializeToString(deterministic=True)

    def to_message(self) -> refdoc_pb2.RefDoc:
        """A copy of the stored message, unknown fields included, for a caller carrying it on the wire."""
        message = refdoc_pb2.RefDoc()
        message.CopyFrom(self._message)
        return message

    @classmethod
    def from_bytes(cls, data: bytes) -> RefDoc:
        """Parse a stored ref document.

        Every stored document names a HEAD, because `publish` will not write one that does not. That
        is what separates a document this code wrote from an empty or truncated encoding — both
        otherwise valid `RefDoc`s naming no packs, which is the shape a sweep deletes against. HEAD
        carries the highest field number, so a truncation loses it before it loses the manifest.

        Raises:
            ValueError: If the bytes are not a document this code wrote. Not a version this build is
                too old for: evolution is additive and unknown fields survive, so there is no version
                to be too old for.
        """
        message = refdoc_pb2.RefDoc()
        try:
            message.ParseFromString(data)
        except message_mod.DecodeError as e:
            raise ValueError(f'ref document is not a RefDoc: {e}') from e
        if not message.HasField('head'):
            raise ValueError('ref document names no HEAD')
        read_target(message.head)  # a HEAD naming neither an object nor a ref is not one either
        return cls(message)

    def advance(self, *, refs: Mapping[str, str], packs: Iterable[str], head: Target) -> RefDoc:
        """Return the successor document for an accepted transition.

        Built by copying this document and overwriting what the transition changes, so anything it
        carries that this build does not model comes along.

        Args:
            refs: The whole ref set after the transition, not a delta.
            packs: The whole manifest after the transition. Stored sorted and deduplicated, since it
                is a set and identical state should serialise identically.
            head: Where a clone should start.
        """
        message = refdoc_pb2.RefDoc()
        message.CopyFrom(self._message)
        # Assigned rather than merged: a ref or a pack the transition drops has to disappear, and
        # merging a map or a repeated field unions it with what is already there.
        # Mutated in place, never rebuilt: a map value is a message, and replacing it drops any field
        # inside it this build does not model — the loss this whole wrapper exists to prevent, one
        # level down. Setting the arm on an existing entry leaves everything else in it alone.
        for gone in set(message.refs) - set(refs):
            del message.refs[gone]
        for name, oid in refs.items():
            message.refs[name].oid = oid
        message.ClearField('packs')
        message.packs.extend(sorted(set(packs)))
        _write_target(message.head, head)
        return RefDoc(message)
