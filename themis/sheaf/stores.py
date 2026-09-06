"""Rebuild a repository from the descriptor `Repository.descriptor` produced.

The pre-receive hook is a separate process git spawns, so it cannot be handed a live repository;
the mirror writes a descriptor to the sync state file and the hook rebuilds one from it. Keeping the
inverse here, beside the backend descriptors it extends, means the mirror and its hook cannot
disagree about which store they are talking to.
"""

from __future__ import annotations

from collections.abc import Mapping

from themis.sheaf import backends
from themis.sheaf import store as store_mod

# The kind `themis.clients.sheaf.store.RemoteStore` describes itself as.
REMOTE_KIND = 'sheaf'


def from_descriptor(descriptor: Mapping[str, str]) -> store_mod.Repository:
    """Rebuild a repository from `{'kind': ..., 'repo': ..., ...}`.

    Args:
        descriptor: A mapping as `Repository.descriptor` produces. `kind` names a backend, or
            `sheaf` for a repository reached through the `Sheaf` service; the remaining keys are
            the implementation's.

    Raises:
        ValueError: If the kind is unknown, or a value is not one the kind accepts.
        KeyError: If a key the kind requires is absent.
        ImportError: If the kind needs a client this build has none of.
    """
    if descriptor.get('kind') == REMOTE_KIND:
        # Deferred: the remote store imports grpc and the generated stubs, which a process on a
        # local backend never needs.
        from themis.clients.sheaf import store as remote  # noqa: PLC0415

        return remote.RemoteStore.from_descriptor(descriptor)
    return store_mod.Store(backends.backend_from_descriptor(descriptor), descriptor['repo'])
