"""Derive an Analysis's session token from a laptop and write it to a token file.

The `Sheaf` service scopes every call by the session token it carries, so a tool that clones an
Analysis's repository has to present the token the sandbox would. That token is not stored
anywhere: it is `HMAC(session-token-signing-key, session_id)`, re-derived by whoever holds the KMS
grant (`themis/clients/auth/derive.py`). This tool reads the Analysis's session id from the dev
database and derives the token through the same KMS call, both as `themis-clu`, and writes
`{"session_token": ..., "calling_as": "CALLING_AS_SELF"}` to a file only its owner can read — the file
`tools/sheaf_remote.py` and `themis.clients.sheaf.store.RemoteStore` take.

Run: ``uv run --group tools_sheaf python -m tools.session_token --analysis-id <id> --session-token-file <path>``.
The caller needs membership of the `themis-clu` group; the account needs `signerVerifier` on the
signing key, which this tool does not check for — a missing grant fails with the KMS error as is.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import pathlib

import requests
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions
from google.auth.transport import requests as auth_requests
from google.cloud import kms_v1

from themis.clients.auth import derive
from themis.clients.sheaf import store as remote_mod
from themis.rpc import sandbox_options_pb2
from tools import clu, clu_database

_KEY_RING = 'themis'
# The version the deploy pins every signer to (infra/__main__.py, `session_token_key_version`).
_KEY_VERSION = 1


class TokenError(Exception):
    """The token cannot be derived: no such Analysis, or no single signing key to derive it with."""


def session_id_for(analysis_id: str, *, project: str, service_account: str) -> str:
    """The Anthropic session id of `analysis_id`, read from the environment's database as `service_account`.

    Raises:
        TokenError: If no such Analysis exists.
    """
    with (
        clu_database.connect(project, service_account) as conn,
        contextlib.closing(conn.cursor()) as cursor,
    ):
        cursor.execute('SELECT session_id FROM analyses WHERE id = %s', (analysis_id,))
        row = cursor.fetchone()
    if row is None:
        raise TokenError(f'no Analysis {analysis_id!r} in {clu_database.connection_name(project)}/{clu.SQL_DATABASE}')
    return row[0]


def signing_key_version(project: str) -> str:
    """The MAC key version every signer uses, found by purpose on the environment's key ring.

    The caller's own login lists the key ring, not `themis-clu`: the account may sign with the key
    but not list the ring.

    Raises:
        TokenError: If the key ring holds anything but one MAC key.
        clu.GoogleCallError: If the key ring cannot be listed.
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    key_ring = f'projects/{project}/locations/{clu.REGION}/keyRings/{_KEY_RING}'
    credentials = clu.caller_credentials(quota_project=project)
    try:
        # Refreshed here: a refresh failing inside the gRPC call surfaces as a retried UNAVAILABLE.
        credentials.refresh(auth_requests.Request())
        with kms_v1.KeyManagementServiceClient(credentials=credentials) as client:
            keys = [key.name for key in client.list_crypto_keys(request={'parent': key_ring, 'filter': 'purpose=MAC'})]
    except (api_exceptions.GoogleAPIError, exceptions.GoogleAuthError, requests.RequestException) as exc:
        raise clu.GoogleCallError(f'could not list the MAC keys on {key_ring}: {exc}') from exc
    if len(keys) != 1:
        raise TokenError(f'expected one MAC key on the {_KEY_RING} key ring, found {keys}; pass --key-version')
    return f'{keys[0]}/cryptoKeyVersions/{_KEY_VERSION}'


async def _sign(key_version: str, session_id: str, service_account: str) -> str:
    """The session token: the KMS MAC of `session_id`, signed as `service_account`.

    The client is built inside the running loop, as the async KMS client requires.
    """
    sign = derive.kms_deriver(key_version, credentials=clu.impersonated(service_account))
    return await sign(session_id)


def derive_session_token(analysis_id: str, *, project: str, service_account: str, key_version: str | None) -> str:
    """The session token of `analysis_id`: its session id read from the database, MAC-signed through KMS.

    Args:
        analysis_id: The Analysis whose token to derive.
        project: The environment's GCP project.
        service_account: The account the database read and the KMS call act as.
        key_version: The signing key version's resource name, or None for the one MAC key on the
            key ring at the pinned version.

    Raises:
        TokenError: If no such Analysis exists, or the key ring holds anything but one MAC key.
        clu.GoogleCallError: If the key ring cannot be listed.
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    key_version = key_version or signing_key_version(project)
    session_id = session_id_for(analysis_id, project=project, service_account=service_account)
    return asyncio.run(_sign(key_version, session_id, service_account))


def write_token_file(path: pathlib.Path, session_token: str) -> None:
    """Write `session_token` to the token file at `path`, readable by its owner alone, claimed as the caller's own."""
    remote_mod.write_credentials(
        path,
        remote_mod.Credentials(
            session_token=session_token, bearer=None, calling_as=sandbox_options_pb2.CALLING_AS_SELF
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--analysis-id', required=True, help='the Analysis whose session token to derive')
    parser.add_argument(
        '--session-token-file', required=True, type=pathlib.Path, help='where to write the token file (mode 0600)'
    )
    parser.add_argument('--project', default=clu.DEFAULT_PROJECT, help='GCP project (default: %(default)s)')
    parser.add_argument(
        '--key-version',
        default=None,
        help='the signing key version resource name (default: the one MAC key on the themis key ring, version 1)',
    )
    parser.add_argument(
        '--as',
        dest='impersonate',
        metavar='SERVICE_ACCOUNT',
        default=None,
        help='impersonate this service account for the database and KMS (default: themis-clu in --project)',
    )
    args = parser.parse_args()
    service_account = args.impersonate or clu.service_account(args.project)
    try:
        session_token = derive_session_token(
            args.analysis_id, project=args.project, service_account=service_account, key_version=args.key_version
        )
    except (TokenError, clu.GoogleCallError, exceptions.DefaultCredentialsError) as exc:
        raise SystemExit(str(exc)) from exc
    args.session_token_file.parent.mkdir(parents=True, exist_ok=True)
    write_token_file(args.session_token_file, session_token)
    print(f'wrote the session token for {args.analysis_id} to {args.session_token_file}')
    print(
        'next: uv run --group tools_sheaf python -m tools.sheaf_remote '
        f'--analysis-id {args.analysis_id} --session-token-file {args.session_token_file}'
    )


if __name__ == '__main__':
    main()
