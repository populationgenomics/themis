"""Worksheet rows and references for the scenario tests, built through the generated stubs."""

from __future__ import annotations

import datetime
from collections.abc import Mapping

from themis.curation import rows
from themis.curation.models import curation_pb2
from themis.evidence.models import evidence_pb2
from themis.svcv4.models import svcv4_pb2
from tools.run_review.variant_classification import reference

REGISTRATION = reference.Registration(
    variant_id='v1',
    gene='FBN1',
    transcript='NM_000138.5',
    hgvs_c='c.7003C>T',
    clingen_allele_id='',
    disease_label='Marfan syndrome',
    mondo_id='MONDO:0007947',
    mode_of_inheritance='AUTOSOMAL_DOMINANT',
    clinical_context='Tall stature and ectopia lentis.\n\nAortic root dilatation at 14.',
)


def at(day: int) -> datetime.datetime:
    return datetime.datetime(2026, 8, day, 9, 0, tzinfo=datetime.UTC)


def routing(consequence: evidence_pb2.Consequence = evidence_pb2.CONSEQUENCE_MISSENSE) -> bytes:
    return curation_pb2.Assessment(
        routing=curation_pb2.RoutingAssessment(consequence_class=consequence, rationale='a missense change')
    ).SerializeToString()


def verdict(classification: svcv4_pb2.Classification = svcv4_pb2.CLASSIFICATION_LIKELY_PATHOGENIC) -> bytes:
    return curation_pb2.Assessment(
        verdict=curation_pb2.VerdictAssessment(classification=classification)
    ).SerializeToString()


def workflow(rationale: str = 'two unrelated probands') -> bytes:
    return curation_pb2.Assessment(
        workflow=curation_pb2.WorkflowAssessment(
            status=svcv4_pb2.ASSESSMENT_STATUS_SCORED,
            rationale=rationale,
            confidence=svcv4_pb2.CONFIDENCE_LEANING,
            fields=[curation_pb2.FieldValue(field_id='f', cell_id='CLN_AFF.specific', label='Specific', value='2')],
        )
    ).SerializeToString()


def full_rows() -> dict[str, bytes]:
    return {rows.ROUTING: routing(), rows.VERDICT: verdict(), 'cln_aff': workflow()}


def submission(
    label: str, day: int, submission_rows: Mapping[str, bytes], *, earlier: tuple[int, ...] = ()
) -> reference.Submission:
    """A curator's latest submission on `day`, after one on each `earlier` day, each with a note naming its day."""
    return reference.Submission(
        label=label,
        workflows_version='1',
        submission_id=f'{label}-{day}',
        submitted_at=at(day),
        history=tuple(reference.Submitted(at=at(d), note=f'submitted on day {d}') for d in (*earlier, day)),
        rows=dict(submission_rows),
    )


def stored(*submissions: reference.Submission, outstanding: tuple[str, ...] = ()) -> reference.StoredReference:
    return reference.StoredReference(registration=REGISTRATION, submissions=submissions, outstanding=outstanding)
