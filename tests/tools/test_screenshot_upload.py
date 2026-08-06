"""Tests for tools.screenshot.upload, against ``fake-gcs-server``."""

from __future__ import annotations

import hashlib
import pathlib
import sys
import uuid

import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage

from tools.screenshot import upload

_PNG = b'\x89PNG\r\n\x1a\nfixture bytes'
_OTHER_PNG = b'\x89PNG\r\n\x1a\ndifferent bytes'
_BUCKET = 'cpg-themis-dev-pr-screenshots'


@pytest.fixture
def screenshot_bucket(gcs_client: storage.Client) -> str:
    """Creates the bucket `main` uploads to; returns the name `main` derives from ``--project``."""
    name = f'themis-test-{uuid.uuid4().hex}-{upload._BUCKET_SUFFIX}'
    gcs_client.bucket(name).create()
    return name


def _stub_client(monkeypatch: pytest.MonkeyPatch, gcs_client: storage.Client) -> None:
    """Point the client `main` constructs at the emulator, which carries its own project."""
    monkeypatch.setattr(upload.storage, 'Client', lambda project: gcs_client)  # noqa: ARG005


def _argv(bucket: str, *paths: pathlib.Path) -> list[str]:
    """The command line that makes `main` upload `paths` to the bucket named `bucket`."""
    project = bucket.removesuffix(f'-{upload._BUCKET_SUFFIX}')
    return ['upload', '--project', project, *(str(path) for path in paths)]


class TestObjectName:
    def test_is_the_sha256_of_the_content(self) -> None:
        assert upload._object_name(_PNG) == f'{hashlib.sha256(_PNG).hexdigest()}.png'

    def test_depends_on_content_not_on_the_file_it_came_from(self, tmp_path: pathlib.Path) -> None:
        # Content addressing is what makes an upload idempotent and an object immutable.
        (tmp_path / 'before.png').write_bytes(_PNG)
        (tmp_path / 'after.png').write_bytes(_PNG)
        (tmp_path / 'other.png').write_bytes(_OTHER_PNG)
        same = {upload._object_name(upload._png_bytes(tmp_path / n)) for n in ('before.png', 'after.png')}
        assert len(same) == 1
        assert upload._object_name(upload._png_bytes(tmp_path / 'other.png')) not in same


class TestPngBytes:
    def test_returns_the_bytes_of_a_png(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / 'after.png'
        path.write_bytes(_PNG)
        assert upload._png_bytes(path) == _PNG

    def test_rejects_a_file_that_is_not_a_png(self, tmp_path: pathlib.Path) -> None:
        # The bucket labels every object image/png, so a JPEG would render as a broken image.
        path = tmp_path / 'after.png'
        path.write_bytes(b'\xff\xd8\xff\xe0 JFIF')
        with pytest.raises(SystemExit, match='not a PNG'):
            upload._png_bytes(path)

    def test_rejects_a_path_that_does_not_exist(self, tmp_path: pathlib.Path) -> None:
        with pytest.raises(SystemExit, match='not a file'):
            upload._png_bytes(tmp_path / 'absent.png')


class TestMarkdownLink:
    def test_targets_the_object_over_anonymous_https(self) -> None:
        # Camo fetches the origin server-side with no credentials, so the target has to be
        # a URL that resolves without one.
        name = upload._object_name(_PNG)
        assert upload._markdown_link('after', _BUCKET, name) == (
            f'![after](https://storage.googleapis.com/{_BUCKET}/{name})'
        )


class TestUpload:
    def test_writes_once_then_reports_the_content_already_stored(self, gcs_bucket: storage.Bucket) -> None:
        name = upload._object_name(_PNG)
        assert upload._upload(gcs_bucket, name, _PNG) is True
        assert upload._upload(gcs_bucket, name, _PNG) is False
        assert gcs_bucket.blob(name).download_as_bytes() == _PNG

    def test_stores_the_object_under_the_type_that_renders(self, gcs_bucket: storage.Bucket) -> None:
        # Camo and the browser render by content-type; application/octet-stream downloads instead.
        name = upload._object_name(_PNG)
        upload._upload(gcs_bucket, name, _PNG)
        stored = gcs_bucket.get_blob(name)
        assert stored is not None
        assert stored.content_type == 'image/png'


class TestMain:
    def test_prints_one_link_per_argument_in_order(
        self,
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
            upload._markdown_link('before', screenshot_bucket, upload._object_name(_PNG)),
            upload._markdown_link('after', screenshot_bucket, upload._object_name(_OTHER_PNG)),
        ]

    def test_keeps_stdout_pastable_when_a_capture_was_already_stored(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        gcs_client: storage.Client,
        screenshot_bucket: str,
    ) -> None:
        path = tmp_path / 'after.png'
        path.write_bytes(_PNG)
        _stub_client(monkeypatch, gcs_client)
        monkeypatch.setattr(sys, 'argv', _argv(screenshot_bucket, path, path))
        upload.main()
        captured = capsys.readouterr()
        link = upload._markdown_link('after', screenshot_bucket, upload._object_name(_PNG))
        assert captured.out.splitlines() == [link, link]
        assert 'already stored' in captured.err

    def test_a_rejected_file_uploads_and_prints_nothing_at_all(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        gcs_client: storage.Client,
        screenshot_bucket: str,
    ) -> None:
        # A bad argument must not leave half a PR body on stdout and half the captures uploaded.
        (tmp_path / 'before.png').write_bytes(_PNG)
        (tmp_path / 'notes.txt').write_bytes(b'not a png')
        _stub_client(monkeypatch, gcs_client)
        monkeypatch.setattr(sys, 'argv', _argv(screenshot_bucket, tmp_path / 'before.png', tmp_path / 'notes.txt'))
        with pytest.raises(SystemExit, match='not a PNG'):
            upload.main()
        assert list(gcs_client.list_blobs(screenshot_bucket)) == []
        assert capsys.readouterr().out == ''

    def test_an_upload_that_fails_partway_prints_nothing_at_all(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        gcs_client: storage.Client,
        screenshot_bucket: str,
    ) -> None:
        # A stale credential or a dropped connection is the likelier way to reach a half-written body.
        (tmp_path / 'before.png').write_bytes(_PNG)
        (tmp_path / 'after.png').write_bytes(_OTHER_PNG)
        _stub_client(monkeypatch, gcs_client)
        stored = upload._upload

        def fail_on_the_second_capture(bucket: storage.Bucket, name: str, data: bytes) -> bool:
            if name == upload._object_name(_OTHER_PNG):
                raise api_exceptions.ServiceUnavailable('connection dropped')
            return stored(bucket, name, data)

        monkeypatch.setattr(upload, '_upload', fail_on_the_second_capture)
        monkeypatch.setattr(sys, 'argv', _argv(screenshot_bucket, tmp_path / 'before.png', tmp_path / 'after.png'))
        with pytest.raises(api_exceptions.ServiceUnavailable):
            upload.main()
        assert capsys.readouterr().out == ''
