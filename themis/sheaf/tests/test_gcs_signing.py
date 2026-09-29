"""A download URL signed over the GCS backend, offline.

The emulator signs nothing and IAM's `signBlob` has no offline form, so the one step replaced is the
signature itself: a `Signing` credential that returns fixed bytes stands where `IamSigner` does in a
deployment. Everything around it — the URL's form, its key, its lifetime — is the storage client's own.
"""

from __future__ import annotations

import datetime
import urllib.parse
from typing import override

import pytest
from google.auth import credentials as auth_credentials
from google.cloud import storage

from themis import sheaf
from themis.sheaf.backends import gcs

_BUCKET = 'p-sheaf-repositories'
_KEY = f'ana-1/packs/{"a" * 64}.pack'
_ACCOUNT = 'themis-sheaf@p.iam.gserviceaccount.com'
_LIFETIME = datetime.timedelta(minutes=5)


class _FixedSignature(auth_credentials.Signing):
    """Signs every message with the same bytes, as `_ACCOUNT`."""

    @override
    def sign_bytes(self, message: bytes) -> bytes:
        del message
        return b'signature'

    @property
    @override
    def signer_email(self) -> str:
        return _ACCOUNT

    @property
    @override
    def signer(self) -> None:
        return None


def _backend(signer: auth_credentials.Signing | None) -> gcs.GcsBackend:
    client = storage.Client(project='p', credentials=auth_credentials.AnonymousCredentials())
    return gcs.GcsBackend(client.bucket(_BUCKET), signer=signer)


def test_a_signed_url_is_path_style_on_the_storage_origin() -> None:
    """The workbench's content security policy admits the bucket's path on this origin, and no bucket host."""
    signed = _backend(_FixedSignature()).sign_immutable(_KEY, _LIFETIME)

    url = urllib.parse.urlsplit(signed.url)
    assert f'{url.scheme}://{url.netloc}' == gcs.SIGNED_URL_ENDPOINT == 'https://storage.googleapis.com'
    assert url.path == f'/{_BUCKET}/{_KEY}'


def test_a_signed_url_is_a_v4_signature_by_the_signer_for_the_lifetime_asked() -> None:
    before = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    signed = _backend(_FixedSignature()).sign_immutable(_KEY, _LIFETIME)

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(signed.url).query)
    assert query['X-Goog-Algorithm'] == ['GOOG4-RSA-SHA256']
    assert query['X-Goog-Credential'][0].startswith(f'{_ACCOUNT}/')
    assert query['X-Goog-Expires'] == [str(int(_LIFETIME.total_seconds()))]
    assert query['X-Goog-Signature'] == [b'signature'.hex()]
    issued = datetime.datetime.strptime(query['X-Goog-Date'][0], '%Y%m%dT%H%M%SZ').replace(tzinfo=datetime.UTC)
    # The reported expiry is never after the one GCS enforces, the URL's date plus its lifetime.
    assert before + _LIFETIME <= signed.expire_time <= issued + _LIFETIME


def test_a_backend_built_without_a_signer_refuses() -> None:
    with pytest.raises(sheaf.SigningUnsupported):
        _backend(None).sign_immutable(_KEY, _LIFETIME)


def test_an_iam_signer_names_the_account_it_signs_as() -> None:
    assert gcs.IamSigner(_ACCOUNT).signer_email == _ACCOUNT
    with pytest.raises(ValueError, match='email'):
        gcs.IamSigner('')
