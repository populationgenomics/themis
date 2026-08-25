"""What this transcription is, and the document set its citations resolve against."""

from __future__ import annotations

import dataclasses

from themis.svcv4 import reference


@dataclasses.dataclass(frozen=True)
class Framework:
    """The standard this package transcribes, and the terms the standard is stated in.

    Attributes:
        framework: The standard's short name.
        full_name: Its full title.
        status: Which phase of the standard's release the transcription follows.
        pilot_opened: When the pilot opened for feedback.
        feedback_deadline: When feedback closed.
        target_publication: When and where the standard is expected to be published.
        unit_of_classification: What a classification is *of* — the monogenic disease entity, not the
            gene and not the phenotype.
        usage: What output from an implementation of a draft standard may and may not be used for.
        variant_under_analysis: The standard's term for the variant being classified.
    """

    framework: str
    full_name: str
    status: str
    pilot_opened: str
    feedback_deadline: str
    target_publication: str
    unit_of_classification: str
    variant_under_analysis: str
    usage: str


FRAMEWORK = Framework(
    framework='SVCv4',
    full_name='ACMG/AMP/CAP/ClinGen Sequence Variant Classification version 4',
    status='draft-phase3-pilot',
    pilot_opened='2026-07-21',
    feedback_deadline='2026-08-19',
    target_publication='2027-01 (Genetics in Medicine)',
    unit_of_classification='MDE',
    variant_under_analysis='VBC',
    usage=(
        'Evaluation only. Not a validated implementation, not for clinical or diagnostic use; output is not a '
        'clinical variant classification. Re-verify every value against the final published standard and the '
        'ClinGen Pilot Calculator (https://calculator.clinicalgenome.org/v4/pilot/ui/classification).'
    ),
)

PROVENANCE = reference.TranscriptionProvenance(
    what=(
        "Themis's own transcription of the public SVCv4 pilot specification, as typed Python values: the code "
        'names, point values, thresholds, matrix multipliers and routing an implementation needs. It carries none '
        'of the supplement texts.'
    ),
    verified_against=(
        "the supplements, at the line each value's citation names, and the ClinGen pilot calculator's cap tables "
        'via tools/svcv4-oracle, where every departure from the calculator is pinned with the passage behind it.'
    ),
    citation_form=(
        "SM<n> §<m> is line <m> of that supplement's text extraction under "
        'svcv4-docs/code-specific-workflow-guidance/txt/ in the repository CITED_DOCUMENTS pins; a decision-tree '
        'citation names a transcription under svcv4-docs/workflow-images/ there.'
    ),
)

CITED_DOCUMENTS = reference.CitedDocuments(
    repository='populationgenomics/SVCv4-info',
    revision='4e7050dc79f12ea80e81ce03e65013f0271ba8e2',
    note=(
        'The revision every SM<n> §<m> and decision-tree citation across this transcription resolves against. The '
        'text extractions and workflow transcriptions are line-addressed, so a citation names one line only under '
        'a fixed tree.'
    ),
)
