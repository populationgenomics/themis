"""Path allowlist + canonicalization for the Anthropic reverse-proxy route (self-hosted-sandbox.md §6).

Confines the sandbox to *its own* session and work item on the Anthropic API. The allowed paths embed
the literal per-execution session/work/environment ids — a wildcard segment would admit other
sessions' paths and the queue-level ``/work/poll`` and ``/work/stats`` that must never be reachable.
Both the bare resource and its subtree are allowed: the worker issues a bare ``GET /v1/sessions/<sid>``
at startup, which a subtree-only pattern would 403.

Canonicalization is the security-critical part: reject an encoded slash outright (``..%2f`` traversal),
percent-decode, collapse ``.``/``..`` segments, and reject anything that escapes the root — so a
crafted path can neither match the allowed prefix nor resolve elsewhere upstream. The query string is
matched separately (stripped by the caller); ``/v1/skills`` is org-wide and deliberately absent.
"""

from __future__ import annotations

import urllib.parse


def canonicalize(raw_path: str) -> str | None:
    """Return the canonical absolute path, or ``None`` if it is unsafe.

    Args:
        raw_path: The request path (no query string).

    Returns:
        The canonical path (leading ``/``, no empty/``.`` segments, ``..`` resolved), or ``None`` if
        it contains an encoded slash/backslash, a literal backslash, or a ``..`` that escapes the root.
    """
    lowered = raw_path.lower()
    if '%2f' in lowered or '%5c' in lowered:
        return None  # an encoded slash/backslash is ambiguous — a traversal vector, reject outright
    decoded = urllib.parse.unquote(raw_path)
    if '\\' in decoded:
        return None  # a backslash may be a separator upstream
    segments: list[str] = []
    for segment in decoded.split('/'):
        if segment in ('', '.'):
            continue
        if segment == '..':
            if not segments:
                return None  # escapes the root
            segments.pop()
            continue
        segments.append(segment)
    return '/' + '/'.join(segments)


class AnthropicAllowlist:
    """Permits only this session's and work item's Anthropic paths (literal ids, bare + subtree)."""

    def __init__(self, *, session_id: str, work_id: str, environment_id: str) -> None:
        self._session_base = f'/v1/sessions/{session_id}'
        self._work_base = f'/v1/environments/{environment_id}/work/{work_id}'

    def permits(self, raw_path: str) -> bool:
        """Whether the canonicalized ``raw_path`` is this session's or work item's resource/subtree."""
        canonical = canonicalize(raw_path)
        if canonical is None:
            return False
        return any(
            canonical == base or canonical.startswith(f'{base}/') for base in (self._session_base, self._work_base)
        )
