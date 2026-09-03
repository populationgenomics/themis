"""The SVCv4 reference: one module per framework area, assembled into the one `Reference`.

This package is Themis's own transcription of the public SVCv4 pilot specification — the code names,
point values, thresholds, matrix multipliers and routing an implementation needs, and none of the
supplement texts. Every value cites the supplement line it is read from, and `meta.CITED_DOCUMENTS`
pins the revision those citations resolve against.

The values are typed literals, so the shape of the transcription is checked where it is written: a
dropped field, a renamed one, or a number where a string belongs is a type error, not a parser's
runtime complaint. What the types cannot check are the agreements between structures, and those run
here and in the modules that state the values — at import, so a transcription that fails one is
never handed to a caller. `reference` holds both the types and the checks.

A value and the words the framework states it in travel together on `Reference`: a code carries its
notes, a bin its printed ratio, a table its caption and the image it was read from. What stays behind
here is the descriptive aggregates — the prose rules, the crosswalk from v3's criteria, the
provenance of a whole set of tables — which a reader looks up next to the values they describe.

Two data files sit beside these modules rather than in them: `predictor_policy.json`, which is
versioned per gene and read by path, and `gencc-lof-mechanism-framework.md`, the GenCC confidence
terms SM18 imports for the mechanism axis.
"""

from __future__ import annotations

import types

from themis.svcv4 import reference
from themis.svcv4.data import (
    assays,
    calibration,
    classification,
    clinical,
    codes,
    gate,
    locus,
    matrix,
    meta,
    policies,
    population,
)


def _assemble() -> reference.Reference:
    """Assemble the reference, running the checks that span two of its structures.

    One `Reference` is shared by every caller, so each keyed table it carries is a read-only copy of
    the module's own: a view over the live dict would still be writable through the module, and a
    writable table is one caller's edit reaching the tally of every other. A table nested inside one
    of those — a grid's cells — is annotated `Mapping` and left as it is, so what stops a write
    there is the annotation and the type checker, not the object.
    """
    reference.validate_pop_frq_precondition(clinical.PRECONDITION, codes.CODES)
    return reference.Reference(
        cited_documents=meta.CITED_DOCUMENTS,
        provenance=meta.PROVENANCE,
        class_order=classification.CLASS_ORDER,
        bands=classification.BANDS,
        vus_subbands=classification.VUS_SUBBANDS,
        gate=types.MappingProxyType(dict(reference.assemble_gate(gate.ROWS, classification.CLASS_ORDER))),
        mechanism_factors=types.MappingProxyType(dict(matrix.MOLECULAR_MECHANISM)),
        exon_factors=types.MappingProxyType(dict(matrix.EXON_RELEVANCE)),
        matrix_omitted_cell=matrix.OMITTED_CELL,
        codes=types.MappingProxyType(dict(codes.CODES)),
        frequency_bins=population.FREQUENCY_BINS,
        binning_grids=types.MappingProxyType(dict(population.GRIDS)),
        clinical_pop_frq_precondition=clinical.PRECONDITION,
        critical_residue_max=policies.CRITICAL_AMINO_ACIDS.max_points,
        oddspath=calibration.SCALE,
        control_counts=types.MappingProxyType(dict(assays.GRIDS)),
        concept_caps=types.MappingProxyType(dict(codes.CONCEPT_CAPS)),
        category_caps=types.MappingProxyType(dict(codes.CATEGORY_CAPS)),
        concept_to_codes=types.MappingProxyType(dict(codes.CONCEPT_TO_CODES)),
        independent_families=codes.INDEPENDENT_FAMILIES,
        per_observation=reference.PerObservationTables(
            homozygous=population.POP_HMZ.weights,
            unaffected=clinical.CLN_UAF,
            alternate_cause=clinical.CLN_ALT,
            affected_monoallelic=clinical.CLN_AFF.monoallelic,
            affected_biallelic=clinical.CLN_AFF.biallelic,
            de_novo=clinical.CLN_DNV.table3,
            case_control=clinical.CLN_CCS.per_determination.rows,
            diagnostic_yield=locus.LOC_PHE.diagnostic_yield_bins,
            cosegregation=locus.LOC_SEG.per_cosegregation.rows,
            non_segregation=locus.LOC_SEG.non_segregation.per_observation.rows,
        ),
    )


_REFERENCE = _assemble()


def load_reference() -> reference.Reference:
    """The validated SVCv4 reference.

    Returns:
        The one `Reference` these modules assemble. It is built and checked while this package is
        imported, so the call itself reads nothing and cannot fail.
    """
    return _REFERENCE
