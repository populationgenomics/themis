"""The SVCv4 deterministic scoring library.

EVALUATION SOFTWARE, AGAINST A DRAFT STANDARD. SVCv4 was released as a pilot in July 2026; its
point values, thresholds, code names and data model may change before publication (target:
*Genetics in Medicine*, ~Jan 2027), and Supplementary Material 17 (non-coding variants) is
unreleased, so that path is out of scope. This library exists to evaluate whether the framework can
be run autonomously. It is **not a validated implementation, not for clinical or diagnostic use,
and its output is not a clinical variant classification** — every total it produces must be
re-verified against the final published standard and the ClinGen Pilot Calculator
(https://calculator.clinicalgenome.org/v4/pilot/ui/classification) before it informs anything.
Framework questions: acmg_svc_pilot@clinicalgenome.org.

`themis.svcv4` is the compute tier of the evidence-interfaces design
(`docs/design/evidence-interfaces.md`): it turns retrieved evidence plus the model's judgement
inputs into a reproducible point tally and classification. It is deterministic arithmetic only —
no network, no model calls, no judgement. The model supplies the judgement inputs (mechanism level,
exon relevance, eligible informative variants, DAFT parameters, path choices); this library
combines them into a transparent, auditable point total.

Modules:
    reference: the typed shape of the framework — bands, gate, matrix, calibration, per-code
        ranges, the tables priced per observed individual — and the checks a transcription of it
        must satisfy.
    data: that transcription, one module per framework area; `data.load_reference` assembles and
        checks the one `Reference` every module below reads.
    scoring: the combining engine (matrix multiplier, INF-after-matrix, caps, missense/splice
        max-path, band mapping, gene-disease-validity gate cap).
    splice_tree: the splice decision tree — the SVI trichotomy a predictor score bins onto, and the
        per-flow, per-colour bounds and assay vocabulary the reference states only as a union.
    duplication_tree: the single-/multi-exon duplication/gain decision-tree cells (per-path family
        and bounds), which the reference likewise states only as a union.
    classify: the combining contract (judgement inputs + evidence in, audit trail out) and the shape
        every door's answer reaches it in.
    builders: the per-variant-type path structure, and `classify_variant` — the routed entry point
        that takes a variant's consequence and its evidence and returns the classification.
    provenance: the upstream releases an answer rests on, carried from a response onto the tally.
    payload: reading a documented path out of a response's untyped `raw` payload, failing loudly
        where the upstream's shape has moved under a contract that still names it.
    frequency: DAFT computation, POP_FRQ binning, POP_HMZ.
    predictor_policy: which calibrated missense predictor a gene's MIS_PRD score must come from —
        frozen, versioned data, resolved per gene rather than judged per variant (SM6) — and the two
        calls that selection governs: what to ask `Vep.Annotate` for, and what to bin off its answer.
    predictors: that predictor's score to SVCv4-bin mapping, and the key VEP serves the score under.
    grantham: the Grantham distance matrix, the protein-HGVS substitution it compares, and the four
        summable MIS_INF sub-rules.
    placement: placing a ClinVar pool against one coding span — which records the codon or exon
        holds, and which carry no span to place.
    nmd: NMD (50-nt rule) and NSD prediction — from a transcript's own exon table, or from the
        determination a predicted skip already carries over the aberrant transcript's.
    functional: an assay's result to FXN points by each of SM20's routes — a deposited OddsPath, the
        control-count grids, and the animal-model table.

`data` is this repository's transcription of the public SVCv4 pilot specification: the code names,
point values, thresholds, matrix multipliers and routing an implementation needs, and none of the
supplement texts. The values are typed literals, so a dropped or renamed field is a type error where
it is written; each cites the supplement line it is read from, and `tools/svcv4-oracle` holds every
cap in it against the ClinGen pilot calculator. `data/predictor_policy.json` beside those modules is
the frozen predictor choice SM6 requires be made in advance, and
`data/gencc-lof-mechanism-framework.md` is the GenCC framework's four confidence terms, which SM18
imports for the mechanism axis, with the evidence-point band that yields each.

The SM*n* §*m* citations, and the decision-tree citations the modules below make, resolve against
the document set `data.meta.CITED_DOCUMENTS` pins — supplement text extractions and one
transcription per workflow diagram, both line-addressed. Diagram and supplement text disagree in
known places; where one is hit the reference value is authoritative, except where it states only a
union over several decision-tree paths and so cannot bound any one of them — the splice and
duplication/gain cells, which `splice_tree` and `duplication_tree` take from the trees instead (see
the module docstrings for the conflicts each module resolves).
"""

from __future__ import annotations
