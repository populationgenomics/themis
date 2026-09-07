"""The guest's two doors to the Analysis repository: `git upload-pack` and `git receive-pack` over stream hatches.

The repository the agent works in is a sheaf repository behind the Sheaf service, and the guest reaches it through
stock git against a bare mirror at a host-only path (sheaf-changeover.md). Each hatch splices the guest's bytes to
one fixed service — the socket is the capability, so a guest holding the fetch socket cannot push — and before
handing a connection over the handler brings the mirror up to the store's current state, under one lock, so git's
own fast-forward check does the rejecting in the common case and a refused pusher's `git pull` converges (the
sync-before-serve argument of `themis.sheaf.wire.server`). The pre-receive hook then turns an accepted push into
one compare-and-swap on the store; postern scrubs the spawned git's environment, so the hook's inputs are passed
explicitly.
"""

from __future__ import annotations

import logging
import os
import pathlib
import threading
import uuid

import postern
from postern import stream

from themis.sheaf import store as store_mod
from themis.sheaf.wire import bare, hook, protect

UPLOAD_PACK = 'upload_pack'
RECEIVE_PACK = 'receive_pack'
_SERVICES = {UPLOAD_PACK: 'upload-pack', RECEIVE_PACK: 'receive-pack'}
# One connection per hatch at a time: a second dial waits in the kernel backlog, so two pushes cannot interleave
# their syncs and ref updates on one mirror. A fetch during a push still can; the store's compare-and-swap keeps
# the data right, and the worst case is a spurious error to the client.
_MAX_CONNECTIONS = 1

# What the guest may never write: its ignored scratch, and the skills the SDK lays down every spawn. The bare
# names too: a symlink committed at `skills` is recreated by the next session's clone, and the SDK resolves it
# before it writes.
PROTECTION = protect.Protection(paths=('scratch', 'scratch/**', 'skills', 'skills/**'))

_logger = logging.getLogger(__name__)


class GitHatches:
    """Two stream hatches over one bare mirror, each spawning the git service it is named for.

    `sync` is the only way the mirror is refreshed, and it serialises: sync writes refs, and two
    concurrent syncs of one mirror would race for no benefit. The store's compare-and-swap in the
    hook is the concurrency control for the data.
    """

    def __init__(
        self,
        mirror: bare.BareRepo,
        *,
        protection: protect.Protection,
        socket_dir: pathlib.Path,
    ) -> None:
        """Serve `mirror` to the guest.

        Args:
            mirror: The bare mirror of the Analysis repository, at a path the guest cannot see.
            protection: Paths a push may not write; reaches the hook through its environment.
            socket_dir: Where the two host-side sockets are bound. Traversable only by the worker
                and whichever uid bwrap runs as, because the socket's own mode is world-writable.
        """
        self.mirror = mirror
        self.protection = protection
        self._lock = threading.Lock()
        self.hatches = [
            stream.StreamHatch(
                self._handler(name), name=name, socket_path=socket_dir / f'{name}.sock', max_conns=_MAX_CONNECTIONS
            )
            for name in _SERVICES
        ]

    def sync(self) -> store_mod.Snapshot:
        """Bring the mirror up to the store's state, serialised against every other sync.

        Raises:
            CorruptRepository: If the store names a pack it no longer holds.
            SheafError: If the ref document cannot be read.
            RuntimeError: If git fails while writing the mirror.
        """
        with self._lock:
            return self.mirror.sync()

    def _handler(self, name: str) -> stream.Handler:
        service = _SERVICES[name]

        def handle(_stream: stream.Stream) -> stream.Process | None:
            try:
                state = self._synced_state()
            except Exception:
                # The request boundary: hydration raises from unrelated hierarchies (the backend's
                # SDK, git via RuntimeError, OSError). Uncaught, postern refuses the connection
                # silently; logged, the failure has a cause an operator can read.
                _logger.exception('mirror sync failed; refusing the %s connection', service)
                return None
            return stream.Process(['git', service, os.fspath(self.mirror.path)], env=self._git_env(state))

        return handle

    def _synced_state(self) -> pathlib.Path:
        """Sync, and give this connection its own copy of the sync state the hook reads.

        The mirror's own state file is rewritten by every sync, so a second connection's sync could
        replace the generation a still-running receive-pack's hook compares against; a copy taken
        under the lock pins the hook to the generation this connection's client saw.
        """
        with self._lock:
            self.mirror.sync()
            state = self.mirror.sync_state_path.read_bytes()
        path = self.mirror.path / f'{bare.SYNC_STATE_FILE}.{uuid.uuid4().hex}'
        path.write_bytes(state)
        return path

    def _git_env(self, state: pathlib.Path) -> dict[str, str]:
        # ext:: carries nothing of the client's protocol request, so the server side has to ask
        # for v2 itself: v2 is what tells a clone of an empty repository which branch HEAD names.
        return {
            **hook.environment(self.mirror, self.protection),
            hook.SYNC_STATE_ENV: os.fspath(state),
            'GIT_PROTOCOL': 'version=2',
        }

    def guest_urls(self, profile: postern.SandboxProfile) -> tuple[str, str]:
        """The `ext::` URLs a guest under `profile` reaches the hatches by: `(fetch, push)`."""
        return stream.git_url(UPLOAD_PACK, profile=profile), stream.git_url(RECEIVE_PACK, profile=profile)

    def close(self) -> None:
        """Stop serving both hatches and tear down whatever git they still have running."""
        for hatch in self.hatches:
            hatch.close()
