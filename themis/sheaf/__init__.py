"""Sheaf: git over object storage.

Git's object model already splits into an append-only content-addressed object store and a small
mutable pointer set, and an object store provides exactly those two primitives — immutable keys, and
compare-and-swap on one key. Nothing here tries to make an object store behave like a POSIX
filesystem.

This module re-exports the package's public API; `themis.sheaf.compact`, `themis.sheaf.orphans` and
`themis.sheaf.wire` are imported directly. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

from themis.sheaf.backend import Backend, Generation, ObjectInfo, StoredBlob
from themis.sheaf.backends.local import LocalBackend
from themis.sheaf.errors import (
    CorruptRepository,
    InvalidRefName,
    NotFound,
    PreconditionFailed,
    RaceLost,
    RefConflict,
    RefDeletionRefused,
    ReflogRequired,
    RetriesExhausted,
    SheafError,
)
from themis.sheaf.refdoc import DirectTarget, RefDoc, SymbolicTarget, Target
from themis.sheaf.store import Intent, RefUpdate, Snapshot, Store, pack_id

__all__ = [
    'Backend',
    'CorruptRepository',
    'DirectTarget',
    'Generation',
    'Intent',
    'InvalidRefName',
    'LocalBackend',
    'NotFound',
    'ObjectInfo',
    'PreconditionFailed',
    'RaceLost',
    'RefConflict',
    'RefDeletionRefused',
    'RefDoc',
    'RefUpdate',
    'ReflogRequired',
    'RetriesExhausted',
    'SheafError',
    'Snapshot',
    'Store',
    'StoredBlob',
    'SymbolicTarget',
    'Target',
    'pack_id',
]
