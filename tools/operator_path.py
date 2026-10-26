"""Call a deployed data-plane gRPC service from a laptop, as `themis-clu`, through the web load balancer's IAP.

The data-plane services take only internal traffic, so a laptop reaches them at the environment's domain. There
the load balancer sends each gRPC service's methods to that service's own backend, which has IAP on it
(`infra/themis_infra/operator_ingress.py`). Every call carries two tokens, both minted as the account from the
caller's application-default login (`tools.clu`):

- `proxy-authorization: Bearer <JWT>`, a JWT the account signs itself for the domain, which IAP checks and then
  consumes;
- `authorization: Bearer <ID token>`, an ID token for the service's own URL, which IAP passes through and the
  service verifies as the caller.

grpc-python reports IAP's refusal as `UNKNOWN: Stream removed`, the same status a dropped connection gets.
`iap_refusal` asks IAP directly, with a plain HTTPS request carrying the same JWT, and returns what IAP said;
`explain` asks it for every status IAP's refusal could arrive as.
The runbook is `docs/runbooks/hand-driving-a-service.md`.
"""

from __future__ import annotations

import base64
import dataclasses
import http
import json
import threading
import time
from typing import override

import grpc
import requests
from google.auth import exceptions
from google.auth.transport import requests as auth_requests

from tools import clu, stack

# Re-mint a token this long before its `exp`: both kinds live an hour.
REFRESH_MARGIN_SECONDS = 10 * 60
_PROBE_TIMEOUT_S = 30
_BODY_EXCERPT = 300
_RUNBOOK = 'docs/runbooks/hand-driving-a-service.md'
# The statuses IAP's refusal could reach grpc-python as: what the spike saw, and the two an HTTP 401 or 403 maps to.
REFUSAL_CODES = frozenset({grpc.StatusCode.UNKNOWN, grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED})


class TargetError(ValueError):
    """An operator target cannot be formed: no single stack names the project, or a URL is not `https://`."""


@dataclasses.dataclass(frozen=True)
class Target:
    """Where an operator's call to one service goes, and whom its ID token names.

    Attributes:
        origin: The environment's `https://` origin, the load balancer's hostname.
        service_url: The service's own URL: the audience of the ID token, never dialed from a laptop.
        grpc_service: A gRPC service the service serves, whose path prefix the load balancer routes to it.
    """

    origin: str
    service_url: str
    grpc_service: str

    def __post_init__(self) -> None:
        for name in ('origin', 'service_url'):
            if not getattr(self, name).startswith('https://'):
                raise TargetError(f'{name} must be an https:// URL, since every call carries a bearer: {self!r}')

    @property
    def grpc_target(self) -> str:
        """The `host:port` a channel dials: the load balancer, on 443."""
        return f'{self.origin.removeprefix("https://").rstrip("/")}:443'


def origin(project: str) -> str:
    """The `https://` origin of the environment deployed in `project`: the `themis:domain` its stack sets.

    Raises:
        TargetError: If no single stack names `project`, or it sets no domain.
    """
    try:
        return f'https://{stack.value(project, "themis:domain")}'
    except ValueError as exc:
        raise TargetError(str(exc)) from exc


def lookup(service: str, grpc_service: str, *, project: str) -> Target:
    """The target for `service` in `project`: the stack's domain, and the service's URL from Cloud Run.

    Raises:
        clu.GoogleCallError: If the service's URL cannot be read.
        TargetError: If no single stack names `project`, or it sets no domain.
    """
    return Target(origin(project), clu.run_service_url(service, project=project), grpc_service)


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
        raise ValueError('not a token that expires: no numeric exp claim')
    return float(expiry)


def fresh(token: str) -> bool:
    """Whether `token` is a JWT that stays valid for longer than the refresh margin."""
    return jwt_expiry(token) - time.time() > REFRESH_MARGIN_SECONDS


class Tokens:
    """The two tokens an operator's call carries, each minted as `service_account` and re-minted near expiry.

    Safe to share across threads. A mint that fails raises `clu.GoogleCallError`.
    """

    def __init__(self, target: Target, service_account: str) -> None:
        self.target = target
        self.service_account = service_account
        self._iap = clu.iap_jwt_credentials(service_account, target.origin)
        self._lock = threading.Lock()
        self._bearer: str | None = None
        self._proxy_bearer: str | None = None

    def current(self) -> tuple[str, str]:
        """The ID token for the service and the JWT for IAP, each valid past the refresh margin.

        Raises:
            clu.GoogleCallError: If a token cannot be minted.
        """
        with self._lock:
            if self._bearer is None or not fresh(self._bearer):
                self._bearer = clu.identity_token(self.service_account, self.target.service_url)
            if self._proxy_bearer is None or not fresh(self._proxy_bearer):
                self._proxy_bearer = self._sign()
            return self._bearer, self._proxy_bearer

    def metadata(self) -> tuple[tuple[str, str], ...]:
        """The call metadata carrying both tokens."""
        bearer, proxy_bearer = self.current()
        return (('authorization', f'Bearer {bearer}'), ('proxy-authorization', f'Bearer {proxy_bearer}'))

    def _sign(self) -> str:
        try:
            self._iap.refresh(auth_requests.Request())
        except (exceptions.GoogleAuthError, requests.RequestException) as exc:
            raise clu.GoogleCallError(
                f'could not sign an IAP token as {self.service_account}: {exc}. Either the application-default '
                f'login is spent (`gcloud auth application-default login`), or you are not in the group that may '
                f'act as the account ({_RUNBOOK})'
            ) from exc
        token = self._iap.token
        return token.decode('utf-8') if isinstance(token, bytes) else str(token)


class _TokensPlugin(grpc.AuthMetadataPlugin):
    def __init__(self, tokens: Tokens) -> None:
        self._tokens = tokens

    @override
    def __call__(self, context: grpc.AuthMetadataContext, callback: grpc.AuthMetadataPluginCallback) -> None:
        try:
            metadata = self._tokens.metadata()
        except clu.GoogleCallError as exc:
            callback((), exc)
            return
        callback(metadata, None)


def channel(tokens: Tokens) -> grpc.Channel:
    """A TLS channel to the load balancer whose every call carries `tokens`."""
    credentials = grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(), grpc.metadata_call_credentials(_TokensPlugin(tokens))
    )
    return grpc.secure_channel(tokens.target.grpc_target, credentials)


def iap_refusal(target: Target, proxy_bearer: str) -> str | None:
    """What IAP says to `proxy_bearer` in front of the service, when it refuses it; None when it lets it through.

    A plain HTTPS request to the service's routed prefix, carrying the JWT alone: IAP answers 401 for a token it
    does not accept and 403 for an account with no access, each with a body naming the check, and anything else
    once it has let the request through. A request that IAP lets through reaches the service with no ID token, so
    the service refuses it before any handler runs.

    Raises:
        requests.RequestException: If the load balancer does not answer.
    """
    response = requests.get(
        f'{target.origin.rstrip("/")}/{target.grpc_service}/',
        headers={'Proxy-Authorization': f'Bearer {proxy_bearer}'},
        timeout=_PROBE_TIMEOUT_S,
        allow_redirects=False,
    )
    if response.status_code not in (http.HTTPStatus.UNAUTHORIZED, http.HTTPStatus.FORBIDDEN):
        return None
    body = response.text.strip()[:_BODY_EXCERPT]
    return (
        f'IAP in front of {target.grpc_service} at {target.origin} answered HTTP {response.status_code}: {body}. '
        f'A 401 is the token refused (its aud must be {target.origin}/*). A 403 is IAP refusing the account, for '
        f"want of IAP access on the backend, or Cloud Run refusing IAP's own service agent; the body says which "
        f'({_RUNBOOK})'
    )


def explain_refusal(status: str, tokens: Tokens) -> str:
    """A message for a call that failed with a `REFUSAL_CODES` status: IAP's refusal where it refused, else `status`.

    Args:
        status: The failed call's status, in its own words.
        tokens: The tokens the call carried.
    """
    try:
        refusal = iap_refusal(tokens.target, tokens.current()[1])
    except (requests.RequestException, clu.GoogleCallError) as probe:
        return f'{status} (asking IAP directly failed too: {probe})'
    return refusal or f'{status} (IAP lets the account through, so the fault is past it)'


def explain(exc: grpc.RpcError, tokens: Tokens) -> str:
    """A message for a failed call: IAP's own refusal where it refused, else the call's status in its own words.

    Raises:
        exc: Re-raised if it carries no status.
    """
    if not isinstance(exc, grpc.Call):
        raise exc
    status = f'{exc.code().name}: {exc.details()}'
    return explain_refusal(status, tokens) if exc.code() in REFUSAL_CODES else status
