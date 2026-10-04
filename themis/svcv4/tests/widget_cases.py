"""An SVCv4 classification to build widget payloads from: a missense variant in FBN1 against Marfan syndrome."""

from __future__ import annotations

import decimal
from collections.abc import Mapping, Sequence

from themis.evidence.models import evidence_pb2
from themis.rpc import gene_disease_pb2
from themis.svcv4 import builders, classify, reference, scoring, splice_tree, widget
from themis.svcv4.models import svcv4_pb2
from themis.widgets.models import svcv4_classification_pb2

Record = svcv4_classification_pb2.Svcv4Classification
D = decimal.Decimal

# The cells each scored independent code of `missense` was priced from.
CELLS: Mapping[str, Sequence[tuple[str, int]]] = {
    'POP_FRQ': [('POP_FRQ.bin.lt_1_5x', 1)],
    'CLN_DNV': [('CLN_DNV.specific.unconfirmed', 1)],
    'LOC_PHE': [('LOC_PHE.yield.ge_82', 1)],
}


def missense(
    dnv: str = '2.0', *, splice: bool = True, gate: gene_disease_pb2.GateLevel = gene_disease_pb2.GATE_LEVEL_DEFINITIVE
) -> widget.Inputs:
    """The inputs: +1 on the amino-acid path, a blue splice path at 0, POP_FRQ 0, CLN_DNV `dnv`, LOC_PHE +4."""
    return widget.Inputs(
        consequence=evidence_pb2.CONSEQUENCE_MISSENSE,
        evidence=builders.MissensePaths(
            amino_acid=builders.AminoAcidEvidence(mis_prd=D('1'), exon=scoring.ExonRelevance.ALL),
            splice=builders.SpliceEvidence(colour=splice_tree.SpliceColour.BLUE, spl_prd=D('0')) if splice else None,
        ),
        independent_codes=[
            classify.IndependentCode('POP_FRQ', D('0.0')),
            classify.IndependentCode('CLN_DNV', D(dnv)),
            classify.IndependentCode('LOC_PHE', D('4.0')),
        ],
        gate_level=gate,
    )


def routing() -> svcv4_classification_pb2.Svcv4Classification.Routing:
    """The routing's annotation."""
    return Record.Routing(
        variant=Record.Variant(transcript_hgvs='NM_000138.5:c.7003C>T', gene_symbol='FBN1', hgnc_id='HGNC:3603'),
        entity=Record.Entity(
            disease='Marfan syndrome',
            mondo_id='MONDO:0007947',
            inheritance=evidence_pb2.INHERITANCE_AUTOSOMAL_DOMINANT,
            mechanism='loss of function',
            validity_source='ClinGen Gene Validity',
            validity_classification='Definitive',
        ),
        rationale='The presentation fits the Marfan syndrome entity.',
    )


def code_annotations(
    ref: reference.Reference, inputs: widget.Inputs, cells: Mapping[str, Sequence[tuple[str, int]]] = CELLS
) -> dict[str, svcv4_classification_pb2.Svcv4Classification.Code]:
    """An annotation for every code `build` needs one for: scored where the tally carries it, else no data."""
    result = inputs.classify(ref)
    carried = {line.label for line in result.contributions}
    for path in (result.selected_path, result.alternate_path):
        if path is not None:
            carried |= {line.label for line in path.contributions}
    notes = {}
    for code in widget.admitted(ref, inputs):
        if code in carried:
            notes[code] = Record.Code(
                status=svcv4_pb2.ASSESSMENT_STATUS_SCORED,
                rationale=f'why {code}',
                confidence=svcv4_pb2.CONFIDENCE_SETTLED,
                cells=[Record.Cell(cell_id=cell, count=count) for cell, count in cells.get(code, [])],
            )
        else:
            notes[code] = Record.Code(
                status=svcv4_pb2.ASSESSMENT_STATUS_NO_DATA, status_reason=f'nothing determines {code}'
            )
    return notes


def built(
    ref: reference.Reference,
    inputs: widget.Inputs,
    *,
    open_values: Sequence[widget.Alternative] = (),
    sensitivity: Sequence[widget.Alternative] = (),
) -> svcv4_classification_pb2.Svcv4Classification:
    """The payload for `inputs`, every code annotated."""
    return widget.build(
        ref,
        inputs,
        routing=routing(),
        codes=code_annotations(ref, inputs),
        open_values=open_values,
        sensitivity=sensitivity,
    )
