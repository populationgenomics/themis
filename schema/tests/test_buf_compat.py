"""Test the S0.6 proto compat gate (``tools.schema.buf_compat``).

Each case under ``compat/`` is a module pair — a ``base`` and a ``new`` proto tree —
that the real ``buf`` image is run over through ``buf_compat.compare``, with the log
and the verdict it produces committed beside it as ``expected.txt``. The gate's output
*is* its product, so a golden makes a change to it reviewable in a diff.

The two Docker-gated tests outside that set assert properties of the repo's own
committed module, which is not an a/b scenario. The two without Docker cover the tool
failing rather than reporting — a condition no module pair can produce. The rule on who
writes a released widget field has one case in the set, and is held to each kind of
change without Docker, over descriptor sets built here.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
from collections.abc import Sequence

import pytest
from google.protobuf import descriptor_pb2, json_format

from themis.widgets.models import widget_pb2
from tools.schema import buf_compat

_CASES = pathlib.Path(__file__).parent / 'compat'
_BASELINE = 'origin/main'
_MISSING_IMPORT = 'import "buf/validate/validate.proto": file does not exist'

# The pre-release scenario names the contract `_PRE_RELEASE` actually lists; when that
# contract graduates, the scenario is describing a carve-out that no longer applies.
_PRE_RELEASE_RELPATH = 'themis/litcache/models/litcache.proto'
assert _PRE_RELEASE_RELPATH in buf_compat._PRE_RELEASE, 'update or drop the pre-release-renamed case'


def _render(outcome: buf_compat._Outcome) -> str:
    """The gate's whole observable result: what it logs, then how it exits."""
    verdict = f'FAIL: {outcome.failure}' if outcome.failure else 'PASS'
    return '\n'.join([*outcome.lines, verdict]) + '\n'


def _run_case(case: pathlib.Path) -> str:
    """Run the gate over a case's module pair, as ``main`` runs it over the repo's."""
    with tempfile.TemporaryDirectory(dir=buf_compat._REPO_ROOT, prefix='.buf-') as tmp:
        scratch = pathlib.Path(tmp)
        for side in ('new', 'base'):
            shutil.copytree(case / side, scratch / side)
            # the module definition is the repo's, not the case's: only protos vary
            for name in buf_compat._MODULE_FILES:
                shutil.copy2(buf_compat._REPO_ROOT / name, scratch / side / name)
        return _render(buf_compat.compare(scratch, _BASELINE))


@pytest.mark.usefixtures('docker_daemon')
@pytest.mark.parametrize('case', sorted(p.name for p in _CASES.iterdir() if p.is_dir()))
def test_compat_case(case: str) -> None:
    assert _run_case(_CASES / case) == (_CASES / case / 'expected.txt').read_text()


@pytest.mark.usefixtures('docker_daemon')
def test_committed_protos_resolve_their_declared_deps() -> None:
    """The committed module builds — a proto importing `buf/validate` compiles."""
    with tempfile.TemporaryDirectory(dir=buf_compat._REPO_ROOT, prefix='.buf-') as tmp:
        scratch = pathlib.Path(tmp)
        buf_compat._materialise(scratch / 'new', None)
        buf_compat._materialise(scratch / 'base', None)
        outcome = buf_compat.compare(scratch, _BASELINE)
    assert outcome == ([], None)


@pytest.mark.usefixtures('docker_daemon')
def test_a_module_that_cannot_resolve_its_deps_is_a_build_failure() -> None:
    """Buf tags an unresolvable import `COMPILE`.

    It reports one exactly as it reports an incompatibility, so the tag is the only
    thing separating "did not build" from "broke a contract". `buf.lock` is what
    resolves the import: drop it from one side and the import stops existing.
    """
    with tempfile.TemporaryDirectory(dir=buf_compat._REPO_ROOT, prefix='.buf-') as tmp:
        scratch = pathlib.Path(tmp)
        buf_compat._materialise(scratch / 'new', None)
        buf_compat._materialise(scratch / 'base', None)
        (scratch / 'new' / 'buf.lock').unlink()
        outcome = buf_compat.compare(scratch, _BASELINE)
    assert outcome.failure == 'compat gate: the proto module failed to build'
    assert any(_MISSING_IMPORT in line for line in outcome.lines)


def test_buf_reporting_nothing_on_a_non_zero_exit_is_a_tool_failure() -> None:
    outcome = buf_compat._outcome('', 'docker: daemon refused the connection', 125, _BASELINE)
    assert outcome.failure is not None
    assert 'buf exited 125 without reporting anything' in outcome.failure
    assert any('daemon refused' in line for line in outcome.lines)  # the raw streams explain it


def test_docker_noise_on_stderr_does_not_fail_a_clean_run() -> None:
    """A cold runner pulls the pinned image on every run, onto stderr.

    Buf's findings come from stdout alone; folding the two streams together read the
    pull progress as a failed gate.
    """
    pull_noise = (
        "Unable to find image 'bufbuild/buf@sha256:abc' locally\n"
        '1f3e46996e29: Pulling fs layer\n'
        'Status: Downloaded newer image for bufbuild/buf@sha256:abc\n'
    )
    assert buf_compat._outcome('', pull_noise, 0, _BASELINE) == ([], None)


_GATE_FILE = 'themis/widgets/cases/gate.proto'
_GATE_PACKAGE = 'themis.widgets.cases.gate'


def _field(
    name: str, number: int, *, guard: Sequence[str] | None = None, key: bool = False, message: str = ''
) -> dict[str, object]:
    """A field, a guard ignoring `guard` when that is given, the element key when `key` is set."""
    options: dict[str, object] = {}
    if guard is not None:
        options['[themis.widgets.models.guard]'] = {'ignores': list(guard)} if guard else {}
    if key:
        options['[themis.widgets.models.element_key]'] = True
    shape = (
        {'type': 'TYPE_MESSAGE', 'typeName': f'.{_GATE_PACKAGE}.{message}', 'label': 'LABEL_REPEATED'}
        if message
        else {'type': 'TYPE_STRING' if key else 'TYPE_BOOL', 'label': 'LABEL_OPTIONAL'}
    )
    return {'name': name, 'number': number, **shape, **({'options': options} if options else {})}


def _files(**messages: list[dict[str, object]]) -> descriptor_pb2.FileDescriptorSet:
    """A module holding `messages`, each a name and its fields, beside the options they read."""
    files = descriptor_pb2.FileDescriptorSet()
    for dependency in (descriptor_pb2.DESCRIPTOR, widget_pb2.DESCRIPTOR):
        dependency.CopyToProto(files.file.add())
    body = {
        'name': _GATE_FILE,
        'package': _GATE_PACKAGE,
        'dependency': [widget_pb2.DESCRIPTOR.name],
        'syntax': 'proto3',
        'messageType': [{'name': name, 'field': fields} for name, fields in messages.items()],
    }
    json_format.ParseDict(body, files.file.add())
    return files


# The released module each case changes: a list of rows a user ticks, each tick judging its label and ignoring its
# hint, and a note beside it holding no judgement.
_ROW = [_field('id', 1, key=True), _field('done', 2, guard=['hint']), _field('label', 3), _field('hint', 4)]
_NOTE = [_field('text', 1)]
_HOLDER = [_field('rows', 1, message='Row'), _field('note', 2, message='Note')]
_ROW_DONE = 1


def _row(done: dict[str, object], *extra: dict[str, object]) -> list[dict[str, object]]:
    return [*_ROW[:_ROW_DONE], done, *_ROW[_ROW_DONE + 1 :], *extra]


@pytest.mark.parametrize(
    ('new', 'expected'),
    [
        pytest.param(_files(Holder=_HOLDER, Row=_ROW, Note=_NOTE), [], id='unchanged'),
        pytest.param(
            _files(Holder=_HOLDER, Row=_row(_field('done', 2)), Note=_NOTE),
            ['"done" on message "themis.widgets.cases.gate.Row" is no longer a guard'],
            id='a guard dropped',
        ),
        pytest.param(
            _files(Holder=_HOLDER, Row=[*_ROW[:2], _field('label', 3, guard=[]), _ROW[3]], Note=_NOTE),
            ['"label" on message "themis.widgets.cases.gate.Row" became a guard'],
            id='a released field turned into a guard',
        ),
        pytest.param(
            _files(Holder=_HOLDER, Row=_row(_field('done', 2, guard=[])), Note=_NOTE),
            ['"done" on message "themis.widgets.cases.gate.Row" no longer ignores hint'],
            id='a guard ignoring less',
        ),
        pytest.param(
            _files(Holder=_HOLDER, Row=_row(_field('done', 2, guard=['hint', 'label'])), Note=_NOTE),
            ['"done" on message "themis.widgets.cases.gate.Row" now ignores label, released before it'],
            id='a guard ignoring a released field',
        ),
        pytest.param(
            _files(
                Holder=_HOLDER, Row=_row(_field('done', 2, guard=['hint', 'shade']), _field('shade', 5)), Note=_NOTE
            ),
            [],
            id='a guard ignoring a field new with the change',
        ),
        pytest.param(
            _files(
                Holder=_HOLDER,
                Row=[*_ROW[:1], _field('done', 2, guard=['hint', 'caption']), _field('caption', 3), _ROW[3]],
                Note=_NOTE,
            ),
            ['"done" on message "themis.widgets.cases.gate.Row" now ignores caption, released before it'],
            id='a guard ignoring a released field under a new name',
        ),
        pytest.param(
            _files(Holder=_HOLDER, Row=[*_ROW[:3], _field('note', 4)], Note=_NOTE),
            ['"done" on message "themis.widgets.cases.gate.Row" no longer ignores hint'],
            id='the ignored field renamed out from under the guard',
        ),
        pytest.param(
            _files(Holder=_HOLDER, Row=[_field('id', 1), *_ROW[1:3], _field('hint', 4, key=True)], Note=_NOTE),
            [
                '"id" on message "themis.widgets.cases.gate.Row" changed whether it is the element_key',
                '"hint" on message "themis.widgets.cases.gate.Row" changed whether it is the element_key',
            ],
            id='the key moved',
        ),
        pytest.param(
            _files(Holder=_HOLDER, Row=[*_ROW, _field('flag', 5, guard=[])], Note=_NOTE),
            ['Message "themis.widgets.cases.gate.Row" gained the guard flag'],
            id='a message holding a guard gains another',
        ),
        pytest.param(
            _files(Holder=_HOLDER, Row=_ROW, Note=[*_NOTE, _field('seen', 2, guard=[])]),
            [
                'Message "themis.widgets.cases.gate.Note" gained the guard seen',
                'Message "themis.widgets.cases.gate.Note" held no guard, directly or beneath it',
            ],
            id='a message holding none gains a guard',
        ),
        pytest.param(
            _files(
                Holder=_HOLDER,
                Row=_ROW,
                Note=[*_NOTE, _field('marks', 2, message='Mark')],
                Mark=[_field('done', 1, guard=[])],
            ),
            ['Message "themis.widgets.cases.gate.Note" held no guard, directly or beneath it'],
            id='a message holding none gains one beneath it',
        ),
        pytest.param(
            _files(
                Holder=_HOLDER, Row=_ROW, Note=_NOTE, Other=[_field('id', 1, key=True), _field('done', 2, guard=[])]
            ),
            [],
            id='a new message holds a guard',
        ),
    ],
)
def test_the_gate_refuses_a_change_to_a_released_message_s_judgements(
    new: descriptor_pb2.FileDescriptorSet, expected: list[str]
) -> None:
    changes = buf_compat.ownership_changes(_files(Holder=_HOLDER, Row=_ROW, Note=_NOTE), new)
    found = changes.get(_GATE_FILE, [])
    assert set(changes) <= {_GATE_FILE}
    assert len(found) == len(expected), found
    for finding, part in zip(found, expected, strict=True):
        assert part in finding


@pytest.mark.parametrize(('before', 'after', 'expected'), [(False, True, 'became'), (True, False, 'is no longer')])
def test_the_gate_refuses_a_released_message_gaining_or_losing_the_widget_mark(
    before: bool, after: bool, expected: str
) -> None:
    """An older hook refuses every asset of a type it holds unmarked, and checks one it holds marked as a payload."""

    def marked(widget: bool) -> descriptor_pb2.FileDescriptorSet:
        files = _files(Holder=_HOLDER, Row=_ROW, Note=_NOTE)
        if widget:
            files.file[-1].message_type[0].options.Extensions[widget_pb2.widget] = True  # pyright: ignore[reportArgumentType]
        return files

    [finding] = buf_compat.ownership_changes(marked(before), marked(after))[_GATE_FILE]
    assert f'Message "{_GATE_PACKAGE}.Holder" {expected} a widget payload' in finding
