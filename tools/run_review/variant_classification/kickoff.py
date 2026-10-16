"""What a variant-classification run is created with, and the date the round would pin it to.

The kickoff carries what the create form carries, and all of it comes from the registration: the
transcript, the coding change and the clinical context. The clinical context is the registered entity,
stated as the one to classify against, then the registration's own clinical context, verbatim but for
its surrounding whitespace, which every curator was shown read-only. A curator's submission is the
answer a run is read against, so nothing from one reaches the kickoff. The date the round would pin
the run's evidence to is that of the latest complete submission; no service applies such a date yet,
so the round records it together with that fact, and the run is never sent it
(docs/design/run-review-loop.md §"What the kickoff carries", §"The date a run may see up to").
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Mapping

import protovalidate

from themis.svcv4.models import svcv4_pb2
from themis.workbench.models import workbench_pb2
from tools.run_review.variant_classification import reference

# Whether a service limited the run's evidence to `date_to_pin`. None does yet; the round records that
# beside the date so the reader never assumes a cutoff.
DATE_APPLIED = False

# Each mode a registration can state, as the clinical context names it.
MODE_WORDS: Mapping[svcv4_pb2.ModeOfInheritance.ValueType, str] = {
    svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_DOMINANT: 'autosomal dominant',
    svcv4_pb2.MODE_OF_INHERITANCE_AUTOSOMAL_RECESSIVE: 'autosomal recessive',
    svcv4_pb2.MODE_OF_INHERITANCE_SEMIDOMINANT: 'semidominant',
    svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_DOMINANT: 'X-linked dominant',
    svcv4_pb2.MODE_OF_INHERITANCE_X_LINKED_RECESSIVE: 'X-linked recessive',
}


@dataclasses.dataclass(frozen=True)
class Kickoff:
    """A run's inputs, and the date the round would pin; only `inputs` is sent to the run."""

    inputs: workbench_pb2.AnalysisInputs
    date_to_pin: datetime.datetime


def build(registration: reference.Registration, date_to_pin: datetime.datetime, project_id: str) -> Kickoff:
    """The kickoff for one variant, checked against the create request's own rules.

    Args:
        registration: The variant as registered, the only source of the run's inputs.
        date_to_pin: When the last complete submission was made, recorded beside the inputs and never in them.
        project_id: The project the run is created in, which the create request's rules check too.

    Raises:
        reference.UnusableReferenceError: If the registration names its disease by no label, or the inputs
            break a rule `CreateAnalysis` would reject them for, such as a clinical context over its length
            limit.
        ValueError: If the registration's mode of inheritance is not one the framework names.
    """
    if not registration.disease_label.strip():
        raise reference.UnusableReferenceError(
            f'{registration.variant_id}: the registration names its disease by no label, so the kickoff cannot '
            'state the entity'
        )
    inputs = workbench_pb2.AnalysisInputs(
        variant_classification=workbench_pb2.VariantClassificationInputs(
            transcript=registration.transcript,
            hgvs_c=registration.hgvs_c,
            clinical_context=clinical_context(registration),
        )
    )
    try:
        protovalidate.validate(workbench_pb2.CreateAnalysisRequest(project_id=project_id, inputs=inputs))
    except protovalidate.ValidationError as e:
        raise reference.UnusableReferenceError(f'{registration.variant_id}: CreateAnalysis would refuse it: {e}') from e
    return Kickoff(inputs=inputs, date_to_pin=date_to_pin)


def clinical_context(registration: reference.Registration) -> str:
    """The registered entity as the one to classify against, a blank line, then the registration's clinical context.

    The registration's clinical context is carried verbatim but for its surrounding whitespace.

    Raises:
        ValueError: If the registration's mode of inheritance is not one the framework names.
    """
    entity = (
        f'Entity to classify against: {registration.disease_label.strip()} ({registration.mondo_id}), '
        f'{MODE_WORDS[registration.mode]}.'
    )
    return f'{entity}\n\n{registration.clinical_context.strip()}'
