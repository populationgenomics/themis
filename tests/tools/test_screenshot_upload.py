"""Tests for ``tools.screenshot.upload`` through ``main()``, against ``fake-gcs-server``."""

from __future__ import annotations

import contextlib
import http.server
import pathlib
import sys
import threading
import uuid
from collections.abc import Iterator
from typing import override

import pytest
import requests
from google.api_core import exceptions as api_exceptions
from google.auth import credentials
from google.cloud import storage

from tools.screenshot import upload

_PNG = b'\x89PNG\r\n\x1a\nfixture bytes'
_PNG_OBJECT = '6d7acc39fe2298434e87a151c2ba57d5655edb0216dc46e4b39aebba4d6d3228.png'
_OTHER_PNG = b'\x89PNG\r\n\x1a\ndifferent bytes'
_OTHER_PNG_OBJECT = '96b5934a15008b015a0b91f22884f0aa0bfd0a617c511412f1c2c8f9b26bd039.png'


@pytest.fixture
def screenshot_bucket(gcs_client: storage.Client) -> str:
    """Creates the bucket ``main`` uploads to; returns the name ``main`` derives from ``--project``."""
    name = f'themis-test-{uuid.uuid4().hex}-pr-screenshots'
    gcs_client.bucket(name).create()
    return name


def _stub_client(monkeypatch: pytest.MonkeyPatch, gcs_client: storage.Client) -> None:
    """Point the client ``main`` constructs at the emulator."""
    monkeypatch.setattr(upload.storage, 'Client', lambda project: gcs_client)  # noqa: ARG005


def _argv(bucket: str, *paths: pathlib.Path) -> list[str]:
    """The command line that makes ``main`` upload ``paths`` to ``bucket``."""
    project = bucket.removesuffix('-pr-screenshots')
    return ['upload', '--project', project, *(str(path) for path in paths)]


@contextlib.contextmanager
def _fail_second_upload_proxy(upstream: str) -> Iterator[storage.Client]:
    """A proxy to ``fake-gcs-server`` that forwards the first object upload and denies the second."""
    uploads = 0
    lock = threading.Lock()

    class _Handler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_POST(self) -> None:
            nonlocal uploads
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            with lock:
                uploads += 1
                current = uploads
            if current >= 2:
                payload = b'{"error":{"code":403,"message":"denied"}}'
                self.send_response(403)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            forwarded_headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'content-length')}
            resp = requests.post(f'{upstream}{self.path}', data=body, headers=forwarded_headers, timeout=10)
            self.send_response(resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ('transfer-encoding', 'content-encoding', 'content-length', 'connection'):
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(resp.content)))
            self.end_headers()
            self.wfile.write(resp.content)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        client = storage.Client(
            project='themis-test',
            credentials=credentials.AnonymousCredentials(),
            client_options={'api_endpoint': f'http://{host}:{port}'},
        )
        with contextlib.closing(client):
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_prints_one_anonymous_https_markdown_link_per_argument_in_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gcs_client: storage.Client,
    screenshot_bucket: str,
) -> None:
    (tmp_path / 'before.png').write_bytes(_PNG)
    (tmp_path / 'after.png').write_bytes(_OTHER_PNG)
    _stub_client(monkeypatch, gcs_client)
    monkeypatch.setattr(sys, 'argv', _argv(screenshot_bucket, tmp_path / 'before.png', tmp_path / 'after.png'))

    upload.main()

    assert capsys.readouterr().out.splitlines() == [
        f'![before](https://storage.googleapis.com/{screenshot_bucket}/{_PNG_OBJECT})',
        f'![after](https://storage.googleapis.com/{screenshot_bucket}/{_OTHER_PNG_OBJECT})',
    ]


def test_stores_the_capture_bytes_under_image_png_so_camo_and_browsers_render(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    gcs_client: storage.Client,
    screenshot_bucket: str,
) -> None:
    path = tmp_path / 'after.png'
    path.write_bytes(_PNG)
    _stub_client(monkeypatch, gcs_client)
    monkeypatch.setattr(sys, 'argv', _argv(screenshot_bucket, path))

    upload.main()

    stored = gcs_client.bucket(screenshot_bucket).get_blob(_PNG_OBJECT)
    assert stored is not None
    assert stored.content_type == 'image/png'
    assert stored.download_as_bytes() == _PNG


def test_identical_content_under_different_filenames_writes_once_and_keeps_stdout_pastable(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gcs_client: storage.Client,
    screenshot_bucket: str,
) -> None:
    (tmp_path / 'before.png').write_bytes(_PNG)
    (tmp_path / 'after.png').write_bytes(_PNG)
    _stub_client(monkeypatch, gcs_client)
    monkeypatch.setattr(sys, 'argv', _argv(screenshot_bucket, tmp_path / 'before.png', tmp_path / 'after.png'))

    upload.main()

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        f'![before](https://storage.googleapis.com/{screenshot_bucket}/{_PNG_OBJECT})',
        f'![after](https://storage.googleapis.com/{screenshot_bucket}/{_PNG_OBJECT})',
    ]
    assert 'already stored' in captured.err
    assert [b.name for b in gcs_client.list_blobs(screenshot_bucket)] == [_PNG_OBJECT]


def _forbid_client(*_args: object, **_kwargs: object) -> storage.Client:
    raise AssertionError('storage.Client must not be opened when an input path fails validation')


def test_a_non_png_file_uploads_and_prints_nothing_at_all(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every file is validated before any upload starts, so a bad file never opens GCS or prints.
    (tmp_path / 'before.png').write_bytes(_PNG)
    (tmp_path / 'notes.png').write_bytes(b'\xff\xd8\xff\xe0 JFIF')
    monkeypatch.setattr(upload.storage, 'Client', _forbid_client)
    monkeypatch.setattr(
        sys, 'argv', _argv('themis-test-pr-screenshots', tmp_path / 'before.png', tmp_path / 'notes.png')
    )

    with pytest.raises(SystemExit, match='not a PNG'):
        upload.main()

    assert capsys.readouterr().out == ''


def test_a_missing_file_uploads_and_prints_nothing_at_all(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / 'before.png').write_bytes(_PNG)
    monkeypatch.setattr(upload.storage, 'Client', _forbid_client)
    monkeypatch.setattr(
        sys, 'argv', _argv('themis-test-pr-screenshots', tmp_path / 'before.png', tmp_path / 'absent.png')
    )

    with pytest.raises(SystemExit, match='not a file'):
        upload.main()

    assert capsys.readouterr().out == ''


def test_an_upload_that_fails_partway_prints_nothing_at_all(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gcs_endpoint: str,
    screenshot_bucket: str,
) -> None:
    (tmp_path / 'before.png').write_bytes(_PNG)
    (tmp_path / 'after.png').write_bytes(_OTHER_PNG)
    monkeypatch.setattr(sys, 'argv', _argv(screenshot_bucket, tmp_path / 'before.png', tmp_path / 'after.png'))

    with _fail_second_upload_proxy(gcs_endpoint) as failing_client:
        _stub_client(monkeypatch, failing_client)
        with pytest.raises(api_exceptions.Forbidden):
            upload.main()

    assert capsys.readouterr().out == ''
