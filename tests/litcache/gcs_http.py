"""HTTP transport interception over `fake-gcs-server` for `google.cloud.storage.Bucket` tests."""

from __future__ import annotations

import contextlib
import io
import json
import urllib.parse
from collections.abc import Callable, Iterator
from typing import override

import requests
import requests.adapters
from google.auth import credentials as auth_credentials
from google.cloud import storage as gcs

type Hook = Callable[[requests.PreparedRequest, Callable[[], requests.Response]], requests.Response]


class _InterceptingAdapter(requests.adapters.HTTPAdapter):
    """Forwards HTTP calls to `fake-gcs-server` through an intercept hook."""

    def __init__(self, hook: Hook) -> None:
        super().__init__()
        self._hook = hook

    @override
    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float, float] | tuple[float, None] | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        base_send = super().send

        def forward() -> requests.Response:
            return base_send(request, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies)

        return self._hook(request, forward)


def is_upload_for(request: requests.PreparedRequest, suffix: str | None = None) -> bool:
    """Whether `request` is a multipart GCS upload, optionally matching `suffix` in the body."""
    if request.method != 'POST' or '/upload/storage/v1/' not in (request.url or ''):
        return False
    if suffix is None:
        return True
    body = request.body
    if isinstance(body, bytes):
        return suffix.encode() in body
    return isinstance(body, str) and suffix in body


def is_download_for(request: requests.PreparedRequest, suffix: str) -> bool:
    """Whether `request` is a media `GET` for an object ending in or containing `suffix`."""
    if request.method != 'GET':
        return False
    url = urllib.parse.unquote(request.url or '')
    return '/download/storage/v1/' in url and suffix in url


def is_metadata_get_for(request: requests.PreparedRequest, suffix: str) -> bool:
    """Whether `request` is an object-metadata `GET` (`reload` or `exists`) matching `suffix`."""
    if request.method != 'GET':
        return False
    url = urllib.parse.unquote(request.url or '')
    return '/storage/v1/' in url and '/download/storage/v1/' not in url and suffix in url


def response(
    request: requests.PreparedRequest,
    status_code: int,
    body: bytes,
    *,
    content_type: str = 'application/json',
) -> requests.Response:
    """Build a synthetic `requests.Response` carrying `body` on its public `raw` stream."""
    built = requests.Response()
    built.status_code = status_code
    built.raw = io.BytesIO(body)
    built.headers['Content-Type'] = content_type
    built.request = request
    return built


def precondition_failed(request: requests.PreparedRequest) -> requests.Response:
    """A 412 Precondition Failed GCS JSON error response."""
    payload = json.dumps({'error': {'code': 412, 'message': 'Precondition Failed'}}).encode()
    return response(request, 412, payload)


def not_found(request: requests.PreparedRequest) -> requests.Response:
    """A 404 Not Found GCS JSON error response."""
    payload = json.dumps({'error': {'code': 404, 'message': 'Not Found'}}).encode()
    return response(request, 404, payload)


@contextlib.contextmanager
def intercepted_bucket(
    gcs_bucket: gcs.Bucket,
    gcs_endpoint: str,
    hook: Hook,
) -> Iterator[gcs.Bucket]:
    """Yield a `gcs.Bucket` handle on `gcs_bucket.name` whose HTTP calls pass through `hook`."""
    with requests.Session() as session:
        session.mount('http://', _InterceptingAdapter(hook))
        with contextlib.closing(
            gcs.Client(
                project='themis-test',
                credentials=auth_credentials.AnonymousCredentials(),
                _http=session,
                client_options={'api_endpoint': gcs_endpoint},
            )
        ) as client:
            yield client.bucket(gcs_bucket.name)
