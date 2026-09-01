"""Backend construction from a JSON-serialisable descriptor.

The pre-receive hook is a separate process spawned by git, so it cannot be handed a live backend
object; it gets a descriptor through the environment and rebuilds one. Keeping both directions here
means the hook and the server cannot disagree about which store they are talking to.
"""

from __future__ import annotations

from collections.abc import Mapping

from themis.sheaf import backend as backend_mod
from themis.sheaf.backends import local


def backend_from_descriptor(descriptor: Mapping[str, str]) -> backend_mod.Backend:
    """Rebuild a backend from `{'kind': ..., ...}`.

    Args:
        descriptor: A mapping as `descriptor_for` produces. `kind` selects the implementation; the
            remaining keys are its constructor arguments.

    Returns:
        A backend bound to the store the descriptor names.

    Raises:
        ValueError: If the kind is unknown.
        KeyError: If a key the kind requires is absent.
        ImportError: If the kind needs the GCS client and this build has none.
    """
    kind = descriptor.get('kind')
    if kind == 'local':
        return local.LocalBackend(descriptor['root'])
    if kind == 'gcs':
        # Deferred for import cost: a process on a local backend never loads the cloud client.
        from google.cloud import storage  # noqa: PLC0415

        from themis.sheaf.backends import gcs  # noqa: PLC0415

        client = storage.Client(project=descriptor.get('project'))
        return gcs.GcsBackend(client.bucket(descriptor['bucket']), descriptor.get('prefix', ''))
    raise ValueError(f'unknown backend kind {kind!r}')


def descriptor_for(backend: backend_mod.Backend) -> dict[str, str]:
    """Describe `backend` in a form `backend_from_descriptor` accepts.

    Raises:
        TypeError: If the backend has no descriptor form.
        ValueError: If a GCS backend is bound to a bucket with no name, which nothing the hook
            could be handed would be reachable through.
        ImportError: If this build has no GCS client, so no backend but the local one can be
            recognised.
    """
    if isinstance(backend, local.LocalBackend):
        return {'kind': 'local', 'root': str(backend.root)}

    from themis.sheaf.backends import gcs  # noqa: PLC0415

    if isinstance(backend, gcs.GcsBackend):
        if backend.bucket.name is None:
            raise ValueError('the GCS backend is bound to a bucket with no name')
        return {'kind': 'gcs', 'bucket': backend.bucket.name, 'prefix': backend.prefix}
    raise TypeError(f'no descriptor for {type(backend).__name__}')
