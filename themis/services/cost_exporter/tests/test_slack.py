"""``SlackChannel.post`` driven over ``slack_sdk.WebClient`` against a loopback Slack HTTP server."""

from __future__ import annotations

import contextlib
import dataclasses
import http.server
import json
import threading
import urllib.parse
from collections.abc import Iterator
from typing import override

import pytest
from slack_sdk import errors as slack_errors

from themis.services.cost_exporter import slack


@dataclasses.dataclass
class _RecordedUpload:
    auth_headers: list[str | None] = dataclasses.field(default_factory=list)
    requested_filename: str | None = None
    requested_length: int | None = None
    uploaded_bytes: bytes | None = None
    completed_channel: str | None = None
    completed_comment: str | None = None
    completed_files: list[dict[str, object]] = dataclasses.field(default_factory=list)


@contextlib.contextmanager
def _slack_loopback(*, complete_error: str | None = None) -> Iterator[tuple[str, _RecordedUpload]]:
    recorded = _RecordedUpload()

    class _Handler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _reply_json(self, payload: dict[str, object]) -> None:
            raw = json.dumps(payload).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length)
            assert isinstance(self.server, http.server.HTTPServer)
            port = self.server.server_port
            if self.path == '/api/files.getUploadURLExternal':
                recorded.auth_headers.append(self.headers.get('Authorization'))
                params = urllib.parse.parse_qs(body.decode('utf-8'))
                recorded.requested_filename = params['filename'][0]
                recorded.requested_length = int(params['length'][0])
                self._reply_json(
                    {'ok': True, 'file_id': 'F0123', 'upload_url': f'http://127.0.0.1:{port}/upload/F0123'}
                )
            elif self.path == '/upload/F0123':
                recorded.uploaded_bytes = body
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'OK')
            elif self.path == '/api/files.completeUploadExternal':
                recorded.auth_headers.append(self.headers.get('Authorization'))
                if complete_error is not None:
                    self._reply_json({'ok': False, 'error': complete_error})
                    return
                params = urllib.parse.parse_qs(body.decode('utf-8'))
                recorded.completed_channel = params['channel_id'][0]
                recorded.completed_comment = params['initial_comment'][0]
                recorded.completed_files = json.loads(params['files'][0])
                self._reply_json({'ok': True, 'files': recorded.completed_files})
            else:
                self.send_error(404)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/api/', recorded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_post_is_one_upload_to_the_channel_with_the_text_as_its_comment() -> None:
    with _slack_loopback() as (base_url, recorded):
        client = slack.web_client('xoxb-test')
        client.base_url = base_url
        slack.SlackChannel(client).post('C0123', 'Themis spend', b'\x89PNG', 'themis-spend-2026-09-07.png')

    assert recorded.auth_headers == ['Bearer xoxb-test', 'Bearer xoxb-test']
    assert recorded.requested_filename == 'themis-spend-2026-09-07.png'
    assert recorded.requested_length == 4
    assert recorded.uploaded_bytes == b'\x89PNG'
    assert recorded.completed_channel == 'C0123'
    assert recorded.completed_comment == 'Themis spend'
    assert recorded.completed_files == [{'id': 'F0123', 'title': 'themis-spend-2026-09-07.png'}]


def test_a_slack_refusal_surfaces_as_a_slack_api_error() -> None:
    with _slack_loopback(complete_error='channel_not_found') as (base_url, _recorded):
        client = slack.web_client('xoxb-test')
        client.base_url = base_url
        with pytest.raises(slack_errors.SlackApiError, match='channel_not_found'):
            slack.SlackChannel(client).post('C0123', 'Themis spend', b'\x89PNG', 'themis-spend-2026-09-07.png')
