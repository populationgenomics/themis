"""The variant-classification scenario: what a round reads, runs and renders per variant.

`prepare` turns every variant with a complete submission into a directory holding its stored reference
and its kickoff, or the reason its reference cannot be used. `run_request` is what the run half is handed for one
variant. `render_variant` decodes a collected run's record and the curators' submissions into YAML for the
reader. The run half never sees anything here but the `RunRequest`.
"""

from __future__ import annotations

import dataclasses
import pathlib
import shutil

from google.protobuf import json_format

from themis.workbench.models import workbench_pb2
from tools.run_review import rounds
from tools.run_review.run import runner
from tools.run_review.variant_classification import kickoff, reference, render

REFERENCE = 'reference.json'
KICKOFF = 'kickoff.json'
UNUSABLE = 'unusable.txt'
RENDERED_DIR = 'rendered'
RENDERED_RECORD = 'record.yaml'
RENDERED_REGISTRATION = 'registration.yaml'


def rendered_submission(label: str) -> str:
    """The name of the file under `rendered/` holding the latest submission of the curator labelled `label`."""
    return f'curator-{label}.yaml'


@dataclasses.dataclass(frozen=True)
class Prepared:
    """The variants a round will run, those whose reference could not be used, and those with no complete submission.

    `unusable` and `incomplete` pair each variant with the reason; an incomplete variant gets no directory,
    as a variant nobody has submitted does not.
    """

    runnable: tuple[str, ...]
    unusable: tuple[tuple[str, str], ...]
    incomplete: tuple[tuple[str, str], ...]


def prepare(round_dir: pathlib.Path, reading: reference.Reading, project_id: str) -> Prepared:
    """Write each variant's reference, and its kickoff or the reason it cannot run, under `round_dir`.

    A variant enters with at least one complete submission; one whose every submission leaves the
    consequence class or the class unset is listed in `Prepared.incomplete` and written nowhere.

    Raises:
        rounds.RoundError: If a variant id cannot name a directory; raised before anything is written.
    """
    references = reading.submitted
    directories = [_variant_dir(round_dir, stored.registration.variant_id) for stored in references]
    unstored = [(_variant_dir(round_dir, variant_id), variant_id, reason) for variant_id, reason in reading.unusable]
    runnable: list[str] = []
    unusable: list[tuple[str, str]] = []
    incomplete: list[tuple[str, str]] = []
    for directory, variant_id, reason in unstored:
        rounds.publish(directory / UNUSABLE, f'{reason}\n'.encode())
        unusable.append((variant_id, reason))
    for stored, directory in zip(references, directories, strict=True):
        variant_id = stored.registration.variant_id
        try:
            decoded = reference.decode(stored)
            built = (
                kickoff.build(stored.registration, reference.last_submitted_at(decoded.complete), project_id)
                if decoded.complete
                else None
            )
        except reference.UnusableReferenceError as e:
            rounds.write_json(directory / REFERENCE, reference.to_json(stored))
            rounds.publish(directory / UNUSABLE, f'{e}\n'.encode())
            unusable.append((variant_id, str(e)))
            continue
        if built is None:
            incomplete.append(
                (variant_id, '; '.join(f'curator {item.label} {item.reason}' for item in decoded.incomplete))
            )
            continue
        rounds.write_json(directory / REFERENCE, reference.to_json(stored))
        rounds.write_json(
            directory / KICKOFF,
            {
                'inputs': json_format.MessageToDict(built.inputs, preserving_proto_field_name=True),
                'date_to_pin': built.date_to_pin.isoformat(),
                'date_applied': kickoff.DATE_APPLIED,
            },
        )
        runnable.append(variant_id)
    return Prepared(runnable=tuple(runnable), unusable=tuple(unusable), incomplete=tuple(incomplete))


def _variant_dir(round_dir: pathlib.Path, variant_id: str) -> pathlib.Path:
    if not variant_id or pathlib.PurePath(variant_id).name != variant_id or variant_id in ('.', '..'):
        raise rounds.RoundError(f'variant id {variant_id!r} cannot name a directory')
    return round_dir / variant_id


def run_request(variant_dir: pathlib.Path, project_id: str) -> runner.RunRequest | None:
    """The request the run half creates this variant's run from; None for a variant whose reference is unusable.

    Raises:
        rounds.RoundError: If the directory holds neither a kickoff nor the reason it has none.
    """
    document = rounds.read_json(variant_dir / KICKOFF)
    if document is None:
        if (variant_dir / UNUSABLE).is_file():
            return None
        raise rounds.RoundError(f'{variant_dir} holds no {KICKOFF} and no {UNUSABLE}; `prepare` writes one of them')
    inputs = json_format.ParseDict(
        rounds.Fields(document, str(variant_dir / KICKOFF)).mapping('inputs'), workbench_pb2.AnalysisInputs()
    )
    return runner.RunRequest(project_id=project_id, inputs=inputs)


def render_variant(variant_dir: pathlib.Path) -> list[pathlib.Path]:
    """Render the collected run's record, the registration and every complete submission into `rendered/`.

    Raises:
        rounds.RoundError: If the variant has no stored reference or no collected run.
        render.RenderError: If the run's record does not decode, or a message holds a field or enum number its
            schema lacks.
        reference.UnusableReferenceError: If the stored reference no longer decodes.
        ValueError: If the registration's mode of inheritance is not one the framework names, its clinical context
            is blank, or a submission's time carries no time zone.
    """
    stored_json = rounds.read_json(variant_dir / REFERENCE)
    if stored_json is None:
        raise rounds.RoundError(f'{variant_dir} holds no {REFERENCE}')
    record = runner.load(variant_dir / rounds.RUN_DIR)
    if record is None or record.collected_at is None:
        raise rounds.RoundError(f'{variant_dir}: the run was not collected, so there is nothing to render')
    stored = reference.from_json(stored_json, str(variant_dir / REFERENCE))
    decoded_reference = reference.decode(stored)
    texts = {
        RENDERED_RECORD: render.record(variant_dir / rounds.RUN_DIR / runner.REPOSITORY),
        RENDERED_REGISTRATION: render.registration(stored, decoded_reference),
        **{
            rendered_submission(decoded.submission.label): render.submission(decoded)
            for decoded in decoded_reference.complete
        },
    }
    out = variant_dir / RENDERED_DIR
    # Every file is rendered before the old ones go, so a failure leaves the last rendering, and none outlives it.
    shutil.rmtree(out, ignore_errors=True)
    for name, text in texts.items():
        rounds.publish(out / name, text.encode('utf-8'))
    return [out / name for name in texts]
