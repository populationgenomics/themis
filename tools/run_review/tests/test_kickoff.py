from __future__ import annotations

import dataclasses

import pytest

from themis.svcv4.models import svcv4_pb2
from tools.run_review.tests import curation_rows
from tools.run_review.variant_classification import kickoff, reference

_ENTITY = 'Entity to classify against: Marfan syndrome (MONDO:0007947), autosomal dominant.'


@pytest.mark.parametrize(
    ('stored', 'carried'),
    [
        (curation_rows.REGISTRATION.clinical_context, curation_rows.REGISTRATION.clinical_context),
        ('\n  Tall stature.\n\n  Lens dislocation at 9.  \n', 'Tall stature.\n\n  Lens dislocation at 9.'),
    ],
    ids=['as stored', 'surrounding whitespace'],
)
def test_the_clinical_context_is_the_entity_then_the_registrations_own(stored: str, carried: str) -> None:
    registration = dataclasses.replace(curation_rows.REGISTRATION, clinical_context=stored)
    built = kickoff.build(registration, curation_rows.at(20), 'p')
    inputs = built.inputs.variant_classification
    assert (inputs.transcript, inputs.hgvs_c) == (registration.transcript, registration.hgvs_c)
    assert inputs.clinical_context == f'{_ENTITY}\n\n{carried}'


def test_the_date_to_pin_is_recorded_beside_the_inputs_and_not_in_them() -> None:
    built = kickoff.build(curation_rows.REGISTRATION, curation_rows.at(22), 'p')
    assert built.date_to_pin == curation_rows.at(22)
    assert built.inputs == kickoff.build(curation_rows.REGISTRATION, curation_rows.at(20), 'p').inputs


def test_every_mode_a_registration_can_state_has_words() -> None:
    stated = {value.number for value in svcv4_pb2.ModeOfInheritance.DESCRIPTOR.values} - {
        svcv4_pb2.MODE_OF_INHERITANCE_UNSPECIFIED
    }
    assert set(kickoff.MODE_WORDS) == stated


def test_a_registration_without_a_disease_label_is_unusable() -> None:
    registration = dataclasses.replace(curation_rows.REGISTRATION, disease_label=' ')
    with pytest.raises(reference.UnusableReferenceError, match='v1: the registration names its disease by no label'):
        kickoff.build(registration, curation_rows.at(20), 'p')


def test_a_composed_context_the_create_form_would_refuse_is_unusable() -> None:
    # The registration's bound is the create request's own, so a context at it fits alone and not beside the entity.
    registration = dataclasses.replace(curation_rows.REGISTRATION, clinical_context='x' * 10_000)
    with pytest.raises(reference.UnusableReferenceError, match='CreateAnalysis would refuse it'):
        kickoff.build(registration, curation_rows.at(20), 'p')
