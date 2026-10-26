"""Serve an Analysis's repository as a loopback git remote over the deployed `Sheaf` service.

`git clone http://127.0.0.1:<port>/<analysis-id>` and `git push` against it, from a laptop, with
every read going through ReadRefDoc and FetchPack and every push through Publish — the path the
sandbox worker's mirror takes, driven by hand. The server is `themis.sheaf.wire.server` over a
`themis.clients.sheaf.store.RemoteStore`; this tool supplies what a laptop has and a service
account does not. The service takes only internal traffic, so a laptop reaches it through the web
load balancer's IAP (`tools.operator_path`), and every call carries two tokens minted as
`themis-clu` from the caller's application-default login: an ID token for the service, and the JWT
IAP admits. Both are kept fresh in the token file beside the session token `tools/session_token.py`
wrote there.

Run: ``uv run --group tools_sheaf python -m tools.sheaf_remote --analysis-id <id> --session-token-file <path>``.
Stop it with Ctrl-C; both tokens are removed from the token file on the way out. The caller needs
membership of the `themis-clu` group, which holds IAP access on the service's load-balancer backend.

The tokens are refreshed on demand rather than on a timer: before each request the server makes,
each token's expiry is read from the JWT and a new one minted when it is within ten minutes — so a
laptop that sleeps through the hour wakes to valid tokens on its next `git` command, and a token is
minted about once an hour otherwise.
"""

from __future__ import annotations

import os

# Set before gRPC is first imported: this process forks `git` while gRPC threads run, and with fork
# support on, gRPC logs every fork on stderr.
os.environ.setdefault('GRPC_ENABLE_FORK_SUPPORT', '0')

import argparse
import contextlib
import dataclasses
import pathlib
import shutil
import signal
import sys
import tempfile
import threading
from collections.abc import Iterator
from typing import override

from google.auth import exceptions

from themis.clients.sheaf import store as remote_mod
from themis.sheaf import errors
from themis.sheaf import store as store_mod
from themis.sheaf.wire import protect, server
from tools import clu, operator_path

SERVICE = 'themis-sheaf'
GRPC_SERVICE = 'themis.rpc.sheaf.Sheaf'


class BearerKeeper:
    """Keeps the token file's two bearers valid, minting each as the account when it is near expiry.

    `refresh` runs on the server's request threads, so a mint that fails raises `clu.GoogleCallError`
    there and the request is answered 503 with the cause logged, rather than ending the thread.
    """

    def __init__(self, token_file: pathlib.Path, tokens: operator_path.Tokens) -> None:
        self.token_file = token_file
        self.tokens = tokens
        self._lock = threading.Lock()

    def refresh(self) -> None:
        """Write the current bearers into the file where it holds others: absent, or about to expire."""
        with self._lock:
            credentials = remote_mod.read_credentials(self.token_file)
            bearer, proxy_bearer = self.tokens.current()
            if (credentials.bearer, credentials.proxy_bearer) == (bearer, proxy_bearer):
                return
            remote_mod.write_credentials(
                self.token_file, dataclasses.replace(credentials, bearer=bearer, proxy_bearer=proxy_bearer)
            )
            print(f'minted tokens as {self.tokens.service_account}', file=sys.stderr)

    def forget(self) -> None:
        """Remove both bearers from the file, leaving the session token."""
        with self._lock:
            credentials = remote_mod.read_credentials(self.token_file)
            remote_mod.write_credentials(
                self.token_file, dataclasses.replace(credentials, bearer=None, proxy_bearer=None)
            )


class RefreshingStore(store_mod.Repository):
    """A `RemoteStore` whose bearers are refreshed before every call, and whose IAP refusals say so.

    The server syncs the mirror — a `read` — before handing any request to git, so the hook, which
    rebuilds a plain `RemoteStore` from the descriptor and reads the same file, finds tokens the
    sync just refreshed. A sync IAP refuses fails first, here, where the refusal is asked of IAP
    directly and named.
    """

    def __init__(self, inner: remote_mod.RemoteStore, keeper: BearerKeeper) -> None:
        self.repo = inner.repo
        self._inner = inner
        self._keeper = keeper

    @override
    def read(self) -> store_mod.Snapshot:
        self._keeper.refresh()
        with self._naming_iap_refusals():
            return self._inner.read()

    @override
    def fetch_pack(self, ident: str) -> bytes:
        self._keeper.refresh()
        with self._naming_iap_refusals():
            return self._inner.fetch_pack(ident)

    @override
    def publish(self, base: store_mod.Snapshot, intent: store_mod.Intent) -> store_mod.Snapshot:
        self._keeper.refresh()
        with self._naming_iap_refusals():
            return self._inner.publish(base, intent)

    @contextlib.contextmanager
    def _naming_iap_refusals(self) -> Iterator[None]:
        """Re-raise a fault IAP's refusal could arrive as with what IAP says to the token the call carried.

        Raises:
            errors.ServiceFault: With IAP's refusal in its details where IAP refuses the token, else the
                fault's own details and a note that IAP lets the account through.
        """
        try:
            yield
        except errors.ServiceFault as exc:
            if exc.code not in {code.name for code in operator_path.REFUSAL_CODES}:
                raise
            raise errors.ServiceFault(
                exc.code, operator_path.explain_refusal(exc.details, self._keeper.tokens)
            ) from exc

    @override
    def descriptor(self) -> dict[str, str]:
        return self._inner.descriptor()


@contextlib.contextmanager
def serving(
    *,
    analysis_id: str,
    token_file: pathlib.Path,
    target: operator_path.Target,
    service_account: str,
    port: int,
    root: pathlib.Path,
) -> Iterator[server.SheafGitServer]:
    """Serve `analysis_id` on a loopback port over the service `target` names, for the duration of the block.

    The bearers are minted into `token_file` before the server starts, kept fresh while it runs, and
    removed from the file on the way out, whatever ends the block.

    Args:
        analysis_id: The Analysis; names the repository in the clone URL.
        token_file: The token file `tools/session_token.py` writes.
        target: The load balancer's origin, and the sheaf service's URL the bearer names.
        service_account: The account the bearers are minted as.
        port: The loopback port; 0 takes an ephemeral one, which the yielded server reports.
        root: Directory for the bare mirror.

    Raises:
        clu.GoogleCallError: If the first bearers cannot be minted.
    """
    keeper = BearerKeeper(token_file, operator_path.Tokens(target, service_account))
    keeper.refresh()
    try:
        with (
            remote_mod.RemoteStore(target.origin, token_file, repo=analysis_id) as remote,
            server.SheafGitServer(
                [RefreshingStore(remote, keeper)], root, port=port, protection=protect.Protection.unprotected()
            ) as instance,
        ):
            yield instance
    finally:
        keeper.forget()


def serve(
    *,
    analysis_id: str,
    token_file: pathlib.Path,
    target: operator_path.Target,
    service_account: str,
    port: int,
    root: pathlib.Path,
) -> None:
    """Serve until SIGINT or SIGTERM, then remove the bearers from the token file."""
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with serving(
        analysis_id=analysis_id,
        token_file=token_file,
        target=target,
        service_account=service_account,
        port=port,
        root=root,
    ) as instance:
        print(f'serving {analysis_id} over {target.service_url}, through {target.origin}', file=sys.stderr)
        print(f'    git clone {instance.url(analysis_id)}', file=sys.stderr)
        print('Ctrl-C to stop.', file=sys.stderr)
        stop.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--analysis-id', required=True, help='the Analysis; names the repository in the clone URL')
    parser.add_argument(
        '--session-token-file',
        required=True,
        type=pathlib.Path,
        help='the token file tools/session_token.py wrote; the bearer is kept in it while this runs',
    )
    parser.add_argument('--project', default=clu.DEFAULT_PROJECT, help='GCP project (default: %(default)s)')
    parser.add_argument(
        '--service-url',
        default=None,
        help=f'the sheaf service URL its ID tokens name (default: looked up in Cloud Run for {SERVICE} in --project)',
    )
    parser.add_argument(
        '--origin',
        default=None,
        help="the environment's https origin, the load balancer the calls go through (default: --project's domain)",
    )
    parser.add_argument('--port', type=int, default=0, help='loopback port to serve on (default: an ephemeral one)')
    parser.add_argument(
        '--root', type=pathlib.Path, default=None, help='directory for the bare mirror (default: a temporary one)'
    )
    parser.add_argument(
        '--as',
        dest='impersonate',
        metavar='SERVICE_ACCOUNT',
        default=None,
        help='mint the tokens as this service account (default: themis-clu in --project)',
    )
    args = parser.parse_args()
    service_account = args.impersonate or clu.service_account(args.project)
    # Read before anything is minted, so a missing or malformed file fails here and not after a token was minted.
    try:
        remote_mod.read_credentials(args.session_token_file)
    except errors.CredentialsUnusable as exc:
        raise SystemExit(str(exc)) from exc

    root = args.root or pathlib.Path(tempfile.mkdtemp(prefix='sheaf-remote-'))
    try:
        target = operator_path.Target(
            origin=args.origin or operator_path.origin(args.project),
            service_url=args.service_url or clu.run_service_url(SERVICE, project=args.project),
            grpc_service=GRPC_SERVICE,
        )
        serve(
            analysis_id=args.analysis_id,
            token_file=args.session_token_file,
            target=target,
            service_account=service_account,
            port=args.port,
            root=root,
        )
    except (clu.GoogleCallError, exceptions.DefaultCredentialsError, operator_path.TargetError) as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if args.root is None:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
