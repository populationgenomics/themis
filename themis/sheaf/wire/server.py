"""Smart HTTP, served by `git http-backend`.

Sheaf implements no wire protocol: stock git owns every byte on it, and this module is a CGI bridge
plus two pieces of policy — sync the bare mirror from the store before handing the request over, so
git's own fast-forward check does the rejecting in the common case, and pass the hook the generation
that sync observed, so the compare-and-swap is checked against the state the client actually saw.

There is no authentication. The server binds loopback, and reaching it from anywhere else needs
something in front that authenticates and forwards; it must not be exposed directly. Design:
`docs/design/sheaf.md`.
"""

from __future__ import annotations

import collections
import http.server
import logging
import os
import pathlib
import subprocess
import threading
from collections.abc import Collection, Iterable
from typing import Self, override

from themis.sheaf import backend as backend_mod
from themis.sheaf import errors
from themis.sheaf import store as store_mod
from themis.sheaf.wire import bare as bare_mod
from themis.sheaf.wire import hook, protect

ENDPOINTS = ('/info/refs', '/git-upload-pack', '/git-receive-pack')
CHUNK = 64 * 1024

_logger = logging.getLogger(__name__)


class _HttpServer(http.server.ThreadingHTTPServer):
    # Request threads are joined by `server_close`: a push mid-hook keeps its server until it is
    # answered, rather than losing it when the caller stops serving.
    daemon_threads = False


class SheafGitServer:
    """A loopback git server whose object database is a sheaf repository.

    Requests for one repository are serialised: sync writes refs, and two concurrent syncs on the
    same bare repo would race for no benefit. The store's compare-and-swap is the real concurrency
    control, and it lives elsewhere.
    """

    def __init__(
        self,
        repositories: Iterable[store_mod.Repository],
        root: str | os.PathLike[str],
        *,
        host: str = '127.0.0.1',
        port: int = 0,
        protection: protect.Protection | None = None,
    ) -> None:
        """Serve `repositories`, keeping bare mirrors under `root`.

        Args:
            repositories: The repositories this server will serve at all, each reached at the path
                its `repo` names. Required: the repository is otherwise taken entirely from the
                request path.
            root: Directory holding one bare mirror per served repository.
            host: Address to bind.
            port: Port to bind; 0 takes an ephemeral one, which `authority` then reports.
            protection: Paths the pushing side may not write and refs it may not rewrite. It
                reaches the hook through the environment rather than the repository, because a
                tracked config file would be editable in the same push it constrains.

        Raises:
            TypeError: If a repository has no descriptor form, so the hook could never be handed
                one.
            ValueError: If two repositories share a name, or a descriptor names nothing reachable.
        """
        # Absolute: git runs the hook with the bare repository as its working directory, and the
        # paths handed to it name this root.
        self.root = pathlib.Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.protection = protection or protect.Protection()
        self._repositories: dict[str, store_mod.Repository] = {}
        for repository in repositories:
            if repository.repo in self._repositories:
                raise ValueError(f'two repositories are named {repository.repo!r}')
            # A repository with no descriptor form cannot reach the hook, so fail at construction
            # rather than inside the first push.
            repository.descriptor()
            self._repositories[repository.repo] = repository
        self._locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._httpd = _HttpServer((host, port), _make_handler(self))
        self._thread: threading.Thread | None = None

    @classmethod
    def over_backend(
        cls,
        backend: backend_mod.Backend,
        root: str | os.PathLike[str],
        *,
        repos: Collection[str],
        host: str = '127.0.0.1',
        port: int = 0,
        protection: protect.Protection | None = None,
    ) -> Self:
        """Serve the named repositories of `backend` through in-process stores.

        Raises:
            TypeError: If `repos` is a single name rather than a collection of them, or if
                `backend` has no descriptor form.
            ValueError: If the backend has a descriptor form that names nothing reachable.
        """
        # A bare string would become a set of characters, and every request would 404.
        if isinstance(repos, str):
            raise TypeError('repos takes a collection of repository names, not one name')
        stores = [store_mod.Store(backend, repo) for repo in repos]
        return cls(stores, root, host=host, port=port, protection=protection)

    @property
    def repos(self) -> frozenset[str]:
        """The names of the repositories served."""
        return frozenset(self._repositories)

    def start(self) -> None:
        """Serve in a background thread."""
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop accepting, wait for in-flight requests, release the port. Safe on a server that was never started."""
        # `shutdown()` waits on an event `serve_forever` sets, so calling it on a server that never
        # served blocks forever. The socket is bound in `__init__`, so the close is unconditional.
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=5)
        self._httpd.server_close()

    def __enter__(self) -> Self:
        """Start on entry."""
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop on exit."""
        self.stop()

    @property
    def authority(self) -> str:
        """The `host:port` the server is bound to."""
        host, port = self._httpd.server_address[:2]
        return f'{host}:{port}'

    def url(self, repo: str) -> str:
        """The clone URL for `repo`."""
        return f'http://{self.authority}/{repo.strip("/")}'

    def bare(self, repo: str) -> bare_mod.BareRepo:
        """The bare mirror of `repo`.

        Raises:
            KeyError: If `repo` is not one this server serves.
        """
        return bare_mod.BareRepo(self._repositories[repo], self.root / repo)

    def lock_for(self, repo: str) -> threading.Lock:
        """The per-repository serialisation lock."""
        return self._locks[repo]

    def cgi_env(self, bare: bare_mod.BareRepo) -> dict[str, str]:
        """Environment for `git http-backend`: what the hook needs (`hook.environment`) plus the CGI export."""
        return {
            **hook.environment(bare, self.protection),
            'GIT_PROJECT_ROOT': str(self.root),
            'GIT_HTTP_EXPORT_ALL': '1',
        }


def _split_path(path: str) -> tuple[str, str] | None:
    """Split a request path into (repo, endpoint), or None if it is not a git endpoint."""
    target = path.split('?', 1)[0]
    for endpoint in ENDPOINTS:
        if target.endswith(endpoint):
            repo = target[: -len(endpoint)].strip('/')
            if not repo or '..' in repo:
                return None
            return repo, endpoint
    return None


def _make_handler(server: SheafGitServer) -> type[http.server.BaseHTTPRequestHandler]:
    """Build a handler bound to `server`."""

    class Handler(http.server.BaseHTTPRequestHandler):
        # Close-delimited responses: git's CGI output carries no `Content-Length` on any response,
        # its own error pages included. Resident per in-flight request is one whole request body
        # plus one whole response — `_dispatch` buffers both.
        protocol_version = 'HTTP/1.0'

        def do_GET(self) -> None:
            _dispatch(server, self, b'')

        def do_POST(self) -> None:
            _dispatch(server, self, _read_body(self))

        # `format` is BaseHTTPRequestHandler's parameter name; renaming it breaks the override.
        @override
        def log_message(self, format: str, *args: object) -> None:
            """Stay quiet: the server is embedded, not an operator-facing daemon."""

    return Handler


def _read_body(handler: http.server.BaseHTTPRequestHandler) -> bytes:
    """Read a request body, decoding chunked framing because CGI needs a length."""
    if (handler.headers.get('Transfer-Encoding') or '').lower() == 'chunked':
        return _read_chunked(handler)
    length = int(handler.headers.get('Content-Length') or 0)
    return handler.rfile.read(length) if length else b''


def _read_chunked(handler: http.server.BaseHTTPRequestHandler) -> bytes:
    """Decode a chunked request body. Git streams a large push this way."""
    body = bytearray()
    while True:
        size_line = handler.rfile.readline(CHUNK).split(b';', 1)[0].strip()
        size = int(size_line or b'0', 16)
        if size == 0:
            while handler.rfile.readline(CHUNK) not in (b'\r\n', b'\n', b''):
                pass
            return bytes(body)
        body += handler.rfile.read(size)
        handler.rfile.read(2)


def _dispatch(server: SheafGitServer, handler: http.server.BaseHTTPRequestHandler, body: bytes) -> None:
    """Sync the mirror, hand the request to git, relay what comes back."""
    route = _split_path(handler.path)
    if route is None:
        handler.send_error(404, 'not a git endpoint')
        return
    repo, endpoint = route
    if repo not in server.repos:
        handler.send_error(404, 'no such repository')
        return

    with server.lock_for(repo):
        bare = server.bare(repo)
        try:
            # Every endpoint, fetch included, or a rejected pusher's `git pull` gets its own stale
            # state back and never converges.
            bare.sync()
        except errors.CorruptRepository:
            # Not 503: the store names a pack that does not exist, and no amount of retrying mends
            # that. Reported apart from an outage so an operator is not told to wait.
            _logger.exception('corrupt repository %s', repo)
            handler.send_error(500, 'the sheaf store is corrupt')
            return
        except Exception:
            # Broad by position, not by indifference: this is the request boundary, and hydration
            # raises from unrelated hierarchies -- the backend's SDK, git through `RuntimeError`,
            # the filesystem through `OSError`. Uncaught, the handler thread dies and git reports
            # `Empty reply from server`, which a client cannot tell from a crashed process.
            _logger.exception('sync failed for %s', repo)
            handler.send_error(503, 'the sheaf store could not be read')
            return
        env = server.cgi_env(bare)
        env.update(
            {
                'REQUEST_METHOD': handler.command,
                'PATH_INFO': f'/{repo}{endpoint}',
                'QUERY_STRING': handler.path.partition('?')[2],
                'CONTENT_TYPE': handler.headers.get('Content-Type', ''),
                'CONTENT_LENGTH': str(len(body)),
                'REMOTE_ADDR': handler.client_address[0],
                'SERVER_PROTOCOL': 'HTTP/1.1',
                'GATEWAY_INTERFACE': 'CGI/1.1',
            }
        )
        process = subprocess.run(
            ['git', 'http-backend'],  # noqa: S607
            input=body,
            capture_output=True,
            env=env,
            cwd=str(server.root),
            check=False,
        )

    _relay(handler, process)


def _relay(handler: http.server.BaseHTTPRequestHandler, process: subprocess.CompletedProcess[bytes]) -> None:
    """Turn git's CGI output into an HTTP response."""
    if process.returncode != 0 and not process.stdout:
        handler.send_error(500, 'git http-backend failed')
        return
    head, separator, payload = process.stdout.partition(b'\r\n\r\n')
    if not separator:
        head, _, payload = process.stdout.partition(b'\n\n')

    status, headers = _parse_cgi_headers(head)
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(payload)


def _parse_cgi_headers(head: bytes) -> tuple[int, list[tuple[str, str]]]:
    """Extract the status and headers from git's CGI response head."""
    status = 200
    headers: list[tuple[str, str]] = []
    for raw in head.decode('latin-1').splitlines():
        name, _, value = raw.partition(':')
        value = value.strip()
        if name.lower() == 'status':
            status = int(value.split()[0])
        elif name:
            headers.append((name, value))
    return status, headers
