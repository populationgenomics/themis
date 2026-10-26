"""The automation identity a laptop tool drives a deployed environment as, and how it gets there.

`themis-clu` is the service account the `themis-clu` group may impersonate; it holds IAP access in
front of the data-plane services, the database login and the grants a tool needs, so a person never needs any of them
directly (`docs/runbooks/hand-driving-a-service.md`). Every road to it starts at the caller's own
application-default login (`gcloud auth application-default login`): impersonated credentials for a
client library, an ID token minted from them for a Cloud Run service, which a user credential cannot
mint itself, and `cloud-sql-proxy`'s own impersonation. The `gcloud` CLI's login plays no part.
"""

from __future__ import annotations

import http
import re
import shutil

import google.auth
import google.auth.iam
import requests
from google.auth import credentials as credentials_mod
from google.auth import exceptions, impersonated_credentials, jwt
from google.auth.transport import requests as auth_requests

DEFAULT_PROJECT = 'cpg-themis-dev'
REGION = 'australia-southeast1'
SQL_INSTANCE = 'themis-sql'
SQL_DATABASE = 'themis'
_CLU_EMAIL = 'themis-clu@{project}.iam.gserviceaccount.com'
_CLOUD_PLATFORM_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
# A user login carries it already; a service-account login needs it asked for to read its own email.
_USERINFO_EMAIL_SCOPE = 'https://www.googleapis.com/auth/userinfo.email'
_USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'
_RUN_SERVICE_URL = 'https://run.googleapis.com/v2/projects/{project}/locations/{region}/services/{service}'


class GoogleCallError(Exception):
    """A call to a Google API failed, or answered with something unusable. A tool's `main` turns this into its exit."""


def service_account(project: str) -> str:
    """The `themis-clu` service account's email in `project`."""
    return _CLU_EMAIL.format(project=project)


def db_user(service_account: str) -> str:
    """The Postgres role a service account logs in as: its email minus the suffix Cloud SQL strips."""
    return service_account.removesuffix('.gserviceaccount.com')


def resolve_binary(name: str) -> str:
    """Absolute path to `name`.

    Raises:
        SystemExit: If it is not on PATH.
    """
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f'{name!r} not found on PATH; install it before running this tool')
    return path


def caller_credentials(*, quota_project: str | None) -> credentials_mod.Credentials:
    """The caller's own application-default credentials.

    Args:
        quota_project: The project a call is billed and quota-checked against, or None for the one
            the login names. Naming the project a call reads keeps a login whose own quota project
            lacks the API from failing it.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login;
            `gcloud auth application-default login` creates one.
    """
    credentials, _project = google.auth.default(
        scopes=[_CLOUD_PLATFORM_SCOPE, _USERINFO_EMAIL_SCOPE], quota_project_id=quota_project
    )
    return credentials


def caller_email() -> str:
    """The email of the principal the caller's application-default login acts as.

    Raises:
        GoogleCallError: If Google does not answer with a verified email for the login.
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    userinfo = _get_json(
        caller_credentials(quota_project=None), _USERINFO_URL, purpose='read the email of the application-default login'
    )
    email = userinfo.get('email')
    if not isinstance(email, str) or not email or userinfo.get('email_verified') is not True:
        raise GoogleCallError(f'the application-default login has no verified email: {userinfo!r}')
    return email


def impersonated(service_account: str) -> impersonated_credentials.Credentials:
    """Credentials acting as `service_account`, sourced from the caller's application-default login.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    return impersonated_credentials.Credentials(
        source_credentials=caller_credentials(quota_project=None),
        target_principal=service_account,
        target_scopes=[_CLOUD_PLATFORM_SCOPE],
    )


def identity_token(service_account: str, audience: str) -> str:
    """An ID token for `audience`, minted as `service_account`, carrying the account's email.

    The email claim is what a gated service verifies the caller from; an impersonated token carries
    it only when asked for, and a token without it is `UNAUTHENTICATED` at every gate.

    Raises:
        GoogleCallError: If the token cannot be minted, with Google's own message (membership of the
            impersonating group is the usual cause), or carries no email claim naming the account. An
            absent application-default login is raised as this too. It is an ordinary exception
            because this runs on a request thread of the loopback server, where an exit would end the
            thread silently.
    """
    try:
        credentials = impersonated_credentials.IDTokenCredentials(
            target_credentials=impersonated(service_account), target_audience=audience, include_email=True
        )
        credentials.refresh(auth_requests.Request())
    except (exceptions.GoogleAuthError, requests.RequestException) as exc:
        raise GoogleCallError(f'could not mint an identity token as {service_account} for {audience}: {exc}') from exc
    token = credentials.token
    # Unverified: Google's IAM Credentials API minted it just now, over TLS, and `refresh` decoded it.
    claims = jwt.decode(token, verify=False)
    if claims.get('email') != service_account:
        raise GoogleCallError(
            f'the identity token minted as {service_account} carries email {claims.get("email")!r}, '
            'and a gated service verifies the caller from that claim'
        )
    return token


def run_service_url(service: str, *, project: str) -> str:
    """The URL of the Cloud Run `service` its ID tokens name, read through the Cloud Run Admin API as the caller.

    Cloud Run serves a service at two URLs. A data-plane service accepts an ID token only for the one built
    from its name, the project number and the region (`infra/themis_infra/cloud_run.py`), so that is the one
    returned. The caller's own login reads it, not `themis-clu`: the account holds no `run.services.get`, and
    reading a URL is no reason to widen what it can do.

    Raises:
        GoogleCallError: If the service cannot be read, or reports no such URL.
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    resource = _get_json(
        caller_credentials(quota_project=project),
        _RUN_SERVICE_URL.format(project=project, region=REGION, service=service),
        purpose=f'read the Cloud Run service {service} in {project}',
    )
    urls = resource.get('urls')
    built = re.compile(rf'https://{re.escape(service)}-[0-9]+\.{re.escape(REGION)}\.run\.app')
    matching = [url for url in urls if isinstance(url, str) and built.fullmatch(url)] if isinstance(urls, list) else []
    if len(matching) != 1:
        raise GoogleCallError(
            f'the Cloud Run service {service} in {project} reports no single URL of the form '
            f'https://{service}-<project number>.{REGION}.run.app: {urls!r}'
        )
    return matching[0]


def iap_jwt_credentials(service_account: str, origin: str) -> jwt.Credentials:
    """Credentials for a JWT that IAP in front of `origin` admits, signed as `service_account` through IAM.

    The `aud` is the origin with the path wildcard: an `aud` of the bare origin admits `/` alone, and IAP
    answers 401 on every other path. `iss` and `sub` are the account, which IAP checks the accessor binding
    against. One hour, the library's default lifetime, is IAP's ceiling.

    Raises:
        google.auth.exceptions.DefaultCredentialsError: If there is no application-default login.
    """
    signer = google.auth.iam.Signer(auth_requests.Request(), caller_credentials(quota_project=None), service_account)
    return jwt.Credentials(
        signer=signer, issuer=service_account, subject=service_account, audience=f'{origin.rstrip("/")}/*'
    )


def _get_json(credentials: credentials_mod.Credentials, url: str, *, purpose: str) -> dict[str, object]:
    """GET `url` as `credentials` and return the JSON object it answers.

    Raises:
        GoogleCallError: If the request fails, answers other than 200, or answers no JSON object.
    """
    with auth_requests.AuthorizedSession(credentials) as session:
        try:
            response = session.get(url)
        except (exceptions.GoogleAuthError, requests.RequestException) as exc:
            raise GoogleCallError(f'could not {purpose}: {exc}') from exc
    if response.status_code != http.HTTPStatus.OK:
        raise GoogleCallError(f'could not {purpose}: HTTP {response.status_code}: {response.text.strip()}')
    try:
        body = response.json()
    except requests.JSONDecodeError as exc:
        raise GoogleCallError(f'could not {purpose}: the answer is not JSON: {exc}') from exc
    if not isinstance(body, dict):
        raise GoogleCallError(f'could not {purpose}: the answer is not a JSON object: {body!r}')
    return body
