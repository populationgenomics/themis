"""The reference a variant-classification run is read against: each submitting curator's latest complete submission.

A variant enters a round once at least one curator assigned to it has submitted a complete worksheet,
one that states a consequence class and a class. Its reference is the registration, which states the
question as the entity and the clinical context, and the latest submission of each curator who has
submitted, kept as the stored rows, with how many times and when each submitted; an assigned curator
who has not submitted is recorded as outstanding. The rows are read from the curation schema in a
read-only transaction and decoded here. A submission that leaves the consequence class or the class
unset is left out of the reference with the reason named, as an outstanding curator is; a reference
whose stored rows cannot be used fails its variant with the reason named
(docs/design/run-review-loop.md §"A variant enters a round with its first complete submission").
"""

from __future__ import annotations

import base64
import collections
import contextlib
import dataclasses
import datetime
import string
from collections.abc import Mapping, Sequence

from google.protobuf import message

from themis.common import sql
from themis.curation import rows
from themis.curation.models import curation_pb2
from themis.svcv4.models import svcv4_pb2
from tools.run_review import protos, rounds

_VARIANTS = (
    'SELECT id, gene, transcript, hgvs_c, clingen_allele_id, disease_label, mondo_id, mode_of_inheritance,'
    ' clinical_context FROM curation.variants ORDER BY id'
)
# `curation.variants.mode_of_inheritance` stores a `ModeOfInheritance` member's name without this prefix.
_MODE_PREFIX = 'MODE_OF_INHERITANCE_'

_WORKSHEETS = 'SELECT id, variant_id, curator_email, workflows_version FROM curation.worksheets ORDER BY id'
_SUBMISSIONS = (
    'SELECT id, worksheet_id, submitted_at, note FROM curation.submissions ORDER BY worksheet_id, submitted_at, id'
)
# The rows of each worksheet's latest submission.
_LATEST_ROWS = """
SELECT a.submission_id, a.workflow_id, a.assessment
FROM curation.assessments a
JOIN (
    SELECT DISTINCT ON (worksheet_id) id FROM curation.submissions ORDER BY worksheet_id, submitted_at DESC, id DESC
) latest ON latest.id = a.submission_id
ORDER BY a.submission_id, a.workflow_id
"""


class UnusableReferenceError(Exception):
    """A variant's reference cannot be read a run against; the message names the variant and the reason."""


@dataclasses.dataclass(frozen=True)
class Registration:
    """The variant as the manager registered it: the question every curator and the run answer.

    The question is the variant against one entity, a disease and a mode of inheritance, with the clinical
    context the manager wrote. `mode_of_inheritance` is the column as stored; `mode` is the member it names.

    Raises:
        ValueError: If `clinical_context` is absent or blank; the column's NOT NULL and CHECK forbid both, so
            the schema is one this code was not written against.
    """

    variant_id: str
    gene: str
    transcript: str
    hgvs_c: str
    clingen_allele_id: str
    disease_label: str
    mondo_id: str
    mode_of_inheritance: str
    clinical_context: str

    def __post_init__(self) -> None:
        # A row read from a schema before the column's NOT NULL carries None here.
        if not isinstance(self.clinical_context, str) or not self.clinical_context.strip():
            raise ValueError(f'{self.variant_id}: the registration states no clinical context')

    @property
    def mode(self) -> svcv4_pb2.ModeOfInheritance.ValueType:
        """The registered mode of inheritance.

        Raises:
            ValueError: If the column names no member, or names the unspecified one; its CHECK forbids both, so
                the schema is one this code was not written against.
        """
        try:
            mode = svcv4_pb2.ModeOfInheritance.Value(f'{_MODE_PREFIX}{self.mode_of_inheritance}')
        except ValueError as e:
            raise ValueError(f'{self.variant_id}: unknown mode of inheritance {self.mode_of_inheritance!r}') from e
        if mode == svcv4_pb2.MODE_OF_INHERITANCE_UNSPECIFIED:
            raise ValueError(f'{self.variant_id}: the registration states no mode of inheritance')
        return mode


@dataclasses.dataclass(frozen=True)
class Submitted:
    """One act of submitting: when, and the note the curator left with it (empty where they left none)."""

    at: datetime.datetime
    note: str


@dataclasses.dataclass(frozen=True)
class Submission:
    """One curator's latest submission on a variant, as stored, with every submission that curator made.

    `label` is how the round names the curator: a letter, given in the order of the assigned curators'
    emails, which the round does not keep, so nothing a reader is handed or writes down carries a curator's
    identity. A label is stable within a round only: an assignment made or removed between rounds shifts it.
    """

    label: str
    workflows_version: str
    submission_id: str
    submitted_at: datetime.datetime
    history: tuple[Submitted, ...]
    rows: Mapping[str, bytes]


@dataclasses.dataclass(frozen=True)
class StoredReference:
    """A variant, the latest submission of each curator who submitted, and the labels of those who have not.

    `submissions` is never empty and is in curator-label order; `outstanding` names the assigned curators
    absent from it.
    """

    registration: Registration
    submissions: tuple[Submission, ...]
    outstanding: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Reading:
    """What the curation schema holds: the variants with a submission, and how many registered variants have none.

    `unusable` holds the variants with a submission whose reference cannot even be stored, each id with the
    reason.
    """

    submitted: tuple[StoredReference, ...]
    unusable: tuple[tuple[str, str], ...]
    unsubmitted: int


def read(conn: sql.Connection) -> Reading:
    """Read every submitted variant's reference from one snapshot, in a read-only transaction rolled back at the end."""
    with contextlib.closing(conn.cursor()) as cursor:
        try:
            # One snapshot for every statement, so a submission landing between two of them cannot be half seen.
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            cursor.execute(_VARIANTS)
            variants = list(cursor.fetchall())
            cursor.execute(_WORKSHEETS)
            worksheets = list(cursor.fetchall())
            cursor.execute(_SUBMISSIONS)
            submissions = list(cursor.fetchall())
            cursor.execute(_LATEST_ROWS)
            latest_rows = list(cursor.fetchall())
        finally:
            conn.rollback()
    return _assemble(variants, worksheets, submissions, latest_rows)


def _assemble(
    variants: Sequence[sql.Row],
    worksheets: Sequence[sql.Row],
    submissions: Sequence[sql.Row],
    latest: Sequence[sql.Row],
) -> Reading:
    worksheets_of: dict[str, list[sql.Row]] = collections.defaultdict(list)
    for worksheet in worksheets:
        worksheets_of[worksheet[1]].append(worksheet)
    submitted: dict[str, list[sql.Row]] = collections.defaultdict(list)
    for submission in submissions:
        submitted[submission[1]].append(submission)
    rows_of: dict[str, dict[str, bytes]] = collections.defaultdict(dict)
    for submission_id, workflow_id, payload in latest:
        rows_of[submission_id][workflow_id] = bytes(payload)
    references: list[StoredReference] = []
    unusable: list[tuple[str, str]] = []
    unsubmitted = 0
    for variant in variants:
        assigned = sorted(worksheets_of[variant[0]], key=lambda worksheet: worksheet[2])
        if not any(submitted[worksheet[0]] for worksheet in assigned):
            unsubmitted += 1
            continue
        if len(assigned) > len(string.ascii_uppercase):
            unusable.append((variant[0], f'{variant[0]}: {len(assigned)} curators, more than the round has labels for'))
            continue
        labelled = list(zip(string.ascii_uppercase, assigned, strict=False))
        references.append(
            StoredReference(
                registration=Registration(*variant),
                submissions=tuple(
                    _submission(label, worksheet, submitted[worksheet[0]], rows_of)
                    for label, worksheet in labelled
                    if submitted[worksheet[0]]
                ),
                outstanding=tuple(label for label, worksheet in labelled if not submitted[worksheet[0]]),
            )
        )
    return Reading(submitted=tuple(references), unusable=tuple(unusable), unsubmitted=unsubmitted)


def _submission(
    label: str, worksheet: sql.Row, submissions: Sequence[sql.Row], rows_of: Mapping[str, Mapping[str, bytes]]
) -> Submission:
    """The worksheet's latest submission; `submissions` is in submission order, so the latest is last."""
    latest = submissions[-1]
    return Submission(
        label=label,
        workflows_version=worksheet[3],
        submission_id=latest[0],
        submitted_at=latest[2],
        history=tuple(Submitted(at=submission[2], note=submission[3]) for submission in submissions),
        # A submission with no rows reads as empty here, and `decode` fails its variant naming it.
        rows=dict(rows_of.get(latest[0], {})),
    )


@dataclasses.dataclass(frozen=True)
class Decoded:
    """One curator's latest submission with every row decoded."""

    submission: Submission
    assessments: Mapping[str, curation_pb2.Assessment]

    @property
    def routing(self) -> curation_pb2.RoutingAssessment:
        return self.assessments[rows.ROUTING].routing

    @property
    def verdict(self) -> curation_pb2.VerdictAssessment:
        return self.assessments[rows.VERDICT].verdict


@dataclasses.dataclass(frozen=True)
class Incomplete:
    """A curator whose latest submission leaves unset what a run is compared on, and what it leaves unset."""

    label: str
    reason: str


@dataclasses.dataclass(frozen=True)
class DecodedReference:
    """Every submitting curator's latest submission, decoded, split by whether it states what a run is compared on.

    `complete` is the reference a run is read against, in curator-label order, and may be empty: every
    submitting curator then left something unset. `incomplete` names each curator left out, and why.
    """

    complete: tuple[Decoded, ...]
    incomplete: tuple[Incomplete, ...]


def decode(reference: StoredReference) -> DecodedReference:
    """Decode every submitting curator's rows and set aside each submission that leaves the routing or the class unset.

    Raises:
        UnusableReferenceError: If a row is stored under a retired section's id, does not decode, holds a
            section other than its id stores or a field or enum number its schema does not declare, a
            curator's latest submission holds no rows, or two of one curator's submissions tie for the latest.
    """
    decoded = tuple(_decode_submission(reference.registration.variant_id, s) for s in reference.submissions)
    complete: list[Decoded] = []
    incomplete: list[Incomplete] = []
    for item in decoded:
        where = f'{reference.registration.variant_id}: curator {item.submission.label}'
        if not item.assessments:
            raise UnusableReferenceError(
                f'{where}: the latest submission, {item.submission.submission_id}, holds no rows'
            )
        history = item.submission.history
        if len(history) > 1 and history[-1].at == history[-2].at:
            raise UnusableReferenceError(
                f'{where} submitted twice at {history[-1].at.isoformat()}, so neither submission is the latest'
            )
        if unset := _unset(item):
            incomplete.append(Incomplete(label=item.submission.label, reason=f'submitted without {unset}'))
        else:
            complete.append(item)
    return DecodedReference(complete=tuple(complete), incomplete=tuple(incomplete))


def _unset(item: Decoded) -> str:
    """What of the consequence class and the class `item` leaves unset, as a phrase; empty where it states both.

    The worksheet saves a section's row only once the curator writes in it, so a missing row is unset too.
    """
    unset = []
    if rows.ROUTING not in item.assessments or not item.routing.consequence_class:
        unset.append('a consequence class')
    if rows.VERDICT not in item.assessments or not item.verdict.classification:
        unset.append('a class')
    return ' or '.join(unset)


def _decode_submission(variant_id: str, submission: Submission) -> Decoded:
    assessments: dict[str, curation_pb2.Assessment] = {}
    for workflow_id, payload in submission.rows.items():
        where = f'{variant_id}: curator {submission.label}, row {workflow_id!r}'
        try:
            expected = rows.kind_of(workflow_id)
        except ValueError as e:
            raise UnusableReferenceError(f'{where}: {e}') from e
        assessment = curation_pb2.Assessment()
        try:
            assessment.ParseFromString(payload)
        except message.DecodeError as e:
            raise UnusableReferenceError(f'{where} does not decode as an Assessment: {e}') from e
        found = assessment.WhichOneof('kind')
        if found != expected:
            raise UnusableReferenceError(f'{where} holds a {found!r} section where its id stores {expected!r}')
        if undeclared := protos.undeclared(assessment):
            raise UnusableReferenceError(
                f'{where} holds fields or enum numbers its schema does not declare ({"; ".join(undeclared)}): a '
                'newer writer set them, or they were retired after the row was written. A routing row still carrying '
                'the retired entity fields is rewritten by `themis.curation.routing_backfill`.'
            )
        assessments[workflow_id] = assessment
    return Decoded(submission=submission, assessments=assessments)


def last_submitted_at(complete: Sequence[Decoded]) -> datetime.datetime:
    """When the last of `complete` was submitted: the date the round would pin a run's evidence to.

    Raises:
        ValueError: If `complete` is empty, so there is no submission to date.
    """
    if not complete:
        raise ValueError('no complete submission, so there is no date to pin')
    return max(item.submission.submitted_at for item in complete)


def to_json(reference: StoredReference) -> dict[str, object]:
    """The reference as stored, rows base64-encoded, so rendering decodes exactly what the round read."""
    return {
        'registration': dataclasses.asdict(reference.registration),
        'submissions': [
            {
                'label': s.label,
                'workflows_version': s.workflows_version,
                'submission_id': s.submission_id,
                'submitted_at': s.submitted_at.isoformat(),
                'history': [{'at': h.at.isoformat(), 'note': h.note} for h in s.history],
                'rows': {wid: base64.b64encode(payload).decode('ascii') for wid, payload in sorted(s.rows.items())},
            }
            for s in reference.submissions
        ],
        'outstanding': list(reference.outstanding),
    }


def from_json(document: rounds.Json, where: str) -> StoredReference:
    """Read back what `to_json` wrote.

    Raises:
        rounds.RoundError: If a field is missing, of another type, or not one `to_json` writes, no
            submission is stored, or a curator is both submitted and outstanding.
        ValueError: If a date or a row's encoding does not parse, or the clinical context is blank.
    """
    fields = rounds.Fields(document, where)
    registration = rounds.Fields(fields.mapping('registration'), f'{where}: `registration`')
    stored = StoredReference(
        registration=Registration(
            **{field.name: registration.text(field.name) for field in dataclasses.fields(Registration)}
        ),
        submissions=tuple(
            _submission_from_json(submission, f'{where}: `submissions`')
            for submission in fields.mappings('submissions')
        ),
        outstanding=tuple(fields.texts('outstanding')),
    )
    registration.finish()
    fields.finish()
    if not stored.submissions:
        raise rounds.RoundError(f'{where}: `submissions` is empty; a variant enters a round with a submission')
    both = {item.label for item in stored.submissions} & set(stored.outstanding)
    if both:
        raise rounds.RoundError(f'{where}: curator(s) {sorted(both)} are both submitted and outstanding')
    return stored


def _submission_from_json(document: rounds.Json, where: str) -> Submission:
    fields = rounds.Fields(document, where)
    stored_rows = fields.mapping('rows')
    for workflow_id, payload in stored_rows.items():
        if not isinstance(payload, str):
            raise rounds.RoundError(f'{where}: row {workflow_id!r} is not base64 text')
    history = [rounds.Fields(item, f'{where}: `history`') for item in fields.mappings('history')]
    submission = Submission(
        label=fields.text('label'),
        workflows_version=fields.text('workflows_version'),
        submission_id=fields.text('submission_id'),
        submitted_at=datetime.datetime.fromisoformat(fields.text('submitted_at')),
        history=tuple(
            Submitted(at=datetime.datetime.fromisoformat(item.text('at')), note=item.text('note')) for item in history
        ),
        rows={wid: base64.b64decode(str(payload), validate=True) for wid, payload in stored_rows.items()},
    )
    for item in history:
        item.finish()
    fields.finish()
    return submission
