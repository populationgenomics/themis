"""Derive an Analysis's session token from a laptop and write it to a token file.

The `Sheaf` service scopes every call by the session token it carries, so a tool that clones an
Analysis's repository has to present the token the sandbox would. That token is not stored
anywhere: it is `HMAC(session-token-signing-key, session_id)`, re-derived by whoever holds the KMS
grant (`themis/clients/auth/derive.py`). This tool reads the Analysis's session id from the dev
database and derives the token through the same KMS call, both as `themis-clu`, and writes
`{"session_token": ...}` to a file only its owner can read — the file `tools/sheaf_remote.py` and
`themis.clients.sheaf.store.RemoteStore` take.

Run: ``uv run --group tools_sheaf python -m tools.session_token --analysis-id <id> --session-token-file <path>``.
The caller needs membership of the `themis-clu` group; the account needs `signerVerifier` on the
signing key, which this tool does not check for — a missing grant fails with the KMS error as is.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import pathlib
import subprocess

from google.auth import exceptions
from google.cloud.sql import connector

from themis.clients.auth import derive
from themis.clients.sheaf import store as remote_mod
from themis.common import sql
from tools import clu

_KEY_RING = 'themis'
# The version the deploy pins every signer to (infra/__main__.py, `session_token_key_version`).
_KEY_VERSION = 1


def session_id_for(analysis_id: str, *, project: str, service_account: str) -> str:
    """The Anthropic session id of `analysis_id`, read from the environment's database as `service_account`.

    Raises:
        SystemExit: If no such Analysis exists.
    """
    connection_name = f'{project}:{clu.REGION}:{clu.SQL_INSTANCE}'
    with (
        contextlib.closing(connector.Connector(credentials=clu.impersonated(service_account))) as pool,
        contextlib.closing(
            sql.iam_connect(
                pool, connection_name=connection_name, database=clu.SQL_DATABASE, db_user=clu.db_user(service_account)
            )
        ) as conn,
        contextlib.closing(conn.cursor()) as cursor,
    ):
        cursor.execute('SELECT session_id FROM analyses WHERE id = %s', (analysis_id,))
        row = cursor.fetchone()
    if row is None:
        raise SystemExit(f'no Analysis {analysis_id!r} in {connection_name}/{clu.SQL_DATABASE}')
    return row[0]


def signing_key_version(project: str) -> str:
    """The MAC key version every signer uses, found by purpose on the environment's key ring.

    Raises:
        SystemExit: If the key ring holds anything but one MAC key.
        clu.GcloudError: If `gcloud` is absent or refuses.
    """
    result = subprocess.run(  # noqa: S603 — resolved path, argv from fixed flags
        [
            clu.resolve_binary('gcloud'),
            'kms',
            'keys',
            'list',
            f'--keyring={_KEY_RING}',
            f'--location={clu.REGION}',
            f'--project={project}',
            '--filter=purpose=MAC',
            '--format=value(name)',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f'gcloud could not list the {_KEY_RING} key ring:\n{result.stderr.strip()}')
    keys = result.stdout.split()
    if len(keys) != 1:
        raise SystemExit(f'expected one MAC key on the {_KEY_RING} key ring, found {keys}; pass --key-version')
    return f'{keys[0]}/cryptoKeyVersions/{_KEY_VERSION}'


async def _sign(key_version: str, session_id: str, service_account: str) -> str:
    """The session token: the KMS MAC of `session_id`, signed as `service_account`.

    The client is built inside the running loop, as the async KMS client requires.
    """
    sign = derive.kms_deriver(key_version, credentials=clu.impersonated(service_account))
    return await sign(session_id)


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
        key_version = args.key_version or signing_key_version(args.project)
        session_id = session_id_for(args.analysis_id, project=args.project, service_account=service_account)
        session_token = asyncio.run(_sign(key_version, session_id, service_account))
    except (clu.GcloudError, exceptions.DefaultCredentialsError) as exc:
        raise SystemExit(str(exc)) from exc
    args.session_token_file.parent.mkdir(parents=True, exist_ok=True)
    remote_mod.write_credentials(
        args.session_token_file, remote_mod.Credentials(session_token=session_token, bearer=None)
    )
    print(f'wrote the session token for {args.analysis_id} to {args.session_token_file}')
    print(
        'next: uv run --group tools_sheaf python -m tools.sheaf_remote '
        f'--analysis-id {args.analysis_id} --session-token-file {args.session_token_file}'
    )


if __name__ == '__main__':
    main()
