"""Issue requests to an IAP-protected Themis environment as the consented developer.

Three gates stand between a request and an answer: IAP admits the token's audience, IAP's IAM
policy admits the identity, and the app admits it again against ``project_members``. Each
refuses differently, and the third refuses as not-found by design, so the statuses are
translated here into what the developer must go and do (``docs/runbooks/iap-access.md``).
"""

from __future__ import annotations

import datetime
import pathlib
from collections.abc import Mapping, Sequence

import requests
from google.auth import transport as google_auth_transport
from google.auth.transport import requests as google_auth_requests

from themis.clients.iap import credentials

type JsonBody = Mapping[str, JsonBody] | Sequence[JsonBody] | str | int | float | bool | None

_TIMEOUT_S = 30.0


class AccessRefusedError(Exception):
    """IAP turned the request away before the app saw it."""


class NotProvisionedError(Exception):
    """The app answered not-found: no ``project_members`` row, or no such path."""


class Client:
    """Talks to one Themis environment as the developer who consented on this machine.

    Holds one minted ID token and replaces it as it nears expiry, so a run outlasting a token's
    hour never presents a dead one. Every response that means "you cannot reach this" is raised
    as ``AccessRefusedError`` or ``NotProvisionedError``; every other response is returned for the caller to
    read, including its own error statuses.
    """

    def __init__(
        self,
        base_url: str,
        stored: credentials.StoredCredentials,
        *,
        timeout_s: float = _TIMEOUT_S,
        request: google_auth_transport.Request | None = None,
        session: requests.Session | None = None,
    ) -> None:
        """Build a client.

        Args:
            base_url: The environment's origin, e.g. ``https://themis-dev.populationgenomics.org.au``.
            stored: The consent to mint ID tokens from.
            timeout_s: Per-request timeout. `requests` has no default, and a smoke check that
                hangs is worse than one that fails.
            request: Transport for the token endpoint. Defaults to a fresh one.
            session: Transport for the app. Defaults to a fresh one.

        Raises:
            ValueError: If ``base_url`` is not an https origin. Every request carries a live ID
                token as a bearer credential, which cleartext would put on the wire.
        """
        if not base_url.startswith('https://'):
            raise ValueError(f'base_url must be an https origin, got {base_url!r}')
        self._base_url = base_url.rstrip('/')
        self._timeout_s = timeout_s
        self._stored = stored
        self._request = request if request is not None else google_auth_requests.Request()
        self._session = session if session is not None else requests.Session()
        self._token: credentials.IdToken | None = None

    def identity(self) -> str:
        """The email the environment sees. Mints a token if none is live, so it may raise."""
        return self._id_token().email

    def get(self, path: str) -> requests.Response:
        """GET ``path`` on the environment."""
        return self.send('GET', path)

    def post(self, path: str, json_body: JsonBody) -> requests.Response:
        """POST ``json_body`` to ``path`` on the environment."""
        return self.send('POST', path, json_body)

    def send(self, method: str, path: str, json_body: JsonBody = None) -> requests.Response:
        """Issue one request bearing a live ID token.

        Args:
            method: The HTTP method.
            path: Path on the environment, e.g. ``/api/projects``.
            json_body: Body to send as JSON, if any.

        Returns:
            The response, for any status that means the request was let through.

        Raises:
            ValueError: If ``path`` is not rooted at ``/``.
            AccessRefusedError: If IAP rejected the token or the identity.
            NotProvisionedError: If the app answered not-found.
        """
        if not path.startswith('/'):
            raise ValueError(f'path must start with "/", got {path!r}')
        token = self._id_token()
        response = self._session.request(
            method,
            f'{self._base_url}{path}',
            headers={'Authorization': f'Bearer {token.value}'},
            json=json_body,
            timeout=self._timeout_s,
            # A sign-in redirect is a refusal; following it yields an HTML page under a 200.
            allow_redirects=False,
        )
        _raise_for_access(
            response,
            path=path,
            email=token.email,
            client_id=self._stored.client.client_id,
            origin=self._base_url,
        )
        return response

    def _id_token(self) -> credentials.IdToken:
        now = datetime.datetime.now(tz=datetime.UTC)
        if self._token is None or self._token.expired(now):
            self._token = credentials.mint(self._stored, self._request)
        return self._token


def from_cache(base_url: str, path: pathlib.Path | None = None) -> Client:
    """Build a client from the consent cached on this machine.

    Args:
        base_url: The environment's origin.
        path: The cache file. Defaults to ``credentials.default_cache_path()``.

    Returns:
        A client ready to issue requests.

    Raises:
        ConsentRequiredError: If the one-time browser consent has not run here.
    """
    return Client(base_url, credentials.load(path if path is not None else credentials.default_cache_path()))


def _raise_for_access(response: requests.Response, path: str, email: str, client_id: str, origin: str) -> None:
    """Translate the statuses that mean "you cannot reach this" into what to do about it.

    Args:
        response: The response to classify.
        path: The path requested, for the message.
        email: The identity the spent token carries.
        client_id: The token's audience — the discriminator between a consent for the wrong
            environment and an allowlist that has not been applied.
        origin: The environment the request went to.
    """
    if response.is_redirect:
        raise AccessRefusedError(
            f'IAP answered {path} with a redirect to {response.headers.get("Location", "a sign-in page")!r} '
            f'instead of serving it. That is the browser sign-in flow: the ID token for {email} was not '
            'accepted as a bearer credential.'
        )
    if response.status_code == 401:
        raise AccessRefusedError(
            f'The ID token for {email} was rejected, and it names audience {client_id}. IAP admits a token '
            f"only when its audience is on that environment's `themis:iapProgrammaticClients`, so either "
            f'the consent being spent is for a different environment than {origin} — one cache holds one '
            'consent, and `token` spends whichever it holds — or the allowlist carrying this client has '
            'not been applied there yet. A JSON body instead means the app rejected the assertion IAP '
            f'passed it, which is a deploy misconfiguration rather than anything to fix here. Body: '
            f'{response.text.strip()[:200]}'
        )
    if response.status_code == 403:
        raise AccessRefusedError(
            f'IAP admitted the token but not {email}: the identity lacks '
            "`roles/iap.httpsResourceAccessor`. Add it to the environment's access group — see "
            'docs/runbooks/iap-access.md.'
        )
    if response.status_code == 404:
        raise NotProvisionedError(
            f'{email} cleared IAP, and the app answered not-found for {path}. Either the path does not '
            f'exist, or {email} has no `project_members` row: the app answers a non-member with not-found '
            'rather than forbidden, so the two look identical from outside. Have a row added for the '
            'project, then retry.'
        )
