"""YAML for what a model cannot read: the run's record, the curators' stored rows, and the registration.

The record and the rows are protobuf messages, so each is written through protobuf's canonical JSON
mapping: every field the message sets, under its proto name, an enum under its member's name and a
timestamp in RFC 3339. A field or an enum number the schema does not name, written by a newer schema,
fails the rendering rather than vanish from it, so nothing is compared and nothing is left out. What
each file is, and what an absent field means, is stated in `#` comments at its top. The registration
states its mode of inheritance under the member name the record's entity uses
(docs/design/run-review-loop.md §"Code renders, and the reader compares").
"""

from __future__ import annotations

import datetime
import math
import pathlib
import subprocess
import typing
from collections.abc import Mapping, Sequence

import yaml
from google.protobuf import json_format, message

from themis.curation import rows
from themis.svcv4.models import svcv4_pb2
from themis.widgets import asset
from themis.widgets.models import svcv4_classification_pb2
from tools.run_review import protos
from tools.run_review.variant_classification import reference

RECORD_PATH = 'assets/svcv4.binpb'
# The working document's branch (docs/design/workbench-workspace.md), as the clone names it.
FINAL_REVISION = 'refs/remotes/origin/main'

_DEFAULTS = (
    'A field not listed holds its default: empty, zero, false, or the enum member numbered 0, which reads as '
    'unspecified.'
)
_RECORD_HEADING = "The run's classification record."


class RenderError(Exception):
    """A message cannot be rendered whole: it does not decode, or holds a field or enum number its schema lacks."""


class _Dumper(yaml.SafeDumper):
    """The safe dumper, writing a multi-line string as a literal block, and a list's items indented under its key."""

    @typing.override
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        del indentless
        super().increase_indent(flow, indentless=False)


# Line breaks YAML reads besides \n; written raw, they read back as \n or a space, so only escaped ones survive.
_OTHER_BREAKS = frozenset('\x85\u2028\u2029')


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if _OTHER_BREAKS & set(value):
        style = '"'
    elif '\n' in value:
        style = '|'
    else:
        style = None
    return dumper.represent_scalar('tag:yaml.org,2002:str', value, style=style)


_Dumper.add_representer(str, _represent_str)


def _yaml(comments: Sequence[str], document: Mapping[str, object]) -> str:
    """`document` as YAML, keys in the order given, under one `#` line per item of `comments`.

    Raises:
        ValueError: If a comment spans lines, so its later lines would read as YAML.
        RenderError: If the YAML does not read back as `document`.
    """
    if multi_line := [line for line in comments if '\n' in line]:
        raise ValueError(f'a comment line spans lines: {multi_line!r}')
    header = ''.join(f'# {line}\n' for line in comments)
    # An infinite width keeps every scalar on one line, as the dumper would otherwise fold long text.
    body = yaml.dump(dict(document), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=math.inf)
    if yaml.safe_load(body) != document:
        raise RenderError('the YAML written does not read back as the document it was written from')
    return header + body


def message_fields(msg: message.Message) -> dict[str, object]:
    """Every field `msg` sets, as protobuf's canonical JSON mapping lays it out under the proto field names.

    Raises:
        RenderError: If `msg` or a message inside it holds a field or an enum number its schema does not name,
            or a value the JSON mapping has no form for, such as a timestamp out of range.
    """
    if undeclared := protos.undeclared(msg):
        raise RenderError(
            f'{msg.DESCRIPTOR.full_name} holds what its schema does not name ({"; ".join(undeclared)}): a newer '
            'writer set it, so regenerate the stubs before rendering, or it was retired after it was written'
        )
    try:
        return json_format.MessageToDict(msg, preserving_proto_field_name=True)
    except json_format.Error as e:
        raise RenderError(f'{msg.DESCRIPTOR.full_name} has no canonical JSON form: {e}') from e


def record(repository: pathlib.Path) -> str:
    """The run's final classification record as YAML, or a `record: null` with the reason the run left none.

    Raises:
        RenderError: If the record does not decode.
        subprocess.CalledProcessError: If `repository` is not a git repository git can read.
    """
    _git(repository, 'rev-parse', '--git-dir')
    if _git(repository, 'rev-parse', '--verify', '--quiet', f'{FINAL_REVISION}^{{commit}}', check=False) is None:
        return _yaml(
            [_RECORD_HEADING, 'The run pushed no commit to main, so it left no record to compare.'],
            {'commit': None, 'record': None},
        )
    commit = _git(repository, 'rev-parse', f'{FINAL_REVISION}^{{commit}}').decode('ascii').strip()
    if _git(repository, 'cat-file', '-e', f'{commit}:{RECORD_PATH}', check=False) is None:
        return _yaml(
            [
                _RECORD_HEADING,
                f'The final revision of main, `commit`, holds no `{RECORD_PATH}`, so the run produced nothing to '
                'compare.',
            ],
            {'commit': commit, 'record': None},
        )
    payload = _git(repository, 'cat-file', 'blob', f'{commit}:{RECORD_PATH}')
    # Without its validation rules: a run interrupted mid-edit can leave a record that breaks them, and it is read.
    try:
        record_message = asset.parse(payload)
    except asset.AssetError as e:
        raise RenderError(f'{RECORD_PATH} at {commit} does not decode as a widget asset: {e}') from e
    if not isinstance(record_message, svcv4_classification_pb2.Svcv4Classification):
        found = 'no widget asset' if record_message is None else record_message.DESCRIPTOR.full_name
        raise RenderError(f'{RECORD_PATH} at {commit} holds {found}, not an Svcv4Classification asset')
    return _yaml(
        [
            _RECORD_HEADING,
            f'Decoded from `{RECORD_PATH}` at `commit`, the final revision of main; earlier revisions are in the '
            "repository's history.",
            _DEFAULTS,
        ],
        {'commit': commit, 'record': message_fields(record_message)},
    )


@typing.overload
def _git(repository: pathlib.Path, *args: str) -> bytes: ...
@typing.overload
def _git(repository: pathlib.Path, *args: str, check: typing.Literal[False]) -> bytes | None: ...
def _git(repository: pathlib.Path, *args: str, check: bool = True) -> bytes | None:
    """The stdout of `git -C repository *args`. A failure raises, or with `check` False reads as None."""
    result = subprocess.run(  # noqa: S603 — fixed argv; the path is the round's own clone
        ['git', '-C', str(repository), *args],  # noqa: S607 — git as installed
        capture_output=True,
        check=check,
    )
    return result.stdout if result.returncode == 0 else None


def registration(stored: reference.StoredReference, decoded: reference.DecodedReference) -> str:
    """The registration as YAML: the variant, the entity and clinical context both sides were given, and who answered.

    Raises:
        ValueError: If the registration's mode of inheritance is not one the framework names, or no curator's
            submission is complete, so there is no reference to render.
    """
    if not decoded.complete:
        raise ValueError(f'{stored.registration.variant_id}: no curator submitted a complete worksheet')
    registered = stored.registration
    return _yaml(
        [
            'The registration: the variant as the manager registered it, against the entity that is the question, '
            'the disease under the mode of inheritance, with the clinical context the manager wrote. Every curator '
            'was shown it read-only, and the run was told the entity, then this clinical context without its '
            "surrounding whitespace, in the kickoff's clinical context.",
            'clingen_allele_id is empty where the manager registered none.',
            'curators.in_reference: the curators whose latest submission is the reference.',
            'curators.left_out: those whose latest submission left the consequence class or the class unset, which '
            'is left out of the reference, each with what it left unset.',
            'curators.outstanding: those assigned who had not submitted when the round was prepared, who are absent '
            'from the reference.',
        ],
        {
            'variant': f'{registered.transcript}:{registered.hgvs_c}',
            'gene': registered.gene,
            'clingen_allele_id': registered.clingen_allele_id,
            'disease': registered.disease_label,
            'mondo_id': registered.mondo_id,
            'mode_of_inheritance': svcv4_pb2.ModeOfInheritance.Name(registered.mode),
            'clinical_context': registered.clinical_context,
            'curators': {
                'in_reference': [item.submission.label for item in decoded.complete],
                'left_out': [{'curator': item.label, 'reason': item.reason} for item in decoded.incomplete],
                'outstanding': list(stored.outstanding),
            },
        },
    )


def submission(decoded: reference.Decoded) -> str:
    """One curator's latest submission as YAML: its dates and history, then every row, worksheet-level sections first.

    Raises:
        RenderError: If a row holds a field or an enum number its schema does not name.
        ValueError: If a submission's time carries no time zone, so it names no instant.
    """
    item = decoded.submission
    return _yaml(
        [
            f"Curator {item.label}'s latest submission, answered against workflows version `workflows_version`.",
            'history: every submission by this curator, oldest first, with the note the curator left with it; the '
            'last is the one below.',
            'rows: every row of the latest submission by workflow id, worksheet-level sections first.',
            _DEFAULTS,
        ],
        {
            'curator': item.label,
            'submitted_at': _instant(item.submitted_at),
            'workflows_version': item.workflows_version,
            'history': [{'at': _instant(h.at), 'note': h.note} for h in item.history],
            'rows': {
                workflow_id: message_fields(getattr(decoded.assessments[workflow_id], rows.kind_of(workflow_id)))
                for workflow_id in _row_order(list(decoded.assessments))
            },
        },
    )


def _instant(at: datetime.datetime) -> str:
    """`at` in RFC 3339.

    Raises:
        ValueError: If `at` carries no time zone.
    """
    if at.tzinfo is None:
        raise ValueError(f'{at.isoformat()} carries no time zone, so it names no instant')
    return at.isoformat()


def _row_order(workflow_ids: Sequence[str]) -> list[str]:
    sections = [row for row in rows.KIND_OF if row in workflow_ids]
    return sections + sorted(wid for wid in workflow_ids if wid not in rows.KIND_OF)
