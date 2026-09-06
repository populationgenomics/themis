"""Serve an Analysis's repository as a loopback git remote over the deployed `Sheaf` service.

`git clone http://127.0.0.1:<port>/<analysis-id>` and `git push` against it, from a laptop, with
every read going through ReadRefDoc and FetchPack and every push through Publish — the path the
sandbox worker's mirror takes, driven by hand. The server is `themis.sheaf.wire.server` over a
`themis.clients.sheaf.store.RemoteStore`; this tool supplies what a laptop has and a service
account does not: an ID token for the service, minted by `gcloud` as `themis-clu` and kept fresh in
the token file beside the session token `tools/session_token.py` wrote there.

Run: ``uv run --group tools_sheaf python -m tools.sheaf_remote --analysis-id <id> --session-token-file <path>``.
Stop it with Ctrl-C; the bearer is removed from the token file on the way out. The caller needs
membership of the `themis-clu` group, which holds `run.invoker` on the service.

The bearer is refreshed on demand rather than on a timer: before each request the server makes,
the token's expiry is read from the JWT and a new one minted when it is within ten minutes — so a
laptop that sleeps through the hour wakes to a valid token on its next `git` command, and `gcloud`
runs about once an hour otherwise.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import shutil
import signal
import sys
import tempfile
import threading
import time
from typing import override

from themis.clients.sheaf import store as remote_mod
from themis.sheaf import errors
from themis.sheaf import store as store_mod
from themis.sheaf.wire import server
from tools import clu

_SERVICE = 'themis-sheaf'
# Re-mint this long before the token's `exp`; ID tokens live an hour.
_REFRESH_MARGIN_SECONDS = 10 * 60


def jwt_expiry(token: str) -> float:
    """The `exp` claim of `token`, read without verifying it: the token is the caller's own.

    Raises:
        ValueError: If `token` is not a JWT carrying a numeric `exp`.
    """
    parts = token.split('.')
    if len(parts) != 3:
        raise ValueError('not a JWT: expected three dot-separated parts')
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f'not a JWT: the payload does not decode: {exc}') from exc
    expiry = claims.get('exp') if isinstance(claims, dict) else None
    if not isinstance(expiry, int | float):
        raise ValueError('not an ID token: no numeric exp claim')
    return float(expiry)


def _fresh(bearer: str) -> bool:
    return jwt_expiry(bearer) - time.time() > _REFRESH_MARGIN_SECONDS


class BearerKeeper:
    """Keeps the token file's bearer valid: mints one as `service_account` when the current one is near expiry.

    `refresh` runs on the server's request threads, so a mint that fails raises `clu.GcloudError`
    there and the request is answered 503 with the cause logged, rather than ending the thread.
    """

    def __init__(self, token_file: pathlib.Path, *, service_account: str, audience: str) -> None:
        self.token_file = token_file
        self.service_account = service_account
        self.audience = audience
        self._lock = threading.Lock()

    def refresh(self) -> None:
        """Mint a bearer if the file's is absent or within the margin of expiring, and write it back."""
        with self._lock:
            credentials = remote_mod.read_credentials(self.token_file)
            if credentials.bearer is not None and _fresh(credentials.bearer):
                return
            bearer = clu.identity_token(self.service_account, self.audience)
            remote_mod.write_credentials(
                self.token_file, remote_mod.Credentials(session_token=credentials.session_token, bearer=bearer)
            )
            print(f'minted an identity token as {self.service_account}', file=sys.stderr)

    def forget(self) -> None:
        """Remove the bearer from the file, leaving the session token."""
        with self._lock:
            credentials = remote_mod.read_credentials(self.token_file)
            remote_mod.write_credentials(
                self.token_file, remote_mod.Credentials(session_token=credentials.session_token, bearer=None)
            )


class RefreshingStore(store_mod.Repository):
    """A `RemoteStore` whose bearer is refreshed before every call.

    The server syncs the mirror — a `read` — before handing any request to git, so the hook, which
    rebuilds a plain `RemoteStore` from the descriptor and reads the same file, finds a token the
    sync just refreshed.
    """

    def __init__(self, inner: remote_mod.RemoteStore, keeper: BearerKeeper) -> None:
        self.repo = inner.repo
        self._inner = inner
        self._keeper = keeper

    @override
    def read(self) -> store_mod.Snapshot:
        self._keeper.refresh()
        return self._inner.read()

    @override
    def fetch_pack(self, ident: str) -> bytes:
        self._keeper.refresh()
        return self._inner.fetch_pack(ident)

    @override
    def publish(self, base: store_mod.Snapshot, intent: store_mod.Intent) -> store_mod.Snapshot:
        self._keeper.refresh()
        return self._inner.publish(base, intent)

    @override
    def descriptor(self) -> dict[str, str]:
        return self._inner.descriptor()


def serve(
    *, analysis_id: str, token_file: pathlib.Path, service_url: str, service_account: str, port: int, root: pathlib.Path
) -> None:
    """Serve until SIGINT or SIGTERM, then remove the bearer from the token file."""
    keeper = BearerKeeper(token_file, service_account=service_account, audience=service_url)
    keeper.refresh()
    try:
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: stop.set())
        with (
            remote_mod.RemoteStore(service_url, token_file, repo=analysis_id) as remote,
            server.SheafGitServer([RefreshingStore(remote, keeper)], root, port=port) as instance,
        ):
            print(f'serving {analysis_id} over {service_url}', file=sys.stderr)
            print(f'    git clone {instance.url(analysis_id)}', file=sys.stderr)
            print('Ctrl-C to stop.', file=sys.stderr)
            stop.wait()
    finally:
        keeper.forget()


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
        '--service-url', default=None, help=f'the sheaf service URL (default: gcloud describes {_SERVICE} in --project)'
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
        help='mint the identity token as this service account (default: themis-clu in --project)',
    )
    args = parser.parse_args()
    service_account = args.impersonate or clu.service_account(args.project)
    # Read before anything is minted, so a missing or malformed file fails here and not after gcloud ran.
    try:
        remote_mod.read_credentials(args.session_token_file)
    except errors.CredentialsUnusable as exc:
        raise SystemExit(str(exc)) from exc

    root = args.root or pathlib.Path(tempfile.mkdtemp(prefix='sheaf-remote-'))
    try:
        serve(
            analysis_id=args.analysis_id,
            token_file=args.session_token_file,
            service_url=args.service_url or clu.run_service_url(_SERVICE, project=args.project),
            service_account=service_account,
            port=args.port,
            root=root,
        )
    except clu.GcloudError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if args.root is None:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main()
