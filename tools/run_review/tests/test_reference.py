"""The reference read from the curation schema the migrations build, and the checks it has to pass."""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
from collections.abc import Iterator, Mapping

import pg8000.dbapi
import pytest
import testcontainers.postgres

from themis.curation import rows
from themis.curation.models import curation_pb2
from themis.evidence.models import evidence_pb2
from themis.migrate import cloudsql, migrate
from themis.svcv4.models import svcv4_pb2
from tools.run_review import rounds
from tools.run_review.tests import curation_rows
from tools.run_review.variant_classification import reference

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[3] / 'themis' / 'migrate' / 'migrations'
_LOGINS: Mapping[str, str] = dict.fromkeys(
    ('AUTH_DB_USER', 'WEB_DB_USER', 'INGEST_DB_USER', 'EVIDENCE_DB_USER'), 'seed@example.iam'
)


@pytest.fixture
def curation_db(docker_daemon: None) -> Iterator[pg8000.dbapi.Connection]:
    del docker_daemon  # gate on a reachable Docker daemon (shared fixture)
    with testcontainers.postgres.PostgresContainer('postgres:16-alpine') as postgres:
        conn = pg8000.dbapi.connect(
            user=postgres.username,
            password=postgres.password,
            host=postgres.get_container_host_ip(),
            port=int(postgres.get_exposed_port(5432)),
            database=postgres.dbname,
        )
        try:
            with contextlib.closing(conn.cursor()) as cursor:
                cursor.execute('CREATE ROLE "seed@example.iam"')
            conn.commit()
            migrate.run(migrate.discover(_MIGRATIONS_DIR), cloudsql.CloudSqlLedger(conn), substitutions=_LOGINS)
            yield conn
        finally:
            conn.close()


def _register(cursor: pg8000.dbapi.Cursor, variant_id: str, mode: str = 'AUTOSOMAL_DOMINANT') -> None:
    # The registration key is unique per variant and entity, so each registration takes its own transcript.
    cursor.execute(
        'INSERT INTO curation.variants'
        ' (id, gene, transcript, hgvs_c, disease_label, mondo_id, mode_of_inheritance, clinical_context, created_by)'
        " VALUES (%s, 'FBN1', %s, 'c.7003C>T', 'Marfan syndrome', 'MONDO:0007947', %s, %s, 'm@example.org')",
        (variant_id, f'NM_{variant_id}', mode, f'The proband registered as {variant_id}.\n'),
    )


def _assign(cursor: pg8000.dbapi.Cursor, worksheet_id: str, variant_id: str, curator: str) -> None:
    cursor.execute(
        'INSERT INTO curation.worksheets (id, variant_id, curator_email, workflows_version, assigned_by)'
        " VALUES (%s, %s, %s, '1', 'm@example.org')",
        (worksheet_id, variant_id, curator),
    )


def _submit(cursor: pg8000.dbapi.Cursor, submission_id: str, worksheet_id: str, day: int, rationale: str) -> None:
    cursor.execute(
        'INSERT INTO curation.submissions (id, worksheet_id, submitted_at, note) VALUES (%s, %s, %s, %s)',
        (submission_id, worksheet_id, curation_rows.at(day), f'note for {rationale}'),
    )
    for workflow_id, payload in {**curation_rows.full_rows(), 'cln_aff': curation_rows.workflow(rationale)}.items():
        cursor.execute(
            'INSERT INTO curation.assessments (submission_id, workflow_id, assessment) VALUES (%s, %s, %s)',
            (submission_id, workflow_id, payload),
        )


def test_a_variant_is_read_with_each_submitting_curators_latest_submission(
    curation_db: pg8000.dbapi.Connection,
) -> None:
    with contextlib.closing(curation_db.cursor()) as cursor:
        _register(cursor, 'two-submitted', mode='X_LINKED_RECESSIVE')
        _assign(cursor, 'w-b', 'two-submitted', 'b@example.org')
        _assign(cursor, 'w-a', 'two-submitted', 'a@example.org')
        _submit(cursor, 's-a1', 'w-a', 18, 'first answer')
        _submit(cursor, 's-a2', 'w-a', 20, 'after discussion')
        _submit(cursor, 's-b1', 'w-b', 22, 'blind answer')
    curation_db.commit()

    [stored] = reference.read(curation_db).submitted

    assert stored.registration.variant_id == 'two-submitted'
    assert stored.registration.mode == svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_RECESSIVE
    assert stored.registration.clinical_context == 'The proband registered as two-submitted.\n'
    assert stored.outstanding == ()
    a, b = stored.submissions
    assert (a.label, a.submission_id, b.label, b.submission_id) == ('A', 's-a2', 'B', 's-b1')
    assert a.history == (
        reference.Submitted(at=curation_rows.at(18), note='note for first answer'),
        reference.Submitted(at=curation_rows.at(20), note='note for after discussion'),
    )
    decoded = reference.decode(stored).complete
    assert decoded[0].assessments['cln_aff'].workflow.rationale == 'after discussion'
    assert set(decoded[1].assessments) == {rows.ROUTING, rows.VERDICT, 'cln_aff'}


def test_a_variant_enters_with_one_submission_and_names_the_curators_outstanding(
    curation_db: pg8000.dbapi.Connection,
) -> None:
    with contextlib.closing(curation_db.cursor()) as cursor:
        _register(cursor, 'part-submitted')
        _assign(cursor, 'w-a', 'part-submitted', 'a@example.org')
        _assign(cursor, 'w-b', 'part-submitted', 'b@example.org')
        _assign(cursor, 'w-c', 'part-submitted', 'c@example.org')
        _submit(cursor, 's-b1', 'w-b', 21, 'alone')
        _register(cursor, 'assigned-unsubmitted')
        _assign(cursor, 'w-d', 'assigned-unsubmitted', 'a@example.org')
        _register(cursor, 'unassigned')
    curation_db.commit()

    reading = reference.read(curation_db)

    assert reading.unsubmitted == 2
    [stored] = reading.submitted
    assert stored.registration.variant_id == 'part-submitted'
    assert [item.label for item in stored.submissions] == ['B']
    assert stored.outstanding == ('A', 'C')


def test_a_variant_with_more_curators_than_labels_is_unusable_and_the_read_goes_on(
    curation_db: pg8000.dbapi.Connection,
) -> None:
    with contextlib.closing(curation_db.cursor()) as cursor:
        _register(cursor, 'crowded')
        for n in range(27):
            _assign(cursor, f'w-{n}', 'crowded', f'c{n:02}@example.org')
            _submit(cursor, f's-{n}', f'w-{n}', 20, 'answer')
        _register(cursor, 'one-curator')
        _assign(cursor, 'w-a', 'one-curator', 'a@example.org')
        _submit(cursor, 's-a', 'w-a', 20, 'answer')
    curation_db.commit()

    reading = reference.read(curation_db)

    assert [stored.registration.variant_id for stored in reading.submitted] == ['one-curator']
    assert reading.unusable == (('crowded', 'crowded: 27 curators, more than the round has labels for'),)


def test_the_read_leaves_no_transaction_open(curation_db: pg8000.dbapi.Connection) -> None:
    reference.read(curation_db)
    with contextlib.closing(curation_db.cursor()) as cursor:
        # A read-only transaction still open would refuse this write.
        _register(cursor, 'after')
    curation_db.commit()


@pytest.mark.parametrize(
    ('replaced', 'reason'),
    [
        ({'cln_aff': b'\xff\xff'}, 'does not decode'),
        ({'cln_aff': curation_rows.verdict()}, "holds a 'verdict' section where its id stores 'workflow'"),
    ],
    ids=['undecodable', 'wrong section'],
)
def test_an_unusable_reference_fails_its_variant_with_the_reason(replaced: dict[str, bytes], reason: str) -> None:
    broken = curation_rows.submission('B', 22, {**curation_rows.full_rows(), **replaced})
    stored = curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows()), broken)
    with pytest.raises(reference.UnusableReferenceError, match=f'v1: curator B.*{reason}'):
        reference.decode(stored)


def test_a_row_carrying_a_field_its_schema_does_not_declare_is_unusable() -> None:
    # Field 1 of RoutingAssessment, a varint: what a routing row written before the field was retired holds.
    retired = curation_pb2.Assessment(
        routing=curation_pb2.RoutingAssessment(consequence_class=evidence_pb2.CONSEQUENCE_MISSENSE)
    )
    retired.routing.MergeFromString(bytes([0x08, 0x01]))
    submission_rows = {**curation_rows.full_rows(), rows.ROUTING: retired.SerializeToString()}
    with pytest.raises(reference.UnusableReferenceError, match=r"row 'routing' holds fields.*RoutingAssessment.*\[1\]"):
        reference.decode(curation_rows.stored(curation_rows.submission('A', 20, submission_rows)))


def test_a_latest_submission_holding_no_rows_is_unusable() -> None:
    with pytest.raises(
        reference.UnusableReferenceError, match='v1: curator A: the latest submission, A-20, holds no rows'
    ):
        reference.decode(curation_rows.stored(curation_rows.submission('A', 20, {})))


def _without(workflow_id: str) -> dict[str, bytes]:
    submission_rows = curation_rows.full_rows()
    del submission_rows[workflow_id]
    return submission_rows


@pytest.mark.parametrize(
    ('submission_rows', 'reason'),
    [
        (
            {
                **curation_rows.full_rows(),
                rows.ROUTING: curation_rows.routing(consequence=evidence_pb2.CONSEQUENCE_UNSPECIFIED),
            },
            'submitted without a consequence class',
        ),
        (_without(rows.ROUTING), 'submitted without a consequence class'),
        (
            {**curation_rows.full_rows(), rows.VERDICT: curation_rows.verdict(svcv4_pb2.CLASSIFICATION_UNSPECIFIED)},
            'submitted without a class',
        ),
        (_without(rows.VERDICT), 'submitted without a class'),
        (
            {
                **curation_rows.full_rows(),
                rows.ROUTING: curation_rows.routing(consequence=evidence_pb2.CONSEQUENCE_UNSPECIFIED),
                rows.VERDICT: curation_rows.verdict(svcv4_pb2.CLASSIFICATION_UNSPECIFIED),
            },
            'submitted without a consequence class or a class',
        ),
    ],
    ids=['no consequence', 'no routing row', 'no class', 'no verdict row', 'neither'],
)
def test_an_incomplete_submission_is_left_out_and_the_rest_stays_the_reference(
    submission_rows: dict[str, bytes], reason: str
) -> None:
    decoded = reference.decode(
        curation_rows.stored(
            curation_rows.submission('A', 20, submission_rows),
            curation_rows.submission('B', 22, curation_rows.full_rows()),
        )
    )
    assert [item.submission.label for item in decoded.complete] == ['B']
    assert decoded.incomplete == (reference.Incomplete(label='A', reason=reason),)


def test_a_storage_fault_in_an_incomplete_submission_still_fails_its_variant() -> None:
    submission_rows = {**_without(rows.VERDICT), 'cln_aff': b'\xff\xff'}
    stored = curation_rows.stored(
        curation_rows.submission('A', 20, submission_rows),
        curation_rows.submission('B', 22, curation_rows.full_rows()),
    )
    with pytest.raises(reference.UnusableReferenceError, match=r'v1: curator A.*does not decode'):
        reference.decode(stored)


# Field 4 of Assessment, a message holding field 1 as text: a case row as the retired section wrote it.
_CASE_ROW = bytes([0x22, 0x06, 0x0A, 0x04]) + b'tall'


def test_a_row_under_the_retired_case_id_fails_its_variant_naming_it() -> None:
    stored = curation_rows.stored(curation_rows.submission('A', 20, {**curation_rows.full_rows(), 'case': _CASE_ROW}))
    with pytest.raises(
        reference.UnusableReferenceError, match=r"v1: curator A, row 'case': 'case' names a retired worksheet section"
    ):
        reference.decode(stored)


@pytest.mark.parametrize('clinical_context', ['', '   ', '\n\t '])
def test_a_registration_without_a_clinical_context_is_refused(clinical_context: str) -> None:
    with pytest.raises(ValueError, match='v1: the registration states no clinical context'):
        dataclasses.replace(curation_rows.REGISTRATION, clinical_context=clinical_context)


@pytest.mark.parametrize('column', ['X_LINKED', 'UNSPECIFIED', ''])
def test_a_registered_mode_the_framework_does_not_name_is_refused(column: str) -> None:
    registration = dataclasses.replace(curation_rows.REGISTRATION, mode_of_inheritance=column)
    with pytest.raises(ValueError, match='v1: '):
        _ = registration.mode


def test_two_submissions_by_one_curator_at_one_instant_leave_no_latest() -> None:
    tied = curation_rows.submission('A', 20, curation_rows.full_rows(), earlier=(20,))
    with pytest.raises(reference.UnusableReferenceError, match='v1: curator A submitted twice at'):
        reference.decode(curation_rows.stored(tied))


def test_the_stored_reference_survives_the_round_file() -> None:
    stored = curation_rows.stored(
        curation_rows.submission('A', 20, curation_rows.full_rows(), earlier=(18,)),
        curation_rows.submission('C', 22, curation_rows.full_rows()),
        outstanding=('B',),
    )
    assert reference.from_json(reference.to_json(stored), 'reference.json') == stored


@pytest.mark.parametrize(
    'clinical_context',
    [curation_rows.REGISTRATION.clinical_context, '  Leading and trailing whitespace.\n\n', 'Café-au-lait macules.'],
    ids=['multi-line', 'surrounding whitespace', 'non-ascii'],
)
def test_the_round_file_keeps_the_clinical_context_verbatim(clinical_context: str) -> None:
    stored = dataclasses.replace(
        curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows())),
        registration=dataclasses.replace(curation_rows.REGISTRATION, clinical_context=clinical_context),
    )
    assert reference.from_json(reference.to_json(stored), 'reference.json').registration.clinical_context == (
        clinical_context
    )


@pytest.mark.parametrize(
    ('stored', 'error', 'reason'),
    [(None, rounds.RoundError, 'has no `clinical_context`'), ('  ', ValueError, 'states no clinical context')],
    ids=['absent', 'blank'],
)
def test_a_round_file_without_a_clinical_context_is_refused(
    stored: str | None, error: type[Exception], reason: str
) -> None:
    document = reference.to_json(curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows())))
    registration = document['registration']
    assert isinstance(registration, dict)
    if stored is None:
        del registration['clinical_context']
    else:
        registration['clinical_context'] = stored
    with pytest.raises(error, match=reason):
        reference.from_json(document, 'reference.json')


def test_a_stored_reference_naming_a_curator_both_submitted_and_outstanding_is_refused() -> None:
    document = reference.to_json(
        curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows()), outstanding=('A',))
    )
    with pytest.raises(rounds.RoundError, match='both submitted and outstanding'):
        reference.from_json(document, 'reference.json')


def test_a_stored_reference_without_a_submission_is_refused() -> None:
    document = reference.to_json(curation_rows.stored(outstanding=('A',)))
    with pytest.raises(rounds.RoundError, match='`submissions` is empty'):
        reference.from_json(document, 'reference.json')


def test_a_stored_reference_of_another_shape_is_refused() -> None:
    document = reference.to_json(curation_rows.stored(curation_rows.submission('A', 20, curation_rows.full_rows())))
    registration = document['registration']
    assert isinstance(registration, dict)
    registration['gene'] = 7
    with pytest.raises(rounds.RoundError, match='`gene` is a string'):
        reference.from_json(document, 'reference.json')


def test_the_date_to_pin_is_the_last_complete_submissions() -> None:
    no_class = {**curation_rows.full_rows(), rows.VERDICT: curation_rows.verdict(svcv4_pb2.CLASSIFICATION_UNSPECIFIED)}
    complete = reference.decode(
        curation_rows.stored(
            curation_rows.submission('A', 20, curation_rows.full_rows(), earlier=(18,)),
            curation_rows.submission('B', 22, curation_rows.full_rows()),
            curation_rows.submission('C', 24, no_class),
        )
    ).complete
    assert reference.last_submitted_at(complete) == curation_rows.at(22)


def test_two_curators_submitting_at_one_instant_still_date_the_round() -> None:
    complete = reference.decode(
        curation_rows.stored(
            curation_rows.submission('A', 22, curation_rows.full_rows()),
            curation_rows.submission('B', 22, curation_rows.full_rows()),
        )
    ).complete
    assert reference.last_submitted_at(complete) == curation_rows.at(22)


def test_no_complete_submission_has_no_date_to_pin() -> None:
    with pytest.raises(ValueError, match='no complete submission'):
        reference.last_submitted_at(())
