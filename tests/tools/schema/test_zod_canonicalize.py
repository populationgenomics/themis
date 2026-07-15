"""Tests for the ``typespec-zod`` canonicalizer (``tools.schema.zod_canonicalize``)."""

from __future__ import annotations

import pytest

from tools.schema import zod_canonicalize

_PROTO = """
syntax = "proto3";
enum Colour {
  red = 0;
  green = 1;
  blue = 2;
}
enum Converter {
  converter_unspecified = 0;
  litdown = 1;
  llm_ocr = 2;
}
message Holder {
  Colour colour = 1;
  google.protobuf.Timestamp when = 2;
}
"""


def test_int_enum_becomes_name_strings() -> None:
    out = zod_canonicalize.canonicalize('export const colour = z.enum([0, 1, 2]);', _PROTO)
    assert out == 'export const colour = z.enum(["red", "green", "blue"]);'


def test_unspecified_sentinel_dropped() -> None:
    out = zod_canonicalize.canonicalize('export const converter = z.enum([0, 1, 2]);', _PROTO)
    assert out == 'export const converter = z.enum(["litdown", "llm_ocr"]);'


def test_timestamp_extern_becomes_iso_datetime() -> None:
    out = zod_canonicalize.canonicalize('when: z.object({ _extern: z.never() }),', _PROTO)
    assert out == 'when: z.iso.datetime(),'


def test_enum_without_proto_counterpart_fails_loud() -> None:
    with pytest.raises(ValueError, match='no proto enum `Missing`'):
        zod_canonicalize.canonicalize('export const missing = z.enum([0]);', _PROTO)


def test_unmapped_well_known_fails_loud() -> None:
    proto = 'message M { google.protobuf.Duration d = 1; }'
    with pytest.raises(ValueError, match='Duration'):
        zod_canonicalize.canonicalize('d: z.object({ _extern: z.never() }),', proto)


def test_non_enum_non_extern_passthrough() -> None:
    source = 'export const inner = z.object({\n  value: z.string(),\n});'
    assert zod_canonicalize.canonicalize(source, _PROTO) == source
