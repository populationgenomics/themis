"""The live channel's one call: the upload addresses the channel by id and carries the text and the chart together."""

from __future__ import annotations

from slack_sdk.web import slack_response

from themis.services.cost_exporter import slack


class _Uploader:
    def __init__(self) -> None:
        self.uploads: list[dict[str, object]] = []

    def files_upload_v2(
        self, *, channel: str, file: bytes, filename: str, title: str, initial_comment: str
    ) -> slack_response.SlackResponse:
        self.uploads.append(
            {'channel': channel, 'file': file, 'filename': filename, 'title': title, 'initial_comment': initial_comment}
        )
        return slack_response.SlackResponse(
            client=None, http_verb='POST', api_url='', req_args={}, data={'ok': True}, headers={}, status_code=200
        )


def test_the_post_is_one_upload_to_the_channel_with_the_text_as_its_comment() -> None:
    uploader = _Uploader()

    slack.SlackChannel(uploader).post('C0123', 'Themis spend', b'\x89PNG', 'themis-spend-2026-09-07.png')

    [upload] = uploader.uploads
    assert upload['channel'] == 'C0123'
    assert upload['file'] == b'\x89PNG'
    assert upload['filename'] == 'themis-spend-2026-09-07.png'
    assert upload['initial_comment'] == 'Themis spend'


def test_the_web_client_bounds_each_request_by_a_timeout() -> None:
    assert 0 < slack.web_client('xoxb-test').timeout <= 30
