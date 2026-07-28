"""Consent once in a browser, then mint a developer ID token per run.

IAP admits an ID token whose ``aud`` is an OAuth client on the backend's
``accessSettings.oauthSettings.programmaticClients`` allowlist. Themis allowlists a dedicated
Desktop client, so the token Google returns for that client already carries the audience IAP
wants and no audience swap against a second client is needed
(``docs/runbooks/iap-access.md``).

``authorize`` runs the loopback consent once and writes the refresh token to a 0600 cache;
``mint`` exchanges it for a fresh ID token on every run. The client secret is cached beside the
refresh token because neither half authenticates without the other, so separating them would
protect nothing; it reaches ``authorize`` from the caller, which reads it out of encrypted
Pulumi config.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import pathlib
import tempfile

from google.auth import exceptions as google_auth_exceptions
from google.auth import jwt as google_auth_jwt
from google.auth import transport as google_auth_transport
from google.oauth2 import credentials as oauth2_credentials
from google_auth_oauthlib import flow as oauth_flow
from oauthlib.oauth2.rfc6749 import errors as oauth_errors

# The canonical form of `email`: Google echoes the granted scope back this way, and oauthlib
# raises on any difference between what was asked for and what came back.
_SCOPES = ('openid', 'https://www.googleapis.com/auth/userinfo.email')
_AUTH_URI = 'https://accounts.google.com/o/oauth2/v2/auth'
_TOKEN_URI = 'https://oauth2.googleapis.com/token'  # noqa: S105 — an endpoint URL, not a secret

_CACHE_ENV = 'THEMIS_IAP_CREDENTIALS'

_LOGIN = 'uv run --group iap python -m themis.clients.iap login'

EXPIRY_MARGIN = datetime.timedelta(minutes=5)
_CONSENT_TIMEOUT_S = 300


class ConsentRequiredError(Exception):
    """No cached consent on this machine; the one-time browser flow has not run."""


class ConsentRejectedError(Exception):
    """Google refused the cached refresh token — revoked, expired, or a rotated client secret."""


@dataclasses.dataclass(frozen=True)
class OAuthClient:
    """The Desktop OAuth client tokens are minted from; its id is every token's ``aud``."""

    client_id: str
    client_secret: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class StoredCredentials:
    """A consented Desktop client plus the refresh token that consent yielded."""

    client: OAuthClient
    refresh_token: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class IdToken:
    """A minted ID token with the two claims a caller acts on: whose it is, and when it dies."""

    value: str
    email: str
    expires_at: datetime.datetime

    def expired(self, now: datetime.datetime) -> bool:
        """Whether the token is spent.

        Anything within ``EXPIRY_MARGIN`` of expiry counts as spent: a token handed out nearer
        than that can die in flight and reach IAP as a rejection the caller cannot tell apart
        from a revoked consent.
        """
        return now + EXPIRY_MARGIN >= self.expires_at


def default_cache_path() -> pathlib.Path:
    """The cache location: ``$THEMIS_IAP_CREDENTIALS``, else ``$XDG_CONFIG_HOME/themis/iap.json``."""
    override = os.environ.get(_CACHE_ENV)
    if override:
        return pathlib.Path(override)
    config_home = os.environ.get('XDG_CONFIG_HOME')
    base = pathlib.Path(config_home) if config_home else pathlib.Path.home() / '.config'
    return base / 'themis' / 'iap.json'


def load(path: pathlib.Path) -> StoredCredentials:
    """Read the cached consent.

    Args:
        path: The cache file, as returned by ``default_cache_path``.

    Returns:
        The stored client and refresh token.

    Raises:
        ConsentRequiredError: If the file does not exist.
        ValueError: If it exists but is malformed. A truncated or hand-edited cache is an
            error to surface, not a reason to silently re-prompt.
    """
    try:
        raw = path.read_text('utf-8')
    except FileNotFoundError as e:
        raise ConsentRequiredError(f'no cached consent at {path}; run `{_LOGIN}` once to create it') from e
    try:
        cached = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f'cached consent at {path} is not valid JSON: {e}; delete it and re-run `{_LOGIN}`') from e
    if not isinstance(cached, dict):
        raise ValueError(f'cached consent at {path} is not a JSON object; delete it and re-run `{_LOGIN}`')
    unusable = [
        field
        for field in ('client_id', 'client_secret', 'refresh_token')
        if not isinstance(cached.get(field), str) or not cached[field]
    ]
    if unusable:
        raise ValueError(
            f'cached consent at {path} is missing or malformed: {", ".join(unusable)}; delete it and re-run `{_LOGIN}`'
        )
    return StoredCredentials(
        client=OAuthClient(client_id=cached['client_id'], client_secret=cached['client_secret']),
        refresh_token=cached['refresh_token'],
    )


def save(stored: StoredCredentials, path: pathlib.Path) -> None:
    """Write the consent to ``path``, readable only by its owner.

    Written to a sibling temp file and renamed over ``path``: an interrupted write would
    otherwise leave a truncated cache, which ``load`` refuses rather than silently repairs.

    Raises:
        OSError: If ``path``'s directory cannot be created or written.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp opens O_EXCL|O_NOFOLLOW at 0600 under a name it alone holds: a name someone else
    # planted is refused, and a temp file a killed process left behind cannot collide.
    fd, name = tempfile.mkstemp(prefix=f'{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(
                {
                    'client_id': stored.client.client_id,
                    'client_secret': stored.client.client_secret,
                    'refresh_token': stored.refresh_token,
                },
                handle,
                indent=2,
            )
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def authorize(client: OAuthClient, path: pathlib.Path) -> StoredCredentials:
    """Run the one-time browser consent and cache the refresh token it yields.

    Serves the redirect on an ephemeral loopback port, which a Desktop client may use without
    registering it. Out-of-band (``urn:ietf:wg:oauth:2.0:oob``) redirection is not used; Google
    withdrew support for it.

    Args:
        client: The Desktop OAuth client to consent to.
        path: Where to cache the result.

    Returns:
        The cached consent, already written to ``path``.

    Raises:
        ValueError: If the consent did not yield a grant to cache — the browser flow was
            abandoned, refused, or completed without a refresh token.
        OSError: If the grant cannot be written to ``path``. The consent is spent by then, so a
            caller has to re-run the flow once the location is writable.
    """
    flow = oauth_flow.InstalledAppFlow.from_client_config(
        {
            'installed': {
                'client_id': client.client_id,
                'client_secret': client.client_secret,
                'auth_uri': _AUTH_URI,
                'token_uri': _TOKEN_URI,
            }
        },
        scopes=list(_SCOPES),
    )
    # access_type=offline is what makes Google issue a refresh token at all; prompt=consent is what
    # makes a re-authorization issue another, since one comes back only on a user's first consent.
    # Both are passed rather than left to `authorization_url`'s defaults, which the cache depends on.
    try:
        consented = flow.run_local_server(
            host='127.0.0.1',
            port=0,
            access_type='offline',
            prompt='consent',
            timeout_seconds=_CONSENT_TIMEOUT_S,
        )
    except oauth_flow.WSGITimeoutError as e:
        raise ValueError(
            f'the browser consent was not completed within {_CONSENT_TIMEOUT_S} seconds. '
            f'Re-run `{_LOGIN}` and finish the sign-in in the tab it opens.'
        ) from e
    # Before the general case: a refused client is an OAuth2Error too, but it fails the code
    # exchange after a sign-in that succeeded, so the declined wording below would misdirect.
    except oauth_errors.InvalidClientError as e:
        raise ValueError(
            f'Google refused the OAuth client itself ({e}): the id and secret do not belong to the same '
            f'client. `{_LOGIN}` reports where it read each half — check both name the client you meant, '
            'then consent again.'
        ) from e
    except oauth_errors.OAuth2Error as e:
        raise ValueError(
            f'Google refused the consent ({e}). The sign-in was declined, or the account is outside '
            f'the organisation the consent screen admits. Re-run `{_LOGIN}` as an org identity.'
        ) from e
    if not consented.refresh_token:
        raise ValueError('Google returned no refresh token, so there is nothing to cache; re-run the consent.')
    stored = StoredCredentials(client=client, refresh_token=consented.refresh_token)
    save(stored, path)
    return stored


def mint(stored: StoredCredentials, request: google_auth_transport.Request) -> IdToken:
    """Exchange the cached refresh token for a fresh ID token.

    Args:
        stored: The consent to spend.
        request: The HTTP transport the token endpoint is called over.

    Returns:
        The minted token, its subject's email, and its expiry.

    Raises:
        ConsentRejectedError: If Google refuses the refresh token.
        ValueError: If the response carries no ID token, or one whose claims IAP would not
            accept.
    """
    grant = oauth2_credentials.Credentials(
        None,
        refresh_token=stored.refresh_token,
        token_uri=_TOKEN_URI,
        client_id=stored.client.client_id,
        client_secret=stored.client.client_secret,
        scopes=list(_SCOPES),
    )
    try:
        grant.refresh(request)
    except google_auth_exceptions.RefreshError as e:
        detail = e.args[0] if e.args else e
        raise ConsentRejectedError(
            f'Google refused the cached refresh token ({detail}). It was revoked, it expired, or the '
            f'client secret rotated. Re-run `{_LOGIN}` to consent again.'
        ) from e
    if grant.id_token is None:
        raise ValueError(
            f'the token endpoint returned no ID token for scopes {" ".join(_SCOPES)}; only an access '
            'token was issued, which IAP does not accept'
        )
    return _claims(grant.id_token, client_id=stored.client.client_id)


def _claims(value: str, client_id: str) -> IdToken:
    """Read the claims IAP keys off, rejecting a token IAP would not accept.

    The signature is not checked: the token arrived from Google's token endpoint over TLS, and
    IAP verifies it again on arrival.
    """
    claims = google_auth_jwt.decode(value, verify=False)
    audience = claims.get('aud')
    if audience != client_id:
        raise ValueError(
            f'the ID token names audience {audience!r}, not the client {client_id!r} it was requested '
            'for. IAP admits a token only when its audience is an allowlisted client, so this one would '
            'be refused.'
        )
    email = claims.get('email')
    if not email:
        raise ValueError(
            f'the ID token carries no `email` claim, which IAP and the app both key off. The '
            f'consent granted scopes other than {" ".join(_SCOPES)}; re-run `{_LOGIN}`.'
        )
    expires_at = claims.get('exp')
    if expires_at is None:
        raise ValueError('the ID token carries no `exp` claim, so its lifetime is unknown')
    return IdToken(
        value=value,
        email=email,
        expires_at=datetime.datetime.fromtimestamp(expires_at, tz=datetime.UTC),
    )
