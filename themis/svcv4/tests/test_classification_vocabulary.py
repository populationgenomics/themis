"""The `Classification` contract and the loaded reference name the same set of outcomes.

`themis.svcv4.models.Classification` is the proto encoding of what a classification comes out as;
`scoring.apply_gate` returns the same thing as a string, taken from `Reference.class_order` or from a
gate row's terminal `result`. The two are written down in different places and nothing at run time
joins them, so a class added to the framework — or a gate row given a new terminal result — would
otherwise land in the reference with no member to carry it across the wire, and be noticed only by
whoever next tried to store one.
"""

from __future__ import annotations

from themis.svcv4 import reference
from themis.svcv4.models import svcv4_pb2

# The join. Left of it is the contract, right of it the framework's own spelling; neither side can
# derive the other, so it is stated once here and the tests below hold both sides to covering it.
_LADDER: dict[svcv4_pb2.Classification, str] = {
    svcv4_pb2.CLASSIFICATION_BENIGN: 'B',
    svcv4_pb2.CLASSIFICATION_LIKELY_BENIGN: 'LB',
    svcv4_pb2.CLASSIFICATION_VUS: 'VUS',
    svcv4_pb2.CLASSIFICATION_LIKELY_PATHOGENIC: 'LP',
    svcv4_pb2.CLASSIFICATION_PATHOGENIC: 'P',
}

_TERMINAL: dict[svcv4_pb2.Classification, str] = {
    svcv4_pb2.CLASSIFICATION_VARIANT_IN_GENE_OF_UNCERTAIN_SIGNIFICANCE: 'Variant in Gene of Uncertain Significance',
    svcv4_pb2.CLASSIFICATION_DO_NOT_REPORT: 'Do not report',
}

# The two members no classification run produces: the proto3 sentinel, and the curator's own finding
# that no class could be established — which the framework has no code for because the framework
# always reaches one.
_OUTSIDE_THE_FRAMEWORK = frozenset({svcv4_pb2.CLASSIFICATION_UNSPECIFIED, svcv4_pb2.CLASSIFICATION_NOT_ESTABLISHED})


def test_every_member_is_accounted_for() -> None:
    """An added member has to be placed: a ladder rung, a terminal gate result, or neither by name."""
    assert set(svcv4_pb2.Classification.values()) == set(_LADDER) | set(_TERMINAL) | _OUTSIDE_THE_FRAMEWORK


def test_the_ladder_members_are_the_reference_classes(ref: reference.Reference) -> None:
    assert sorted(_LADDER.values()) == sorted(ref.class_order)


def test_the_terminal_members_are_the_gate_results(ref: reference.Reference) -> None:
    results = {row.result for row in ref.gate.values() if row.result is not None}
    assert set(_TERMINAL.values()) == results


def test_the_join_is_injective() -> None:
    """Two members mapping onto one framework spelling would make a decoded value ambiguous."""
    spellings = [*_LADDER.values(), *_TERMINAL.values()]
    assert len(set(spellings)) == len(spellings)
