"""Stamping a retrieval with where it came from, shared by every evidence interface's backend.

Every response of every evidence interface carries `Provenance` and a `raw` Struct, built the same way
from the upstream adapters' result dataclasses — so the shape lives here rather than once per
interface.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping
from typing import Protocol

from google.protobuf import json_format, struct_pb2, timestamp_pb2

from themis.evidence.models import evidence_pb2


class Sourced(Protocol):
    """The provenance-carrying shape every upstream Result dataclass exposes (read-only fields)."""

    @property
    def source(self) -> str: ...
    @property
    def dataset_versions(self) -> tuple[str, ...]: ...
    @property
    def query(self) -> str: ...


def utcnow() -> datetime.datetime:
    """The as-of time a response's Provenance entries are stamped with.

    One reading per response, taken before its upstream calls, so every entry of a composed answer
    carries the same as-of time rather than the moment each leg happened to return.
    """
    return datetime.datetime.now(datetime.UTC)


def struct(raw: Mapping[str, object]) -> struct_pb2.Struct:
    """The upstream payload as the `raw` Struct every response carries."""
    value = struct_pb2.Struct()
    json_format.ParseDict(dict(raw), value)
    return value


def provenance(result: Sourced, at: datetime.datetime) -> evidence_pb2.Provenance:
    """One Provenance for one upstream that answered."""
    return evidence_pb2.Provenance(
        source=result.source, dataset_versions=result.dataset_versions, query=result.query, retrieved_at=_timestamp(at)
    )


def _timestamp(at: datetime.datetime) -> timestamp_pb2.Timestamp:
    stamp = timestamp_pb2.Timestamp()
    stamp.FromDatetime(at)
    return stamp
