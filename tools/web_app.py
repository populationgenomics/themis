"""Call the deployed web app's `Workbench` rpcs from a laptop, as `themis-clu`, through IAP.

The app sits behind IAP on its Cloud Run service. IAP admits a JWT the account signs itself, whose
`aud` is the app's origin with the path wildcard, and the app authorizes the email in it like any
user's (`docs/runbooks/hand-driving-a-service.md`). The signing is IAM's, authorized by the caller's
application-default login, so the caller needs what `tools.clu` needs: membership of the `themis-clu`
group.

The rpcs are the Connect protocol over HTTP: a POST to `/api/rpc/<service>/<method>` carrying the
request message, here in its binary encoding, so the generated stubs are the whole contract and no
JSON mapping sits in between. The app's origin is the stack's own `themis:domain` (`tools.stack`), so no
URL is configured twice.
"""

from __future__ import annotations

import http
import threading

import google.auth
import google.auth.exceptions
import google.auth.iam
import google.auth.jwt
import requests
from google.auth.transport import requests as google_auth_requests
from google.protobuf import message

from themis.workbench.models import workbench_pb2
from themis.workbench.rpc import workbench_pb2 as workbench_rpc_pb2
from tools import stack

_CLOUD_PLATFORM_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
_SERVICE = workbench_rpc_pb2.DESCRIPTOR.services_by_name['Workbench'].full_name
_TIMEOUT_S = 60
_PROTO = 'application/proto'
_BODY_EXCERPT = 300
_RUNBOOK = 'docs/runbooks/hand-driving-a-service.md'
_SIGNING_FAILURES = (google.auth.exceptions.RefreshError, google.auth.exceptions.TransportError)


class AccessRefusedError(Exception):
    """No token could be signed, or IAP turned the request away before the app saw it."""


class RpcError(Exception):
    """The app answered with a Connect error."""

    def __init__(self, method: str, code: str, detail: str) -> None:
        super().__init__(f'{method}: {code}: {detail}')
        self.code = code


class UnavailableError(RpcError):
    """The app, or the proxy in front of it, said it cannot answer now: a call that may succeed if made again.

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
# The errors the transport raises before any answer, worth trying again on the same terms.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (requests.ConnectionError, requests.Timeout, UnavailableError)
# The errors after which the app provably did not act on the request: no token could be signed, IAP turned
# it away before the app saw it, or the app refused it at its boundary.
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
        # The wildcard: an `aud` of the bare origin admits `/` alone, and IAP answers 401 on every rpc path.
        self._audience = f'{self._origin}/*'
        source, _project = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
        signer = google.auth.iam.Signer(google_auth_requests.Request(), source, service_account)
        # One hour, the library's default lifetime, is IAP's ceiling.
        self._credentials = google.auth.jwt.Credentials(
            signer=signer, issuer=service_account, subject=service_account, audience=self._audience
        )
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
            AccessRefusedError: If no token could be signed, or IAP refused the token or the account.
            UnavailableError: If the app or its proxy answered that it cannot answer now.
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
        if response.status_code == http.HTTPStatus.UNAUTHORIZED:
            raise AccessRefusedError(
                f'IAP refused the token signed by {self._service_account} for audience {self._audience}; '
                f'its body names the check: {response.text.strip()[:_BODY_EXCERPT]}'
            )
        # The app's own refusals are Connect errors in JSON; a 403 in any other shape is IAP's.
        if response.status_code == http.HTTPStatus.FORBIDDEN and not _is_json(response):
            raise AccessRefusedError(
                f'IAP admitted the token and refused {self._service_account}: it holds no '
                "`roles/iap.httpsResourceAccessor` on the web app's Cloud Run service"
            )


def _is_json(response: requests.Response) -> bool:
    return response.headers.get('content-type', '').startswith('application/json')


def _error_kind(code: str) -> type[RpcError]:
    if code in _UNAVAILABLE_CODES:
        return UnavailableError
    return RejectedError if code in _REJECTED_CODES else RpcError


def _rpc_error(method: str, response: requests.Response) -> RpcError:
    if not _is_json(response):
        code = f'http {response.status_code}'
        return _error_kind(code)(method, code, response.text.strip()[:_BODY_EXCERPT])
    body = response.json()
    code = str(body.get('code', f'http {response.status_code}'))
    detail = str(body.get('message', ''))
    if code == 'not_found':
        # The app answers a Project or Analysis outside the caller's membership as not found.
        detail += ' (the account may have no project_members row for the Project named)'
    return _error_kind(code)(method, code, detail)
