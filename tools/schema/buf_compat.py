"""Backward-compatibility gate for the committed gRPC proto contracts (S0.6).

The **sole** authored-data compat gate (proto.md): diffs the committed proto module
under ``schema/proto`` against its baseline with ``buf breaking`` (FILE category —
field renumber/removal, type/label changes, renames), failing on any incompatible
delta — additive-only evolution, no override (``docs/design/proto.md`` "Schema
evolution"). Gates every committed proto — RPC and at-rest alike; a pre-release
contract (no persisted data, no deployed consumer) is left out of the compared module
until it stabilizes (see ``_PRE_RELEASE``), so it has no baseline to be incompatible
with.

**Baseline.** The released line, stood in by the PR base branch (``main`` under
additive-only evolution). Pass it explicitly as ``--baseline-ref`` (CI supplies
``origin/<base>``; locally e.g. ``main``).

Each side is materialised as the repo's *own* module — ``buf.yaml`` + ``buf.lock``
alongside the ``schema/proto`` tree — because a proto's imports only resolve against
the deps its module declares (``buf/validate/validate.proto`` comes from the pinned
``buf.build/bufbuild/protovalidate``). Imports are excluded from the comparison: a
dep bump is that dep's change, not ours.

``buf breaking`` runs from a pinned ``docker run``, which needs network to fetch the
declared deps. Runs in CI (Docker present on the runner); locally needs Docker and a
baseline ref: ``uv run python -m tools.schema.buf_compat --baseline-ref main``.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple

from tools.schema import baseline

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PROTO_DIR = _REPO_ROOT / 'schema' / 'proto'
_PROTO_TREE = 'schema/proto'
# buf reports a finding's path relative to the run's working directory, so it carries
# the input side's directory: new/schema/proto/<module-relative path>.
_FINDING_PREFIX = f'new/{_PROTO_TREE}/'
# A deleted file has no surviving path, and buf omits the field entirely; such a
# finding is scoped to the module rather than to any one contract.
_MODULE_SCOPE = '<module>'

# The module definition each side is rebuilt from; buf.lock pins the dep commits so
# both sides resolve `buf/validate/validate.proto` to the same module.
_MODULE_FILES = ('buf.yaml', 'buf.lock')

# Pre-release contracts, held out of the compared module on both sides: no persisted data and
# no deployed consumer, so a one-time reshape is intended. Each rejoins the gate once released.
# Paths are relative to _PROTO_DIR, and a renamed or deleted contract keeps its old path listed
# until the baseline no longer carries it — a path here is never compared, present or not.
_PRE_RELEASE = frozenset({'themis/litcache/models/litcache.proto'})

# buf breaking runner, pinned by digest (not a moving tag).
_BUF_IMAGE = 'bufbuild/buf@sha256:c34c81ac26044490a10fb5009eb618640834b9048f38d4717538421c6a25e4d7'


def _materialise(side: pathlib.Path, ref: str | None) -> None:
    """Write the repo's proto module into ``side``, at ``ref`` (working tree if None)."""
    side.mkdir(parents=True, exist_ok=True)
    if ref is None:
        for name in _MODULE_FILES:
            shutil.copy2(_REPO_ROOT / name, side / name)
        shutil.copytree(_PROTO_DIR, side / _PROTO_TREE)
        return
    archive = subprocess.run(  # noqa: S603
        ['git', 'archive', ref, '--', *_MODULE_FILES, _PROTO_TREE],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise SystemExit(f'cannot read the proto module at {ref}: {archive.stderr.decode().strip()}')
    subprocess.run(['tar', '-x', '-C', str(side)], input=archive.stdout, check=True)  # noqa: S603, S607


def _run_buf(scratch: pathlib.Path) -> tuple[str, str, int]:
    """Diff the materialised module against its baseline with ``buf breaking``.

    Scratch sits under the repo so the Docker host mount reaches it (Colima/Lima mount
    $HOME, not /tmp). Returns ``(stdout, stderr, returncode)``. buf writes every
    finding — incompatibility and build error alike — as JSON on stdout; stderr
    carries docker's own noise, such as the image pull on a cold cache, so the two
    are never folded together.
    """
    cmd = [
        'docker',
        'run',
        '--rm',
        '-v',
        f'{scratch}:/work',
        '-w',
        '/work',
    ]
    # The pinned deps are fetched into the container's cache. Reuse the host's when it
    # exists so a warm machine needs no network (and an intercepting TLS proxy cannot
    # break the fetch); CI runs cold and fetches.
    host_cache = pathlib.Path.home() / '.cache' / 'buf'
    if host_cache.is_dir():
        cmd += ['-v', f'{host_cache}:/buf-cache', '-e', 'BUF_CACHE_DIR=/buf-cache']
    cmd += [
        _BUF_IMAGE,
        'breaking',
        'new',
        '--against',
        'base',
        '--exclude-imports',
        '--error-format',
        'json',
    ]
    # timeout so an unreachable/wedged daemon fails loud instead of hanging the gate;
    # buf runs in seconds against a warm daemon, plus a dep fetch on a cold cache.
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)  # noqa: S603
    return result.stdout, result.stderr, result.returncode


def _findings_by_proto(stdout: str) -> tuple[dict[str, list[str]], list[str]]:
    """Group buf's JSON findings by the proto they belong to.

    Returns ``(findings, build_failures)``. A `COMPILE` finding means the module did
    not build — a tool failure, not an incompatible change — and buf reports the two
    identically, so they are separated here rather than read as the same verdict. A
    line that is not JSON at all joins the build failures.
    """
    grouped: dict[str, list[str]] = {}
    build: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            finding = json.loads(line)
        except json.JSONDecodeError:
            build.append(line)
            continue
        path = finding.get('path', '')
        rel = path[len(_FINDING_PREFIX) :] if path.startswith(_FINDING_PREFIX) else path or _MODULE_SCOPE
        where = f'{finding.get("start_line", "?")}:{finding.get("start_column", "?")}'
        message = finding.get('message', line)
        if finding.get('type') == 'COMPILE':
            build.append(f'{rel}:{where}: {message}')
        else:
            grouped.setdefault(rel, []).append(f'{where}: {message}')
    return grouped, build


class _Outcome(NamedTuple):
    """What a buf run means: the lines to log, and the gate's failure (``None`` passes).

    One classification yields both, so the log cannot call a contract
    backward-compatible while the verdict says the module never built.
    """

    lines: list[str]
    failure: str | None


def _outcome(stdout: str, stderr: str, returncode: int, baseline_ref: str) -> _Outcome:
    """Classify a ``buf breaking`` run, in precedence order.

    The module failed to build, so nothing was compared and no contract has a verdict;
    or buf reported nothing to explain a non-zero exit, so the tool itself failed; or
    buf's findings stand. Every contract in the compared module is gated, so a finding
    carries the verdict and the exit code is only ever corroboration.
    """
    findings, build_failures = _findings_by_proto(stdout)
    if build_failures:
        return _Outcome(
            ['::error::compat: the proto module failed to build; nothing was compared:']
            + [f'  {failure}' for failure in build_failures],
            'compat gate: the proto module failed to build',
        )
    if not findings:
        if returncode != 0:
            return _Outcome([stdout, stderr], f'compat gate: buf exited {returncode} without reporting anything')
        return _Outcome([], None)

    lines: list[str] = []
    for proto_rel, changes in sorted(findings.items()):
        lines.append(f'::error::compat {proto_rel}: breaking change(s) vs {baseline_ref}:')
        lines += [f'  {change}' for change in changes]
    return _Outcome(
        lines, f'compat gate: {len(findings)} incompatible proto contract(s) — additive-only evolution, no override'
    )


def compare(scratch: pathlib.Path, baseline_ref: str) -> _Outcome:
    """Diff the ``new`` module under ``scratch`` against the ``base`` one beside it.

    Pre-release contracts are held out of both sides first: buf reports a deletion or
    rename against the module carrying no path, so the carve-out is only expressible
    on the input — a filter over findings has nothing to match such a finding on.
    """
    for side in ('new', 'base'):
        for relpath in _PRE_RELEASE:
            # missing_ok: a side legitimately lacks the path when the contract is newer
            # than the baseline, or was renamed or deleted and its old path still listed
            (scratch / side / _PROTO_TREE / relpath).unlink(missing_ok=True)
    stdout, stderr, returncode = _run_buf(scratch)
    return _outcome(stdout, stderr, returncode, baseline_ref)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--baseline-ref',
        required=True,
        help='git ref holding the released proto contracts to diff against (e.g. origin/main).',
    )
    args = parser.parse_args()

    if shutil.which('docker') is None:
        raise SystemExit('docker not found; the buf breaking gate runs via a pinned docker run')

    baseline.require_ref(args.baseline_ref)

    protos = sorted(_PROTO_DIR.rglob('*.proto'))
    if not protos:
        raise SystemExit(f'no committed protos under {_PROTO_DIR}')
    compared = [path for path in protos if str(path.relative_to(_PROTO_DIR)) not in _PRE_RELEASE]
    if not compared:
        raise SystemExit('every committed proto is pre-release; the gate would pass without comparing anything')

    with tempfile.TemporaryDirectory(dir=_REPO_ROOT, prefix='.buf-') as tmp:
        scratch = pathlib.Path(tmp)
        _materialise(scratch / 'new', None)
        _materialise(scratch / 'base', args.baseline_ref)
        outcome = compare(scratch, args.baseline_ref)

    for line in outcome.lines:
        print(line)
    if outcome.failure:
        sys.stdout.flush()  # stderr is unbuffered, so without this the raise outruns the log
        raise SystemExit(outcome.failure)
    print(f'compat gate: {len(compared)} proto(s) vs {args.baseline_ref}: no incompatibility')
    return 0


if __name__ == '__main__':
    sys.exit(main())
