"""Tests for the webhook signature verification (Standard Webhooks scheme)."""

from __future__ import annotations

import base64
import datetime

import pytest
import standardwebhooks

from themis.services.dispatcher import webhook

_SIGNING_KEY = f'whsec_{base64.b64encode(b"a-32-byte-webhook-signing-secret!").decode()}'


def _signed(body: bytes, *, webhook_id: str = 'msg_1', at: datetime.datetime | None = None) -> dict[str, str]:
    timestamp = at or datetime.datetime.now(datetime.UTC)
    signature = standardwebhooks.Webhook(_SIGNING_KEY).sign(webhook_id, timestamp, body.decode())
    return {
        'webhook-id': webhook_id,
        'webhook-timestamp': str(int(timestamp.timestamp())),
        'webhook-signature': signature,
    }


def test_accepts_a_valid_signature() -> None:
    body = b'{"type":"event","data":{"type":"session.status_run_started"}}'
    webhook.verify(_signed(body), body, _SIGNING_KEY)  # does not raise


def test_case_insensitive_headers() -> None:
    body = b'{"type":"event"}'
    headers = {key.title(): value for key, value in _signed(body).items()}  # Webhook-Id, ...
    webhook.verify(headers, body, _SIGNING_KEY)


def test_rejects_a_tampered_body() -> None:
    headers = _signed(b'original')
    with pytest.raises(webhook.SignatureError):
        webhook.verify(headers, b'tampered', _SIGNING_KEY)


def test_rejects_a_stale_delivery() -> None:
    body = b'{}'
    stale = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10)
    with pytest.raises(webhook.SignatureError):
        webhook.verify(_signed(body, at=stale), body, _SIGNING_KEY)


def test_rejects_missing_headers() -> None:
    with pytest.raises(webhook.SignatureError):
        webhook.verify({}, b'body', _SIGNING_KEY)
