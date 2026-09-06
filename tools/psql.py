"""Interactive ``psql`` against the themis Cloud SQL instance over the IAM-auth connector.

The instance (`infra/themis_infra/sql.py`) has an empty ``authorizedNetworks``, so it refuses
every direct connection; the only way in is the Cloud SQL connector's Admin-API ephemeral cert.
Auth is IAM (`cloudsql.iam_authentication`), so the Postgres login is a GCP principal, not a
password. This tool bridges that to a real ``psql`` session: it runs ``cloud-sql-proxy
--auto-iam-authn`` on a loopback port and execs ``psql`` against it, tearing the proxy down on exit.

It connects as ``themis-clu`` by default: a personal identity has no database login, and that account
is the one anything else reaches the instance as (`infra/themis_infra/clu.py`). ``--as`` overrides
it, and ``--as ''`` uses the caller's own principal.

Run: ``uv run python -m tools.psql`` (add ``--project``, ``--as <sa>``, or pass ``psql`` args after
``--``, e.g. ``-- -c 'select 1'``). The login principal still needs both the project roles a
connection requires (`roles/cloudsql.instanceUser` + `roles/cloudsql.client`) and the in-database
GRANTs the migrations apply — the two are separate, so a connection can succeed and every table still
be unreadable.

``themis-clu`` is a member of the migrator role and inherits it, so it reads and writes every table the
migrator owns.
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import subprocess
import time

from tools import clu

_PROXY = 'cloud-sql-proxy'
_PSQL = 'psql'


def _free_loopback_port() -> int:
    """A currently-free 127.0.0.1 port for the proxy to listen on."""
    with contextlib.closing(socket.socket()) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _active_gcloud_account() -> str:
    result = subprocess.run(  # noqa: S603 — fixed argv, resolved path
        [clu.resolve_binary('gcloud'), 'config', 'get-value', 'account'],
        capture_output=True,
        text=True,
        check=True,
    )
    account = result.stdout.strip()
    if not account:
        raise SystemExit('no active gcloud account; run `gcloud auth login` first')
    return account


def _login_role(impersonate: str | None) -> str:
    """The Postgres role to log in as: the IAM principal, minus the suffix Cloud SQL strips off SAs."""
    return clu.db_user(impersonate or _active_gcloud_account())


def _wait_until_listening(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.closing(socket.socket()) as probe:
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                return
        time.sleep(0.2)
    raise SystemExit(f'cloud-sql-proxy did not start listening on 127.0.0.1:{port} within {timeout:.0f}s')


def _run(project: str, impersonate: str | None, psql_args: list[str]) -> int:
    proxy_bin = clu.resolve_binary(_PROXY)
    psql_bin = clu.resolve_binary(_PSQL)
    connection_name = f'{project}:{clu.REGION}:{clu.SQL_INSTANCE}'
    port = _free_loopback_port()
    # --quota-project pins the Admin-API ephemeral-cert call to the target project; without it the
    # call bills to the caller's ADC quota project, which may not have sqladmin enabled.
    proxy_cmd = [proxy_bin, '--auto-iam-authn', '--quota-project', project, '--port', str(port)]
    if impersonate:
        proxy_cmd.append(f'--impersonate-service-account={impersonate}')
    proxy_cmd.append(connection_name)

    proxy = subprocess.Popen(proxy_cmd)  # noqa: S603 — resolved path, argv from fixed config + flags
    try:
        _wait_until_listening(port)
        dsn = f'host=127.0.0.1 port={port} dbname={clu.SQL_DATABASE} user={_login_role(impersonate)} sslmode=disable'
        return subprocess.run([psql_bin, dsn, *psql_args], check=False).returncode  # noqa: S603 — resolved path
    finally:
        proxy.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proxy.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', default=clu.DEFAULT_PROJECT, help='GCP project (default: %(default)s)')
    parser.add_argument(
        '--as',
        dest='impersonate',
        metavar='SERVICE_ACCOUNT',
        default=None,
        help='Impersonate this service account for both the proxy and the Postgres login '
        "(default: themis-clu in --project; pass '' for your own principal)",
    )
    args, psql_args = parser.parse_known_args()
    # parse_known_args leaves the `--` separator in the extras; drop it so it doesn't reach psql
    # (psql treats everything after its own `--` as positional and would ignore the query).
    if psql_args and psql_args[0] == '--':
        psql_args = psql_args[1:]
    impersonate = clu.service_account(args.project) if args.impersonate is None else args.impersonate
    try:
        status = _run(args.project, impersonate or None, psql_args)
    except clu.GcloudError as exc:
        raise SystemExit(str(exc)) from exc
    raise SystemExit(status)


if __name__ == '__main__':
    main()
