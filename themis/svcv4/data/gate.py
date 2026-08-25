"""The gene-disease-validity gate: which classes each validity level lets a total reach.

A level is named by the contract enum a curated entity carries (`DescribeGene`), not by the
standard's display spelling, so a level the gate does not carry is a gene nothing can be scored
against — which is what `assemble_gate` and `gene_disease_validity.check_gate_levels` hold the rows
to from the two sides.
"""

from __future__ import annotations

from themis.rpc import gene_disease_pb2
from themis.svcv4 import reference
from themis.svcv4.data import classification

_EVERY_CLASS = frozenset(classification.CLASS_ORDER)

ROWS = (
    reference.GateRow(level=gene_disease_pb2.GATE_LEVEL_DEFINITIVE, allows=_EVERY_CLASS, result=None),
    reference.GateRow(level=gene_disease_pb2.GATE_LEVEL_STRONG, allows=_EVERY_CLASS, result=None),
    reference.GateRow(
        level=gene_disease_pb2.GATE_LEVEL_MODERATE, allows=frozenset({'LP', 'VUS', 'LB', 'B'}), result=None
    ),
    reference.GateRow(level=gene_disease_pb2.GATE_LEVEL_LIMITED, allows=frozenset({'VUS', 'LB', 'B'}), result=None),
    reference.GateRow(
        level=gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED,
        allows=frozenset(),
        result='Variant in Gene of Uncertain Significance',
    ),
    reference.GateRow(
        level=gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED, allows=frozenset(), result='Do not report'
    ),
)

NOTE = (
    'Also required (SM18) for molecular-mechanism scoring to be > "Uncertain": gene-disease validity must be '
    'Moderate or higher.\n'
)
