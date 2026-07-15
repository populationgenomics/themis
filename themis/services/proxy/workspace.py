"""Pack and restore the sandbox's ``/workspace``, with hardened extraction (self-hosted-sandbox.md §9).

The ephemeral scratch is an opaque tar archive synced through the store. The agent controls
``/workspace``, so it can craft the archive a checkpoint uploads — extraction must not become an
arbitrary-write primitive against the credential-holding proxy. The PEP 706 ``data`` filter confines
every entry to the destination: it rejects a ``..`` traversal and an escaping symlink/hardlink target,
and strips a leading slash (an absolute member lands inside the destination). The entry-count and size
caps bound resource use. The reader is uncompressed-only (``pack`` writes uncompressed), so a
compressed or malformed archive — a compromised store's decompression bomb — is rejected at open,
before any decompression. The checkpoint tars without dereferencing symlinks.
"""

from __future__ import annotations

import io
import pathlib
import tarfile
from collections.abc import Container

_MAX_ENTRIES = 20_000
_MAX_TOTAL_BYTES = 512 * 1024 * 1024  # 512 MiB


class UnsafeArchiveError(Exception):
    """The archive contained an entry that escapes the destination or exceeds a resource cap."""


def pack(root: pathlib.Path, *, exclude: Container[pathlib.Path]) -> bytes:
    """Tar ``root`` recursively into bytes, skipping ``exclude`` paths, symlinks stored as symlinks."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', dereference=False) as tar:
        for path in sorted(root.rglob('*')):
            if path in exclude or any(parent in exclude for parent in path.parents):
                continue
            tar.add(path, arcname=str(path.relative_to(root)), recursive=False)
    return buffer.getvalue()


def unpack(archive: bytes, dest: pathlib.Path) -> None:
    """Extract ``archive`` into ``dest``, capping resource use and confining every entry to ``dest``.

    ``mode='r:'`` matches ``pack``'s uncompressed output, so a compressed or malformed archive is
    rejected at open before any decompression. The caps are enforced before extraction; the PEP 706
    ``data`` filter rejects any entry that escapes ``dest`` (``..`` traversal, escaping links).

    Raises:
        UnsafeArchiveError: On an entry-count or size cap breach, an escaping entry, or a malformed
            archive.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tar:
            members = tar.getmembers()
            if len(members) > _MAX_ENTRIES:
                raise UnsafeArchiveError(f'too many entries: {len(members)}')
            total = sum(member.size for member in members)
            if total > _MAX_TOTAL_BYTES:
                raise UnsafeArchiveError(f'archive exceeds the decompressed-size cap: {total} bytes')
            tar.extractall(dest, members=members, filter='data')  # PEP 706 data filter confines entries to dest
    except tarfile.TarError as e:
        raise UnsafeArchiveError(str(e)) from e
