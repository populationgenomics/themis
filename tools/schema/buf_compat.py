"""Backward-compatibility gate for the committed gRPC proto contracts (S0.6).

The **sole** authored-data compat gate (proto.md): diffs the committed proto module
under ``schema/proto`` against its baseline with ``buf breaking``, under the rule set
``buf.yaml`` declares (renumbers, unreserved removals, type/label changes, renames),
failing on any incompatible delta, with no override. Evolution is additive, plus
retiring a field whose number *and* name are reserved (``docs/design/proto.md``
"Schema evolution"). Retiring an RPC, or a message in a service tree, is outside
the gate — proto reserves neither name, so there is nothing in the schema to check
it against, and the type-checkers carry it instead. At-rest contracts keep
``MESSAGE_NO_DELETE``: no type-checker sees their last reader go.
Gates every committed proto — RPC and at-rest alike; a pre-release
contract (no persisted data, no deployed consumer) is left out of the compared module
until it stabilizes (see ``_PRE_RELEASE``), so it has no baseline to be incompatible
with.

**Baseline.** The released line, stood in by the PR base branch. Pass it explicitly as
``--baseline-ref`` (CI supplies ``origin/<base>``; locally e.g. ``main``).

Each side is materialised as the repo's *own* module — ``buf.yaml`` + ``buf.lock``
alongside the ``schema/proto`` tree — because a proto's imports only resolve against
the deps its module declares (``buf/validate/validate.proto`` comes from the pinned
``buf.build/bufbuild/protovalidate``). Imports are excluded from the comparison: a
dep bump is that dep's change, not ours.

**A user's judgements in a widget.** ``buf breaking`` reads no custom option, and the
ownership rule (``themis/widgets/ownership.py``) reads two off every payload field: which
fields are guards, a user's judgements, and what each ignores; and which field a list's
elements are matched by. A push hook built before a change reads them as they were, and
descends only into messages that held a guard when it was built. So the gate also builds
both sides and, for a released message, refuses a field that gains, loses or changes its
``guard`` beyond ignoring a field new with the change, a changed ``element_key``, a new
guard, a guard gained beneath a message that held none, and the ``widget`` mark gained
or lost: an older hook would leave each unchecked or check it differently. A new
message is free to carry any of them.

``buf`` runs from a pinned ``docker run``, which needs network to fetch the
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
from collections.abc import Iterator, Mapping
from typing import NamedTuple

from google.protobuf import descriptor, descriptor_pb2, descriptor_pool

from themis.widgets import asset, ownership
from themis.widgets.models import widget_pb2
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
# no deployed consumer, so a one-time reshape is intended. Each rejoins the gate once released —
# except the copied upstream schema, whose field numbers are positional, so a pin bump rewrites it
# wholesale for as long as we carry it. Paths are relative to _PROTO_DIR, and a renamed or deleted
# contract keeps its old path listed until the baseline no longer carries it — a path here is never
# compared, present or not. A listed path is unlinked from both sides, so nothing a *gated* proto
# imports may be listed: the import would not resolve and the module would fail to build.
_PRE_RELEASE = frozenset(
    {
        'clinvar_proto/clinvar.proto',
        'themis/litcache/models/litcache.proto',
        'themis/litcache/models/crossref.proto',
        'themis/litcache/models/openalex.proto',
        'themis/rpc/clinvar.proto',
        'themis/rpc/cspec.proto',
        'themis/rpc/gene_disease.proto',
        'themis/rpc/gnomad.proto',
        'themis/rpc/mavedb.proto',
        'themis/rpc/splice.proto',
        'themis/rpc/transcript.proto',
        'themis/rpc/variant.proto',
        'themis/rpc/vep.proto',
    }
)

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


def _buf(scratch: pathlib.Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run the pinned ``buf`` over ``scratch``, mounted as its working directory.

    Scratch sits under the repo so the Docker host mount reaches it (Colima/Lima mount
    $HOME, not /tmp).
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
    # timeout so an unreachable/wedged daemon fails loud instead of hanging the gate;
    # buf runs in seconds against a warm daemon, plus a dep fetch on a cold cache.
    return subprocess.run([*cmd, _BUF_IMAGE, *args], capture_output=True, check=False, timeout=300)  # noqa: S603


def _run_buf(scratch: pathlib.Path) -> tuple[str, str, int]:
    """Diff the materialised module against its baseline with ``buf breaking``.

    Returns ``(stdout, stderr, returncode)``. buf writes every finding — incompatibility
    and build error alike — as JSON on stdout; stderr carries docker's own noise, such as
    the image pull on a cold cache, so the two are never folded together.
    """
    result = _buf(scratch, 'breaking', 'new', '--against', 'base', '--exclude-imports', '--error-format', 'json')
    return result.stdout.decode(), result.stderr.decode(), result.returncode


def _descriptor_set(scratch: pathlib.Path, side: str) -> descriptor_pb2.FileDescriptorSet:
    """The side's module as buf builds it: every file, imports included, each after the files it imports.

    Raises:
        RuntimeError: If buf cannot build it, which a module ``buf breaking`` has just built does not do.
    """
    result = _buf(scratch, 'build', side, '--as-file-descriptor-set', '-o', '-#format=binpb')
    if result.returncode != 0:
        raise RuntimeError(f'buf build {side} failed: {result.stderr.decode(errors="replace")}')
    files = descriptor_pb2.FileDescriptorSet()
    files.ParseFromString(result.stdout)
    return files


def _pool(files: descriptor_pb2.FileDescriptorSet) -> descriptor_pool.DescriptorPool:
    pool = descriptor_pool.DescriptorPool()
    for file in files.file:
        pool.Add(file)
    return pool


def _messages(desc: descriptor.FileDescriptor | descriptor.Descriptor) -> Iterator[descriptor.Descriptor]:
    """Every message `desc` declares, nested ones included."""
    declared = desc.message_types_by_name if isinstance(desc, descriptor.FileDescriptor) else desc.nested_types_by_name
    for message in declared.values():
        yield message
        yield from _messages(message)


def _ignored(message: descriptor.Descriptor, guard: widget_pb2.Guard) -> dict[int, str]:
    """The fields of `message` that `guard` ignores, by number: a renamed field is the same field."""
    return {message.fields_by_name[name].number: name for name in guard.ignores if name in message.fields_by_name}


def _rest_of(guard: widget_pb2.Guard) -> bytes:
    """`guard` without what it ignores: every other part of the option, as the wire carries it."""
    rest = widget_pb2.Guard()
    rest.CopyFrom(guard)
    rest.ClearField('ignores')
    return rest.SerializeToString(deterministic=True)


def _field_changes(
    message: descriptor.Descriptor,
    field: descriptor.FieldDescriptor,
    later_message: descriptor.Descriptor,
    later: descriptor.FieldDescriptor,
) -> list[str]:
    """What `later` changes of released `field`'s guard and element key; `message` is the released message."""
    where = f'Field "{field.number}" with name "{later.name}" on message "{message.full_name}"'
    found = []
    before_guard, later_guard = ownership.guard_of(field), ownership.guard_of(later)
    if before_guard is None and later_guard is not None:
        found.append(f'{where} became a guard; a push hook built before the change still reads it as content.')
    elif before_guard is not None and later_guard is None:
        found.append(f'{where} is no longer a guard; a push hook built before the change still reads it as one.')
    elif before_guard is not None and later_guard is not None:
        before_ignored, later_ignored = _ignored(message, before_guard), _ignored(later_message, later_guard)
        dropped = [name for number, name in before_ignored.items() if number not in later_ignored]
        gained = [
            name
            for number, name in later_ignored.items()
            if number not in before_ignored and number in message.fields_by_number
        ]
        if dropped:
            found.append(
                f'{where} no longer ignores {", ".join(dropped)}; a push hook built before the change lets the agent '
                'change it and keep the judgement.'
            )
        if gained:
            found.append(
                f'{where} now ignores {", ".join(gained)}, released before it; a push hook built before the change '
                'still clears the judgement when it changes.'
            )
        if _rest_of(before_guard) != _rest_of(later_guard):
            found.append(
                f'{where} changed its guard option beyond what it ignores; a push hook built before the change still '
                'reads the option as it was.'
            )
    if ownership.is_element_key(field) != ownership.is_element_key(later):
        found.append(
            f'{where} changed whether it is the element_key; a push hook built before the change still matches '
            'elements by the old key.'
        )
    return found


def ownership_changes(
    base: descriptor_pb2.FileDescriptorSet, new: descriptor_pb2.FileDescriptorSet
) -> dict[str, list[str]]:
    """What `new` changes of the ownership of released messages, grouped by the proto declaring each in `new`.

    A message or field `new` no longer holds is ``buf breaking``'s to judge, and one `base` does not hold is free.
    """
    base_pool, new_pool = _pool(base), _pool(new)
    changes: dict[str, list[str]] = {}
    for file in base.file:
        for before in _messages(base_pool.FindFileByName(file.name)):
            try:
                after = new_pool.FindMessageTypeByName(before.full_name)
            except KeyError:
                continue
            found = changes.setdefault(after.file.name, [])
            for field in before.fields:
                later = after.fields_by_number.get(field.number)
                if later is not None:
                    found.extend(_field_changes(before, field, after, later))
            found.extend(
                f'Message "{after.full_name}" gained the guard {later.name}; a push hook built before the change reads '
                'it as a field it does not know.'
                for later in after.fields
                if later.number not in before.fields_by_number and ownership.is_guard(later)
            )
            if asset.is_payload_type(before) != asset.is_payload_type(after):
                found.append(
                    f'Message "{after.full_name}" '
                    + (
                        'became a widget payload; a push hook built before the change holds it unmarked in the widgets '
                        'package, and refuses every push writing one of its assets.'
                        if asset.is_payload_type(after)
                        else 'is no longer a widget payload; a push hook built before the change still checks its '
                        'assets as one.'
                    )
                )
            if not ownership.holds_guards(before) and ownership.holds_guards(after):
                found.append(
                    f'Message "{after.full_name}" held no guard, directly or beneath it, and now holds one; a push '
                    'hook built before the change does not compare it.'
                )
    return {proto: found for proto, found in sorted(changes.items()) if found}


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
        lines,
        f'compat gate: {len(findings)} incompatible proto contract(s) — additive evolution; a '
        'retired field must reserve its number and name; no override',
    )


def _with_ownership(outcome: _Outcome, changes: Mapping[str, list[str]], baseline_ref: str) -> _Outcome:
    """`outcome`, with each ownership change after buf's findings and in the verdict."""
    if not changes:
        return outcome
    lines = list(outcome.lines)
    for proto_rel, found in changes.items():
        lines.append(f'::error::compat {proto_rel}: ownership change(s) vs {baseline_ref}:')
        lines += [f'  {change}' for change in found]
    failure = (
        f"compat gate: {len(changes)} proto contract(s) change a released widget message's judgements — a released "
        'field keeps its guard and element_key, a guard ignores only fields new with the change, and a released '
        'message gains no guard and keeps its widget mark'
    )
    return _Outcome(lines, failure if outcome.failure is None else f'{outcome.failure}; {failure}')


def compare(scratch: pathlib.Path, baseline_ref: str) -> _Outcome:
    """Diff the ``new`` module under ``scratch`` against the ``base`` one beside it.

    Pre-release contracts are held out of both sides first: buf reports a deletion or
    rename against the module carrying no path, so the carve-out is only expressible
    on the input — a filter over findings has nothing to match such a finding on. The
    ownership rule runs over what buf compared, and not at all when it compared nothing.

    Raises:
        RuntimeError: If buf compared the module and then could not build it.
    """
    for side in ('new', 'base'):
        for relpath in _PRE_RELEASE:
            # missing_ok: a side legitimately lacks the path when the contract is newer
            # than the baseline, or was renamed or deleted and its old path still listed
            (scratch / side / _PROTO_TREE / relpath).unlink(missing_ok=True)
    stdout, stderr, returncode = _run_buf(scratch)
    outcome = _outcome(stdout, stderr, returncode, baseline_ref)
    findings, build_failures = _findings_by_proto(stdout)
    if build_failures or (returncode != 0 and not findings):
        return outcome
    changes = ownership_changes(_descriptor_set(scratch, 'base'), _descriptor_set(scratch, 'new'))
    return _with_ownership(outcome, changes, baseline_ref)


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
