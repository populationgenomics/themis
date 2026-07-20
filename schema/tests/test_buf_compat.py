"""Test the S0.6 proto compat gate (``tools.schema.buf_compat``).

Each case under ``compat/`` is a module pair — a ``base`` and a ``new`` proto tree —
that the real ``buf`` image is run over through ``buf_compat.compare``, with the log
and the verdict it produces committed beside it as ``expected.txt``. The gate's output
*is* its product, so a golden makes a change to it reviewable in a diff.

The two Docker-gated tests outside that set assert properties of the repo's own
committed module, which is not an a/b scenario. The two without Docker cover the tool
failing rather than reporting — a condition no module pair can produce.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest

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
