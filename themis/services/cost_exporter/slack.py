"""Posting the report to Slack: the message and the chart as one file upload to the spend channel.

A file upload addresses its channel by id and carries the message as the upload's comment, so the report is one post
and one API path.
"""

from __future__ import annotations

import abc
from typing import Protocol, override

import slack_sdk
from slack_sdk.web import slack_response

# Per request; the SDK makes several for one upload, and the run's deadline is checked before the post.
_REQUEST_TIMEOUT_SECONDS = 30


class ReportChannel(abc.ABC):
    """Where the report goes: one post carrying the text and the chart."""

    @abc.abstractmethod
    def post(self, channel_id: str, text: str, png: bytes, filename: str) -> None:
        """Post `text` with the chart attached as `filename`; raise rather than post part of it."""


class FileUploader(Protocol):
    """The one Slack call the channel makes; `slack_sdk.WebClient` satisfies it."""

    def files_upload_v2(
        self, *, channel: str, file: bytes, filename: str, title: str, initial_comment: str
    ) -> slack_response.SlackResponse: ...


class SlackChannel(ReportChannel):
    """The live channel: a file upload through the Slack app's bot token.

    The SDK validates every step of the upload and raises its own errors: `SlackApiError`, naming Slack's error, on
    an answer with `ok` false, and `SlackRequestError` when the file upload itself fails.
    """

    def __init__(self, client: FileUploader) -> None:
        self._client = client

    @override
    def post(self, channel_id: str, text: str, png: bytes, filename: str) -> None:
        self._client.files_upload_v2(
            channel=channel_id, file=png, filename=filename, title=filename, initial_comment=text
        )


def web_client(token: str) -> slack_sdk.WebClient:
    """The SDK client for the bot token, each request bounded by its own timeout."""
    return slack_sdk.WebClient(token=token, timeout=_REQUEST_TIMEOUT_SECONDS)
