r"""Create an Analysis whose run is driven outside the workbench, and derive its session token.

The workbench mints an Anthropic session per Analysis and the platform drives the run. A harness
evaluation drives its own, so there is no platform session to mint — but everything downstream of the
session id still works, because the id is only ever a string: the bearer is a KMS MAC over it, and
`session_context` resolves the hash of that bearer to the Analysis whose repository the Sheaf service
then serves. So this writes the same two rows the BFF writes, with a session id the platform has
never heard of and never will.

The prefix is the contract: the workbench reads it to know the run has no conversation to show
(`apps/web/src/lib/harness.ts`), and treats any other shape as the platform's. Nothing enforces the
agreement across the two languages, so this tool is the only thing that should be minting them.

Run:

    uv run --group tools_sheaf python -m tools.pi_analysis \\
        --project-id <project> --prompt "..." --session-token-file <path>

`--dry-run` prints the ids and the statements without reaching KMS or the database. The caller needs
membership of the `themis-clu` group, and the signing key's `signerVerifier`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import pathlib
import subprocess
import uuid

from google.auth import exceptions
from google.cloud.sql import connector

from themis.clients.auth import derive
from themis.clients.sheaf import store as remote_mod
from themis.common import sql
from themis.services.auth import backend as auth_backend
from themis.workbench.models import workbench_pb2
from tools import clu, session_token

# What the workbench reads to know a run has no conversation of its own; keep in step with
# `UNMANAGED_SESSION_PREFIX` in apps/web/src/lib/harness.ts.
UNMANAGED_SESSION_PREFIX = 'pi_'


def caller_email(project: str) -> str:
    """The account `gcloud` is logged in as, recorded as the Analysis's creator.

    Raises:
        SystemExit: If gcloud names no active account.
    """
    result = subprocess.run(  # noqa: S603 — resolved path, argv from fixed flags
        [clu.resolve_binary('gcloud'), 'config', 'get-value', 'account', f'--project={project}'],
        capture_output=True,
        text=True,
        check=False,
    )
    account = result.stdout.strip()
    if result.returncode != 0 or not account or account == '(unset)':
        raise SystemExit('gcloud names no active account; pass --created-by')
    return account


async def sign(key_version: str, session_id: str, service_account: str) -> str:
    """The session bearer: the KMS MAC of `session_id`, signed as `service_account`."""
    signer = derive.kms_deriver(key_version, credentials=clu.impersonated(service_account))
    return await signer(session_id)


def new_ids() -> tuple[str, str]:
    """A fresh `(analysis_id, session_id)`: the analysis is ordinary, the session is ours."""
    return f'an_{uuid.uuid4()}', f'{UNMANAGED_SESSION_PREFIX}{uuid.uuid4()}'


def inputs_for(prompt: str) -> bytes:
    """The serialized `AnalysisInputs` the row carries — a free-form scenario, as the curator stated it.

    Raises:
        SystemExit: If the prompt is blank, which the proto's own validation refuses.
    """
    if not prompt.strip():
        raise SystemExit('the prompt must contain a non-whitespace character')
    inputs = workbench_pb2.AnalysisInputs(free_form=workbench_pb2.FreeFormInputs(prompt=prompt))
    return inputs.SerializeToString()


def insert(
    *,
    analysis_id: str,
    session_id: str,
    project_id: str,
    inputs: bytes,
    created_by: str,
    token_hash: str,
    project: str,
    service_account: str,
) -> None:
    """Write the analysis and the session binding, in one transaction.

    Both or neither: a binding without its analysis violates the foreign key, and an analysis whose
    binding is missing is a repository nothing can reach.

    Raises:
        SystemExit: If the write is refused — a duplicate id, or an unknown Project.
    """
    connection_name = f'{project}:{clu.REGION}:{clu.SQL_INSTANCE}'
    with (
        contextlib.closing(connector.Connector(credentials=clu.impersonated(service_account))) as pool,
        contextlib.closing(
            sql.iam_connect(
                pool, connection_name=connection_name, database=clu.SQL_DATABASE, db_user=clu.db_user(service_account)
            )
        ) as conn,
    ):
        try:
            with contextlib.closing(conn.cursor()) as cursor:
                cursor.execute(
                    'INSERT INTO analyses (id, session_id, project_id, inputs, created_by) VALUES (%s, %s, %s, %s, %s)',
                    (analysis_id, session_id, project_id, inputs, created_by),
                )
                cursor.execute(
                    'INSERT INTO session_context (token_hash, project_id, analysis_id) VALUES (%s, %s, %s)',
                    (token_hash, project_id, analysis_id),
                )
            conn.commit()
        except Exception as exc:  # the driver's errors are its own hierarchy
            raise SystemExit(f'the write was refused: {type(exc).__name__}: {exc}') from exc


def main() -> None:
    """Mint the ids, derive the token, write the rows, and leave a token file behind."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project-id', required=True, help='the Themis Project the Analysis lands in')
    parser.add_argument('--prompt', required=True, help='what the Analysis was asked to do')
    parser.add_argument(
        '--session-token-file', required=True, type=pathlib.Path, help='where to write the token file (mode 0600)'
    )
    parser.add_argument('--created-by', default=None, help='the creator recorded on the row (default: the caller)')
    parser.add_argument('--project', default=clu.DEFAULT_PROJECT, help='GCP project (default: %(default)s)')
    parser.add_argument('--key-version', default=None, help='the signing key version (default: the themis MAC key)')
    parser.add_argument(
        '--as',
        dest='impersonate',
        metavar='SERVICE_ACCOUNT',
        default=None,
        help='impersonate this service account for the database and KMS (default: themis-clu in --project)',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='print the ids and statements; reach neither KMS nor the database',
    )
    args = parser.parse_args()

    analysis_id, session_id = new_ids()
    inputs = inputs_for(args.prompt)
    created_by = args.created_by or caller_email(args.project)

    if args.dry_run:
        print(f'analysis_id  {analysis_id}')
        print(f'session_id   {session_id}')
        print(f'project_id   {args.project_id}')
        print(f'created_by   {created_by}')
        print(f'inputs       {len(inputs)} bytes of AnalysisInputs(free_form)')
        print()
        print('INSERT INTO analyses (id, session_id, project_id, inputs, created_by) VALUES (…);')
        print('INSERT INTO session_context (token_hash, project_id, analysis_id) VALUES (…);')
        print()
        print('the token hash needs the KMS signature, so it is not derived on a dry run')
        return

    service_account = args.impersonate or clu.service_account(args.project)
    try:
        key_version = args.key_version or session_token.signing_key_version(args.project)
        bearer = asyncio.run(sign(key_version, session_id, service_account))
    except (clu.GcloudError, exceptions.DefaultCredentialsError) as exc:
        raise SystemExit(str(exc)) from exc

    insert(
        analysis_id=analysis_id,
        session_id=session_id,
        project_id=args.project_id,
        inputs=inputs,
        created_by=created_by,
        token_hash=auth_backend.hash_token(bearer),
        project=args.project,
        service_account=service_account,
    )

    args.session_token_file.parent.mkdir(parents=True, exist_ok=True)
    remote_mod.write_credentials(args.session_token_file, remote_mod.Credentials(session_token=bearer, bearer=None))
    print(f'created {analysis_id} (session {session_id}) in {args.project_id}')
    print(f'wrote its session token to {args.session_token_file}')
    print(
        'next: uv run --group tools_sheaf python -m tools.sheaf_remote '
        f'--analysis-id {analysis_id} --session-token-file {args.session_token_file}'
    )


if __name__ == '__main__':
    main()
