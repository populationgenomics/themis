"""A Job's settings from its environment: every variable required, and a miss or a malformed value exits naming it.

Both entrypoints of the image (`__main__`, `report`) read their whole configuration before any network call, so a
misconfigured Job fails at startup with the variable in the message, never partway through a run.
"""

from __future__ import annotations

from collections.abc import Mapping


def require(environ: Mapping[str, str], name: str) -> str:
    """The value of `name`; unset or empty exits naming it."""
    value = environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


def positive_seconds(environ: Mapping[str, str], name: str) -> int:
    """The value of `name` as a positive whole number of seconds; anything else exits naming it."""
    value = require(environ, name)
    if not value.isdigit() or int(value) == 0:
        raise SystemExit(f'{name} must be a positive whole number of seconds, got {value!r}')
    return int(value)
