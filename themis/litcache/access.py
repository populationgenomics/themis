"""Validation for the flat ``Access`` message (ADR 0004, discipline 2).

``Access`` models a sum type flat — a ``string`` discriminant plus an optional
``publisher`` — because ``@typespec/protobuf`` emits no ``oneof``. The flat shape makes
the illegal states (``licensed`` without a publisher, or a publisher on a non-``licensed``
access) *representable*, so the invariant is enforced here — fail-loud, at the read/write
boundary — rather than structurally.
"""

from __future__ import annotations

from themis.litcache.models import litcache_pb2

# The access vocabulary. A `string` field, not a proto enum (the values are hyphenated),
# so membership is checked here rather than by the schema (ADR 0004).
ACCESS_VALUES = frozenset({'free-to-read', 'licensed', 'institution-captured', 'unknown'})


def validate_access(access: litcache_pb2.Access) -> None:
    """Enforce the ``Access`` invariants, raising on violation.

    Args:
        access: The ``Access`` message to check.

    Raises:
        ValueError: ``access.access`` is not a known value, or a non-empty ``publisher``
            is present iff ``access == "licensed"`` (a non-empty publisher is required for
            ``licensed`` and forbidden otherwise). An empty-string publisher counts as
            absent — an ``optional string`` reads as ``''`` when unset.
    """
    if access.access not in ACCESS_VALUES:
        raise ValueError(f'unknown access value {access.access!r}; expected one of {sorted(ACCESS_VALUES)}')
    has_publisher = bool(access.publisher)
    if (access.access == 'licensed') != has_publisher:
        raise ValueError(
            f'publisher must be set iff access == "licensed" (access={access.access!r}, publisher set={has_publisher})'
        )
