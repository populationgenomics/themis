"""Tests for the session-token derivation's KMS response-integrity checks."""

from __future__ import annotations

import google_crc32c
import pytest

from themis.clients.auth import derive


def test_verified_bearer_accepts_a_sound_response() -> None:
    mac = b'\x01\x02\x03\x04'
    bearer = derive._verified_bearer(mac, google_crc32c.value(mac), verified_data_crc32c=True)
    assert bearer == derive._encode(mac)


def test_verified_bearer_rejects_unverified_request_crc() -> None:
    mac = b'\x01\x02\x03\x04'
    with pytest.raises(RuntimeError):
        derive._verified_bearer(mac, google_crc32c.value(mac), verified_data_crc32c=False)


def test_verified_bearer_rejects_mac_crc_mismatch() -> None:
    with pytest.raises(RuntimeError):
        derive._verified_bearer(b'\x01\x02\x03\x04', 12345, verified_data_crc32c=True)
