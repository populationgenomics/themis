"""A Google Cloud Storage backend.

`ifGenerationMatch` on a single object is the compare-and-swap the protocol needs, and
`ifGenerationMatch=0` means "only if absent", which is how a repository is created exactly once.
Object versioning on the bucket makes every superseded ref document a retained noncurrent
generation — the durable reflog, at no cost in code. A download URL is signed V4 through IAM
Credentials `signBlob` as a named service account, so no key file is ever held. Design:
`docs/design/sheaf.md`.

Each request to GCS is a client span, a child of whatever span is current (the rpc's, in the sheaf
service), and so is each download URL's signature, one IAM `signBlob` call under an `IamSigner`. A
listing is the exception: the client fetches its pages lazily, as the caller iterates.
"""

from __future__ import annotations

import contextlib
import datetime
import threading
from collections.abc import Iterator
from typing import override

import google.auth
from google.api_core import exceptions as api_exceptions
from google.auth import credentials as auth_credentials
from google.auth import iam
from google.auth.transport import requests as auth_requests
from google.cloud import storage
from opentelemetry import trace

from themis.sheaf import backend, errors

# GCS spells "the object must not exist" as a generation precondition of zero.
MUST_NOT_EXIST = 0


_READ_ATTEMPTS = 8
# What an access token needs to call IAM Credentials' signBlob.
_CLOUD_PLATFORM_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
# Signed URLs are path-style on this origin (`<endpoint>/<bucket>/<object>`), whatever endpoint the client
# talks to: the workbench's content security policy admits the sheaf bucket's path here and nothing else.
SIGNED_URL_ENDPOINT = 'https://storage.googleapis.com'

_TRACER = trace.get_tracer(__name__)


def _generation_of(blob: storage.Blob) -> backend.Generation:
    """The generation the server assigned `blob`.

    Raises:
        SheafError: If the client carries none. A locally-constructed blob has no generation; one
            the server has answered for always does, so its absence means the response was not the
            round-trip this code assumes and the precondition cannot be trusted.
    """
    if blob.generation is None:
        raise errors.SheafError(f'{blob.name}: the storage client reported no generation')
    return blob.generation


def _size_of(blob: storage.Blob) -> int:
    """The blob's size in bytes.

    Raises:
        SheafError: If the client carries none. Compaction's ratio is computed from it, so a
            default silently reprices the manifest it is judging.
    """
    if blob.size is None:
        raise errors.SheafError(f'{blob.name}: the storage client reported no size')
    return blob.size


class IamSigner(auth_credentials.Signing):
    """Signs as one service account through IAM Credentials `signBlob`, with no key file.

    The process's own credentials authorise each signature, so they need `signBlob` on `account` —
    on Cloud Run, the runtime account holding it on itself. They are resolved on the first
    signature rather than at construction, so a backend can be built where no credentials exist.
    """

    def __init__(self, account: str) -> None:
        """Sign as `account`, a service account's email.

        Raises:
            ValueError: If `account` is empty.
        """
        if not account:
            raise ValueError('a signer needs the email of the service account it signs as')
        self.account = account
        self._lock = threading.Lock()
        self._iam: iam.Signer | None = None

    def _iam_signer(self) -> iam.Signer:
        with self._lock:
            if self._iam is None:
                credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
                self._iam = iam.Signer(auth_requests.Request(), credentials, self.account)
            return self._iam

    @override
    def sign_bytes(self, message: bytes) -> bytes:
        """Sign `message` as the account, one IAM `signBlob` call.

        Raises:
            google.auth.exceptions.DefaultCredentialsError: If the process has no credentials.
            google.auth.exceptions.TransportError: If IAM refuses or fails the signature.
        """
        return self._iam_signer().sign(message)

    @property
    @override
    def signer_email(self) -> str:
        return self.account

    @property
    @override
    def signer(self) -> iam.Signer:
        return self._iam_signer()


class GcsBackend(backend.Backend):
    """Object-store semantics over one GCS bucket, optionally under a key prefix."""

    def __init__(self, bucket: storage.Bucket, prefix: str = '', *, signer: auth_credentials.Signing | None) -> None:
        """Serve `bucket`, every key under `prefix`.

        Args:
            bucket: The bucket, bound to a client whose credentials hold a role on it.
            prefix: Key prefix every object lives under.
            signer: The identity download URLs are signed as — in a deployment, an `IamSigner` — or
                None where nothing signs, and then `sign_immutable` refuses. A signed URL reads with
                the signer's own permission, so it should be the account the client's credentials are.

        Raises:
            ValueError: The bucket handle carries no name, so no request could address it.
        """
        if bucket.name is None:
            raise ValueError('precondition failed: the bucket handle carries no name')
        self.bucket = bucket
        self._bucket_name: str = bucket.name
        self.prefix = prefix.strip('/')
        self.signer = signer

    def _key(self, key: str) -> str:
        return f'{self.prefix}/{key}' if self.prefix else key

    def _request(self, operation: str, blob_name: str) -> contextlib.AbstractContextManager[trace.Span]:
        """A client span around one request to GCS, named for its operation."""
        return _TRACER.start_as_current_span(
            f'gcs.{operation}',
            kind=trace.SpanKind.CLIENT,
            attributes={'gcs.bucket': self._bucket_name, 'gcs.object': blob_name},
        )

    @override
    def get_mutable(self, key: str) -> backend.StoredBlob:
        """Read the live generation of `key`.

        Raises:
            NotFound: If the object does not exist.
            SheafError: If the client reports no generation for it.
        """
        full = self._key(key)
        for _ in range(_READ_ATTEMPTS):
            with self._request('get_blob', full):
                blob = self.bucket.get_blob(full)
            if blob is None:
                raise errors.NotFound(key)
            generation = _generation_of(blob)
            # Pin the download to the generation just observed, so the bytes and the token agree
            # even if another writer lands mid-read. On an unversioned bucket that overwrite makes
            # the pinned generation unfetchable, so the read starts over.
            try:
                with self._request('download', full):
                    data = blob.download_as_bytes(if_generation_match=generation)
            except (api_exceptions.NotFound, api_exceptions.PreconditionFailed):
                continue
            return backend.StoredBlob(data=data, generation=generation)
        raise errors.SheafError(f'{key}: overwritten on every one of {_READ_ATTEMPTS} reads')

    @override
    def cas_mutable(self, key: str, data: bytes, expected: backend.Generation | None) -> backend.Generation:
        """Write `key` only if its generation is still `expected`.

        Raises:
            PreconditionFailed: If the generation moved, or the object exists and `expected` is
                None.
            SheafError: If the client reports no generation for the object it just wrote.
        """
        full = self._key(key)
        blob = self.bucket.blob(full)
        precondition = MUST_NOT_EXIST if expected is None else expected
        try:
            with self._request('upload', full):
                blob.upload_from_string(
                    data,
                    content_type='application/x-protobuf',
                    if_generation_match=precondition,
                )
        except api_exceptions.PreconditionFailed as exc:
            raise errors.PreconditionFailed(f'{key}: generation {expected} is stale') from exc
        return _generation_of(blob)

    @override
    def history_mutable(self, key: str) -> list[backend.StoredBlob]:
        """Return retained generations of `key`, newest first.

        Needs object versioning on the bucket; without it only the live generation comes back.

        Raises:
            SheafError: If the client reports no generation for a listed version.
        """
        full = self._key(key)
        with self._request('list_versions', full):
            versions = [
                (b, _generation_of(b)) for b in self.bucket.list_blobs(prefix=full, versions=True) if b.name == full
            ]
        versions.sort(key=lambda pair: pair[1], reverse=True)
        history = []
        for blob, generation in versions:
            with self._request('download', full):
                data = blob.download_as_bytes(if_generation_match=generation)
            history.append(backend.StoredBlob(data=data, generation=generation))
        return history

    @override
    def put_immutable(self, key: str, data: bytes) -> None:
        """Upload an immutable object unless one is already at `key`.

        `if_generation_match=0` is GCS's create-if-absent; the failed precondition is the existing
        object, which content addressing makes identical. One request, and the precondition puts the
        upload under the client's default retry policy.
        """
        full = self._key(key)
        with self._request('upload', full) as span:
            try:
                self.bucket.blob(full).upload_from_string(
                    data, content_type='application/x-git-packed-objects', if_generation_match=0
                )
            except api_exceptions.PreconditionFailed:
                # Not a failed request in the trace: the object is already there, which is the outcome asked for.
                span.set_attribute('gcs.already_present', True)

    @override
    def get_immutable(self, key: str) -> bytes:
        """Download an immutable object.

        Raises:
            NotFound: If the object is absent.
        """
        full = self._key(key)
        blob = self.bucket.blob(full)
        try:
            with self._request('download', full):
                return blob.download_as_bytes()
        except api_exceptions.NotFound as exc:
            raise errors.NotFound(key) from exc

    @override
    def list_immutable(self, prefix: str) -> Iterator[backend.ObjectInfo]:
        """Enumerate immutable objects under `prefix`."""
        offset = len(self.prefix) + 1 if self.prefix else 0
        for blob in self.bucket.list_blobs(prefix=self._key(prefix)):
            yield backend.ObjectInfo(key=blob.name[offset:], size=_size_of(blob))

    @override
    def sign_immutable(self, key: str, lifetime: datetime.timedelta) -> backend.SignedUrl:
        """A V4 signed GET URL for `key`, path-style on `SIGNED_URL_ENDPOINT`, signed as the backend's signer.

        Raises:
            SigningUnsupported: If the backend was built without a signer.
            google.auth.exceptions.TransportError: If IAM refuses or fails the signature.
        """
        if self.signer is None:
            raise errors.SigningUnsupported(f'{key}: this bucket backend was built with no account to sign as')
        # Taken before signing and floored to the second, as the URL's own X-Goog-Date is, so this expiry is never
        # after the URL's.
        issued = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
        full = self._key(key)
        with self._request('sign_url', full):
            url = self.bucket.blob(full).generate_signed_url(
                version='v4',
                expiration=lifetime,
                method='GET',
                api_access_endpoint=SIGNED_URL_ENDPOINT,
                virtual_hosted_style=False,
                credentials=self.signer,
            )
        return backend.SignedUrl(url=url, expire_time=issued + lifetime)
