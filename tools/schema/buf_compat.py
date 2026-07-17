"""Backward-compatibility gate for the committed gRPC proto contracts (S0.6).

The **sole** authored-data compat gate (proto.md): diffs each committed
``schema/proto/**/<domain>.proto`` against its baseline with ``buf breaking``
(FILE category — field renumber/removal, type/label changes, renames), failing on
any incompatible delta — additive-only evolution, no override
(``docs/design/proto.md`` "Schema evolution"). Gates every committed proto — RPC and at-rest
alike; a pre-release contract (no persisted data, no deployed consumer) is skipped until it
stabilizes (see ``_PRE_RELEASE``).

**Baseline.** The released line, stood in by the PR base branch (``main`` under
additive-only evolution). Pass it explicitly as ``--baseline-ref`` (CI supplies
``origin/<base>``; locally e.g. ``main``).

``buf breaking`` runs from a pinned ``docker run``. Runs in CI (Docker present on
the runner); locally needs Docker and a baseline ref:
``uv run python -m tools.schema.buf_compat --baseline-ref main``.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile

from tools.schema import baseline

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PROTO_DIR = _REPO_ROOT / 'schema' / 'proto'

# Pre-release contracts, excluded from the breaking gate: no persisted data and no deployed
# consumer, so a one-time reshape is intended. Each rejoins the gate once released. Paths are
# relative to _PROTO_DIR.
_PRE_RELEASE = frozenset({'themis/litcache/models/litcache.proto'})

# buf breaking runner, pinned by digest (not a moving tag).
_BUF_IMAGE = 'bufbuild/buf@sha256:c34c81ac26044490a10fb5009eb618640834b9048f38d4717538421c6a25e4d7'


def _run_buf(new_text: str, baseline_text: str, relpath: str) -> tuple[str, int]:
    """Diff a committed ``.proto`` against its baseline with ``buf breaking``.

    ``buf`` compares modules, so each side is materialised as a one-file module (the
    proto at ``relpath`` + a minimal ``buf.yaml``) and diffed under the ``FILE``
    category — the strictest (field renumber/removal, type/label changes, renames).
    Returns ``(output, returncode)``; a non-zero return is a breaking change. Scratch
    sits under the repo so the Docker host mount reaches it (Colima/Lima mount $HOME,
    not /tmp).
    """
    buf_yaml = 'version: v2\nbreaking:\n  use:\n    - FILE\n'
    proto_rel = pathlib.Path(relpath)
    with tempfile.TemporaryDirectory(dir=_REPO_ROOT, prefix='.buf-') as tmp:
        scratch = pathlib.Path(tmp)
        for side, text in (('new', new_text), ('base', baseline_text)):
            module = scratch / side
            (module / proto_rel.parent).mkdir(parents=True, exist_ok=True)
            (module / proto_rel).write_text(text)
            (module / 'buf.yaml').write_text(buf_yaml)
        cmd = [
            'docker',
            'run',
            '--rm',
            '-v',
            f'{scratch}:/work',
            '-w',
            '/work',
            _BUF_IMAGE,
            'breaking',
            'new',
            '--against',
            'base',
        ]
        # timeout so an unreachable/wedged daemon fails loud instead of hanging
        # the gate; buf runs in seconds against a warm daemon.
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=120)  # noqa: S603
    return result.stdout + result.stderr, result.returncode


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

    hard_total = 0
    for path in protos:
        proto_rel = str(path.relative_to(_PROTO_DIR))
        if proto_rel in _PRE_RELEASE:
            print(f'compat: {path.name}: pre-release, not gated')
            continue
        repo_rel = str(path.relative_to(_REPO_ROOT))
        baseline_text = baseline.show_at_ref(args.baseline_ref, repo_rel)
        if baseline_text is None:
            print(f'compat: {path.name}: not present at {args.baseline_ref}; no baseline, skipping')
            continue
        output, returncode = _run_buf(path.read_text(), baseline_text, proto_rel)
        # buf yields no machine-parsed findings to split tool-failure from
        # incompatibility, so any non-zero counts as breaking (fail-safe — a crash
        # blocks the gate, never passes it).
        if returncode != 0:
            print(f'::error::compat {path.name}: breaking change(s) vs {args.baseline_ref}:\n{output}')
            hard_total += 1
        else:
            print(f'compat: {path.name}: backward-compatible')

    if hard_total:
        raise SystemExit(
            f'compat gate: {hard_total} incompatible proto change(s) — additive-only evolution, no override'
        )
    print('compat gate: every committed proto is backward-compatible with its baseline')
    return 0


if __name__ == '__main__':
    sys.exit(main())
