"""Call the deployed web app's `Workbench` rpcs from a laptop, as `themis-clu`, through IAP.

The app sits behind IAP on its Cloud Run service. IAP admits a JWT the account signs itself, whose
`aud` is the app's origin with the path wildcard, and passes the account's email on to the app in its
own assertion. The app asks the auth service what that email may reach, as it would for a person; the
auth service grants the deployment's operator account, `themis-clu`, both roles on every Project in the
deployment's ACL without the ACL listing it (`docs/runbooks/hand-driving-a-service.md`). The
signing is IAM's, authorized by the caller's application-default login, so the caller needs what
`tools.clu` needs: membership of the `themis-clu` group.

The rpcs are the Connect protocol over HTTP: a POST to `/api/rpc/<service>/<method>` carrying the
request message, here in its binary encoding, so the generated stubs are the whole contract and no
JSON mapping sits in between. The app's origin is the stack's own `themis:domain` (`tools.stack`), so no
URL is configured twice.
"""

from __future__ import annotations

import http
import threading

import google.auth.exceptions
import requests
from google.auth.transport import requests as google_auth_requests
from google.protobuf import message

from themis.workbench.models import workbench_pb2
from themis.workbench.rpc import workbench_pb2 as workbench_rpc_pb2
from tools import clu, stack

_SERVICE = workbench_rpc_pb2.DESCRIPTOR.services_by_name['Workbench'].full_name
_TIMEOUT_S = 60
_PROTO = 'application/proto'
_BODY_EXCERPT = 300
_RUNBOOK = 'docs/runbooks/hand-driving-a-service.md'
_SIGNING_FAILURES = (google.auth.exceptions.RefreshError, google.auth.exceptions.TransportError)


class AccessRefusedError(Exception):
    """No token could be signed, or IAP or the app's perimeter turned the request away before any rpc ran."""


class RpcError(Exception):
    """The app answered with a Connect error."""

    def __init__(self, method: str, code: str, detail: str) -> None:
        super().__init__(f'{method}: {code}: {detail}')
        self.code = code


class UnavailableError(RpcError):
    """The app, or the proxies in front of it, said it cannot answer now: a call that may succeed if made again.

    Whether to make it again is the caller's: a `CreateAnalysis` that raised this may still have created.
    """


class RejectedError(RpcError):
    """The app refused the request at its boundary, before acting on it: invalid, or the caller is not allowed.

    The app validates a request and checks the caller's membership before it does anything, so a request
    refused this way changed nothing.
    """


# The Connect code, and the gateway statuses a proxy answers with, for an app that cannot answer now.
_UNAVAILABLE_CODES = frozenset({'unavailable', 'http 502', 'http 503', 'http 504'})
# The Connect codes the app's boundary (validation, authentication, membership) answers with.
_REJECTED_CODES = frozenset({'invalid_argument', 'unauthenticated', 'permission_denied', 'not_found'})
# What the app's perimeter and rpc interceptor answer, with a 403, for a caller the auth service does not admit
# (apps/web/src/proxy.ts, apps/web/src/server/rpc/interceptors.ts).
_NOT_ADMITTED = ('permission_denied', 'not admitted')
# The errors the transport raises before any answer, worth trying again on the same terms.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (requests.ConnectionError, requests.Timeout, UnavailableError)
# The errors after which the app provably did not act on the request: no token could be signed, IAP or the
# app's perimeter turned it away, or the app refused it at its boundary.
NOT_ACTED_ON: tuple[type[Exception], ...] = (AccessRefusedError, RejectedError)


def origin(project: str) -> str:
    """The app's https origin in `project`: the `themis:domain` of the stack deployed there.

    Raises:
        ValueError: If no single stack names `project`, or it sets no domain.
    """
    return f'https://{stack.value(project, "themis:domain")}'


class Workbench:
    """The app's `Workbench` rpcs, called as the service account whose JWT every request bears.

    Safe to share across threads: the token is re-signed under a lock as it nears expiry. Nothing
    here retries; `CreateAnalysis` mints an Analysis and a session, so a retried create is a second run.
    """

    def __init__(self, app_origin: str, service_account: str) -> None:
        if not app_origin.startswith('https://'):
            raise ValueError(f'the app origin must be https, since every request carries a bearer: {app_origin!r}')
        self._origin = app_origin.rstrip('/')
        self._service_account = service_account
        self._credentials = clu.iap_jwt_credentials(service_account, self._origin)
        self._audience = f'{self._origin}/*'
        self._lock = threading.Lock()

    def list_projects(self) -> workbench_pb2.ListProjectsResponse:
        return self._call('ListProjects', workbench_pb2.ListProjectsRequest(), workbench_pb2.ListProjectsResponse())

    def list_analyses(self, project_id: str) -> workbench_pb2.ListAnalysesResponse:
        return self._call(
            'ListAnalyses',
            workbench_pb2.ListAnalysesRequest(project_id=project_id),
            workbench_pb2.ListAnalysesResponse(),
        )

    def create_analysis(self, request: workbench_pb2.CreateAnalysisRequest) -> workbench_pb2.CreateAnalysisResponse:
        return self._call('CreateAnalysis', request, workbench_pb2.CreateAnalysisResponse())

    def interrupt(self, analysis_id: str) -> None:
        self._call(
            'Interrupt', workbench_pb2.InterruptRequest(analysis_id=analysis_id), workbench_pb2.InterruptResponse()
        )

    def _call[Reply: message.Message](self, method: str, request: message.Message, reply: Reply) -> Reply:
        """POST `request` to `method` and parse the answer into `reply`.

        Raises:
            AccessRefusedError: If no token could be signed, IAP refused the token, or the app refused the account.
            UnavailableError: If the app or a proxy in front of it answered that it cannot answer now.
            RejectedError: If the app refused the request at its boundary.
            RpcError: If the app answered with any other Connect error.
            requests.ConnectionError, requests.Timeout: If no answer came.
        """
        response = requests.post(
            f'{self._origin}/api/rpc/{_SERVICE}/{method}',
            data=request.SerializeToString(),
            headers={
                'Authorization': f'Bearer {self._bearer()}',
                'Content-Type': _PROTO,
                'Connect-Protocol-Version': '1',
            },
            timeout=_TIMEOUT_S,
            # A sign-in redirect is a refusal; following it would read an HTML page as a reply.
            allow_redirects=False,
        )
        self._raise_for_refusal(method, response)
        if response.status_code != http.HTTPStatus.OK:
            raise _rpc_error(method, response)
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith(_PROTO):
            # Any bytes parse as some message, an empty body as an empty one; only the type says it is a reply.
            raise RpcError(method, 'unexpected reply', f'a 200 with content-type {content_type!r}')
        reply.ParseFromString(response.content)
        return reply

    def _bearer(self) -> str:
        with self._lock:
            # `valid` turns false ahead of `exp`, so IAP never sees a token about to expire.
            if not self._credentials.valid:
                try:
                    self._credentials.refresh(google_auth_requests.Request())
                except _SIGNING_FAILURES as e:
                    raise AccessRefusedError(
                        f'no token could be signed as {self._service_account}: {e}. Either the application-default '
                        'login is spent (`gcloud auth application-default login`), or you are not in the group '
                        f'that may act as the account ({_RUNBOOK})'
                    ) from e
            token = self._credentials.token
        return token.decode('utf-8') if isinstance(token, bytes) else str(token)

    def _raise_for_refusal(self, method: str, response: requests.Response) -> None:
        if response.is_redirect:
            raise AccessRefusedError(f'IAP answered {method} with a sign-in redirect: the bearer was not accepted')
        # The app's own refusals are Connect errors in JSON; a 401 or 403 in any other shape is IAP's.
        error = _connect_error(response)
        if response.status_code == http.HTTPStatus.UNAUTHORIZED and error is None:
            raise AccessRefusedError(
                f'IAP refused the token signed by {self._service_account} for audience {self._audience}; '
                f'its body names the check: {response.text.strip()[:_BODY_EXCERPT]}'
            )
        if response.status_code == http.HTTPStatus.FORBIDDEN and error is None:
            raise AccessRefusedError(
                f'IAP or Cloud Run refused {self._service_account} after IAP accepted its token: the IAP policy on the '
                "web app's Cloud Run service, or an org policy over it, does not let the account through: "
                f'{response.text.strip()[:_BODY_EXCERPT]}'
            )
        if response.status_code == http.HTTPStatus.UNAUTHORIZED and error is not None:
            raise AccessRefusedError(
                f'IAP let {self._service_account} through and the app could not verify the IAP assertion: the '
                'audience it checks (THEMIS_PROJECT_NUMBER, THEMIS_REGION, THEMIS_WEB_SERVICE_NAME on the web '
                'service) does not name the service IAP signed for'
            )
        if response.status_code == http.HTTPStatus.FORBIDDEN and error == _NOT_ADMITTED:
            raise AccessRefusedError(
                f'the app authenticated {self._service_account} and refused it as not admitted. The auth service '
                "grants the deployment's operator account every Project the ACL holds, so either the ACL holds "
                f'none, this is not that account, or the deployment is misconfigured ({_RUNBOOK})'
            )


def _connect_error(response: requests.Response) -> tuple[str, str] | None:
    """The `(code, message)` of a Connect error body, or None for a body that is not one."""
    if not response.headers.get('content-type', '').startswith('application/json'):
        return None
    try:
        body = response.json()
    except requests.JSONDecodeError:
        return None
    if not isinstance(body, dict) or 'code' not in body:
        return None
    return str(body['code']), str(body.get('message', ''))


def _error_kind(code: str) -> type[RpcError]:
    if code in _UNAVAILABLE_CODES:
        return UnavailableError
    return RejectedError if code in _REJECTED_CODES else RpcError


def _rpc_error(method: str, response: requests.Response) -> RpcError:
    error = _connect_error(response)
    if error is None:
        code = f'http {response.status_code}'
        return _error_kind(code)(method, code, response.text.strip()[:_BODY_EXCERPT])
    code, detail = error
    if code == 'not_found':
        # The app answers a Project or Analysis outside the caller's membership as not found.
        detail += ' (the operator account reaches every Project in the ACL, so this one may not be in it)'
    return _error_kind(code)(method, code, detail)
