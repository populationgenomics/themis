"""Test the S0.6 proto compat gate (``tools.schema.buf_compat``).

``buf breaking`` (FILE category) is the wire analogue of the chuckd trio — a
field renumber trips it red, an additive field is tolerated, an unchanged proto
passes. Together they prove the gate the proto pipeline relies on. All run the
real ``buf`` image, so they are gated on a reachable Docker daemon.
"""

from __future__ import annotations

import pytest

from tools.schema import buf_compat

_SAMPLE_PROTO = """\
syntax = "proto3";

package themis.rpc.sample;

message Foo {
  string a = 1;
  string b = 2;
}
"""

_SAMPLE_RELPATH = 'themis/rpc/sample.proto'


@pytest.mark.usefixtures('docker_daemon')
def test_buf_field_renumber_is_breaking() -> None:
    renumbered = _SAMPLE_PROTO.replace('string b = 2;', 'string b = 3;')
    _output, returncode = buf_compat._run_buf(renumbered, _SAMPLE_PROTO, _SAMPLE_RELPATH)
    assert returncode != 0


@pytest.mark.usefixtures('docker_daemon')
def test_buf_field_addition_is_tolerated() -> None:
    added = _SAMPLE_PROTO.replace('  string b = 2;\n', '  string b = 2;\n  string c = 3;\n')
    _output, returncode = buf_compat._run_buf(added, _SAMPLE_PROTO, _SAMPLE_RELPATH)
    assert returncode == 0


@pytest.mark.usefixtures('docker_daemon')
def test_buf_unchanged_proto_has_no_findings() -> None:
    _output, returncode = buf_compat._run_buf(_SAMPLE_PROTO, _SAMPLE_PROTO, _SAMPLE_RELPATH)
    assert returncode == 0
