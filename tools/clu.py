"""The automation identity a laptop tool drives a deployed environment as, and how it gets there.

`themis-clu` is the service account the `themis-clu` group may impersonate; it holds the invoker
bindings, the database login and the grants a tool needs, so a person never needs any of them
directly (`docs/runbooks/hand-driving-a-service.md`). Two roads lead to it from a terminal and both
start at the caller's own `gcloud` login: `google-auth` impersonated credentials for a client
library, and `gcloud auth print-identity-token --impersonate-service-account` for the ID token a
Cloud Run service admits, which a user credential cannot mint.
"""

from __future__ import annotations

import shutil
import subprocess

import google.auth
from google.auth import credentials as credentials_mod
from google.auth import impersonated_credentials

DEFAULT_PROJECT = 'cpg-themis-dev'
REGION = 'australia-southeast1'
SQL_INSTANCE = 'themis-sql'
SQL_DATABASE = 'themis'
_CLU_EMAIL = 'themis-clu@{project}.iam.gserviceaccount.com'
_CLOUD_PLATFORM_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'


class GcloudError(Exception):
    """`gcloud` is absent or refused, in its own words. A tool's `main` turns this into its exit."""


def service_account(project: str) -> str:
    """The `themis-clu` service account's email in `project`."""
    return _CLU_EMAIL.format(project=project)


def db_user(service_account: str) -> str:
    """The Postgres role a service account logs in as: its email minus the suffix Cloud SQL strips."""
    return service_account.removesuffix('.gserviceaccount.com')


def resolve_binary(name: str) -> str:
    """Absolute path to `name`.

    Raises:
        GcloudError: If it is not on PATH.
    """
    path = shutil.which(name)
    if path is None:
        raise GcloudError(f'{name!r} not found on PATH; install it before running this tool')
    return path


def impersonated(service_account: str) -> credentials_mod.Credentials:
    """Credentials acting as `service_account`, sourced from the caller's application-default login.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login;
            `gcloud auth application-default login` creates one.
    """
    source, _project = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    return impersonated_credentials.Credentials(
        source_credentials=source, target_principal=service_account, target_scopes=[_CLOUD_PLATFORM_SCOPE]
    )


def identity_token(service_account: str, audience: str) -> str:
    """An ID token for `audience`, minted by `gcloud` as `service_account`.

    Raises:
        GcloudError: If `gcloud` is absent or refuses, with its own message — membership of the
            impersonating group is the usual cause. Raised as an ordinary exception because this runs
            on a request thread of the loopback server, where an exit would end the thread silently.
    """
    result = subprocess.run(  # noqa: S603 — resolved path, argv from fixed flags
        [
            resolve_binary('gcloud'),
            'auth',
            'print-identity-token',
            f'--impersonate-service-account={service_account}',
            f'--audiences={audience}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GcloudError(f'gcloud could not mint an identity token as {service_account}:\n{result.stderr.strip()}')
    token = result.stdout.strip()
    if not token:
        raise GcloudError('gcloud printed no identity token')
    return token


def run_service_url(service: str, *, project: str) -> str:
    """The URL of the Cloud Run `service`, as `gcloud run services describe` reports it.

    Raises:
        GcloudError: If `gcloud` is absent or refuses, or reports no https URL.
    """
    result = subprocess.run(  # noqa: S603 — resolved path, argv from fixed flags
        [
            resolve_binary('gcloud'),
            'run',
            'services',
            'describe',
            service,
            f'--region={REGION}',
            f'--project={project}',
            '--format=value(status.url)',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GcloudError(f'gcloud could not describe {service}:\n{result.stderr.strip()}')
    url = result.stdout.strip()
    if not url.startswith('https://'):
        raise GcloudError(f'gcloud reported no https URL for {service}: {url!r}')
    return url
