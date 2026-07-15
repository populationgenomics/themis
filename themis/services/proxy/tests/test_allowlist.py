"""Tests for the Anthropic path allowlist + canonicalization (crafted-request hardening)."""

from __future__ import annotations

import pytest

from themis.services.proxy import allowlist

_SID = 'sesn_abc'
_WID = 'sesn_abc'  # the work item id is the session id (§12)
_EID = 'env_1'

_ALLOW = allowlist.AnthropicAllowlist(session_id=_SID, work_id=_WID, environment_id=_EID)


@pytest.mark.parametrize(
    'path',
    [
        f'/v1/sessions/{_SID}',  # bare session (worker startup retrieve)
        f'/v1/sessions/{_SID}/events',  # session subtree (SSE stream)
        f'/v1/sessions/{_SID}/events/send',  # deeper subtree
        f'/v1/environments/{_EID}/work/{_WID}',  # bare work item
        f'/v1/environments/{_EID}/work/{_WID}/ack',  # work subtree (ack / stop / heartbeat)
    ],
)
def test_permits_own_session_and_work_paths(path: str) -> None:
    assert _ALLOW.permits(path)


@pytest.mark.parametrize(
    'path',
    [
        '/v1/sessions/other',  # another session
        f'/v1/environments/{_EID}/work/poll',  # queue-level poll — must never be reachable
        f'/v1/environments/{_EID}/work/stats',  # queue-level stats
        '/v1/skills',  # org-wide skills, deliberately excluded
        f'/v1/environments/{_EID}/work/other/ack',  # another work item
        f'/v1/sessions/{_SID}extra',  # prefix but not a path boundary
    ],
)
def test_rejects_other_paths(path: str) -> None:
    assert not _ALLOW.permits(path)


@pytest.mark.parametrize(
    'path',
    [
        f'/v1/sessions/{_SID}/../skills',  # literal traversal out of the sid subtree
        f'/v1/sessions/{_SID}%2f..%2fskills',  # encoded-slash traversal
        f'/v1/sessions/{_SID}/%2e%2e/skills',  # encoded-dot traversal (decodes to ..)
        '/../v1/skills',  # escapes root
        f'/v1/sessions/{_SID}/..%5c..',  # encoded backslash
    ],
)
def test_rejects_traversal_and_encoded_separators(path: str) -> None:
    assert not _ALLOW.permits(path)


def test_canonicalize_resolves_dot_segments() -> None:
    assert allowlist.canonicalize('/v1/./sessions//x/../y') == '/v1/sessions/y'


def test_canonicalize_rejects_encoded_slash() -> None:
    assert allowlist.canonicalize('/a%2fb') is None


def test_canonicalize_rejects_root_escape() -> None:
    assert allowlist.canonicalize('/a/../..') is None
