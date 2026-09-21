"""Admission stays where rpc-authorization.md puts it, asserted over the evidence image's source."""

from __future__ import annotations

from themis.services import evidence
from themis.services.evidence import __main__ as entrypoint
from themis.services.evidence.literature import servicer as literature_servicer
from themis.testing import gate


def test_the_evidence_server_is_built_only_through_the_gated_factory() -> None:
    gate.assert_built_only_through_the_gated_factory(entrypoint)


def test_no_evidence_module_constructs_a_server() -> None:
    gate.assert_no_module_constructs_a_server(evidence)


def test_the_literature_servicer_reads_no_call_metadata() -> None:
    gate.assert_servicer_reads_no_call_metadata(literature_servicer)
