"""Backward-compatibility gate for the committed JSON Schema artifacts (S0.6).

Diffs each committed ``jsonschema/<domain>.schema.json`` against its baseline
with ``chuckd`` in ``BACKWARD`` mode and fails on any incompatible delta —
additive-only evolution, no override (``docs/design/typespec.md`` "Schema
evolution").

**Baseline.** The design says "last released version"; there is no release
process yet, so the Stage-0 stand-in is the schema on the PR base branch (``main``
is the released line under additive-only evolution). Pass it explicitly as
``--baseline-ref`` (CI supplies ``origin/<base>``; locally e.g. ``main``).

**Why per-type, not the bundle.** ``chuckd`` (Confluent's JSON Schema diff)
traverses from a schema's root, but the committed bundle's root is a ``$defs``
container that references nothing — diffing the bundle as-is compares two empty
roots and finds every change compatible. So each ``$def`` is promoted to a root
schema (its body + an absolute ``$id`` + the full ``$defs`` retained so
``#/$defs`` refs still resolve) and diffed on its own; the verdict is the union
over types. Only types whose body changed are diffed — a breaking change lives
in exactly the type whose body changed (a referenced type's change surfaces in
that type's own diff), so unchanged types need no run.

**The one downgrade.** ``PROPERTY_ADDED_TO_OPEN_CONTENT_MODEL`` becomes a
warning: ``chuckd`` emits it only for open (wire) content models, where a
lenient reader tolerates an added field — an invariant, not a judgment. Every
other finding fails hard.

``chuckd`` is a JVM tool shipped only as a Docker image; this shells out to a
pinned ``docker run``. Runs in CI (Docker present on the runner); locally needs
Docker and a baseline ref:
``uv run python -m tools.schema.compat --baseline-ref main``.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import NamedTuple

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_JSONSCHEMA_DIR = _REPO_ROOT / 'schema' / 'tests' / 'jsonschema'

# chuckd 0.6.0, multi-arch index digest (resolves per-platform on the runner).
# JVM tool, Docker-only (no PyPI); pinned by digest, not a moving tag.
_CHUCKD_IMAGE = 'anentropic/chuckd@sha256:7d4aef1d27d3f6ebd1840118bb57bbae911c544bcfd1f1f452d84482e204ad4c'

_DRAFT_2020_12 = 'https://json-schema.org/draft/2020-12/schema'
# Absolute base for each per-type $id: jsonsKema (chuckd's loader) rejects a
# relative $id, and the bundle's own $id is relative. `.invalid` is reserved and
# never dereferenced — all refs are local `#/$defs/...`.
_TYPE_ID_BASE = 'https://themis.invalid/'

# The sole finding downgraded to a warning (see module docstring).
_OPEN_CONTENT_ADDED = 'PROPERTY_ADDED_TO_OPEN_CONTENT_MODEL'

# chuckd prints one `{errorType:"X", description:"..."}` block per incompatibility
# (the description is not valid JSON — single-quoted, unterminated — so match the
# type only and keep the whole line as the human-readable detail).
_ERROR_TYPE_RE = re.compile(r'errorType:"([A-Z_]+)"')


class Finding(NamedTuple):
    type_name: str  # the $def the incompatibility was found in
    error_type: str  # chuckd's errorType, e.g. COMBINED_TYPE_SUBSCHEMAS_CHANGED
    detail: str  # chuckd's full report line, for the operator message


def extract_types(bundle: dict) -> dict[str, dict]:
    """Promote each ``$def`` to a standalone root schema for per-type diffing.

    Each returned schema is the type's body with ``$schema`` and an absolute
    ``$id`` added and the bundle's whole ``$defs`` retained, so its ``#/$defs``
    refs resolve when ``chuckd`` loads it as a root.
    """
    defs = bundle['$defs']
    return {
        name: {**body, '$schema': _DRAFT_2020_12, '$id': _TYPE_ID_BASE + name, '$defs': dict(defs)}
        for name, body in defs.items()
    }


def parse_findings(output: str, type_name: str) -> list[Finding]:
    """Parse ``chuckd``'s report for one type into structured findings."""
    findings = []
    for line in output.splitlines():
        match = _ERROR_TYPE_RE.search(line)
        if match:
            findings.append(Finding(type_name=type_name, error_type=match.group(1), detail=line.strip()))
    return findings


def classify(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Split findings into ``(hard_failures, downgraded_to_warning)``."""
    hard = [f for f in findings if f.error_type != _OPEN_CONTENT_ADDED]
    soft = [f for f in findings if f.error_type == _OPEN_CONTENT_ADDED]
    return hard, soft


def _run_chuckd(new_schema: dict, baseline_schema: dict) -> tuple[str, int]:
    """Diff one (new, baseline) type pair with ``chuckd`` BACKWARD.

    Returns ``(combined_output, returncode)``. New schema is left-most per
    ``chuckd``'s CLI (``new`` then older versions).
    """
    with tempfile.TemporaryDirectory() as tmp:
        scratch = pathlib.Path(tmp)
        (scratch / 'new.json').write_text(json.dumps(new_schema))
        (scratch / 'old.json').write_text(json.dumps(baseline_schema))
        cmd = [
            'docker',
            'run',
            '--rm',
            '-v',
            f'{scratch}:/schemas',
            _CHUCKD_IMAGE,
            '-c',
            'BACKWARD',
            '-f',
            'JSONSCHEMA',
            'new.json',
            'old.json',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    return result.stdout + result.stderr, result.returncode


def diff_bundles(new_bundle: dict, baseline_bundle: dict) -> tuple[list[Finding], list[Finding]]:
    """Diff every changed type between two bundles; return ``(hard, soft)`` findings.

    Types present in both with an identical body are skipped (nothing to diff).
    Types only in ``new_bundle`` are additive and skipped. Types only in
    ``baseline_bundle`` were removed (or renamed) — breaking under additive-only
    evolution, and invisible to ``chuckd`` (which diffs per surviving type), so
    flagged here as a hard finding. A non-zero ``chuckd`` exit with no parsed
    finding is ``chuckd`` itself failing — raised, never read as compatible.
    """
    new_types = extract_types(new_bundle)
    base_types = extract_types(baseline_bundle)
    new_defs = new_bundle['$defs']
    base_defs = baseline_bundle['$defs']

    hard: list[Finding] = []
    soft: list[Finding] = []
    for name in sorted(new_types.keys() & base_types.keys()):
        if new_defs[name] == base_defs[name]:
            continue
        output, returncode = _run_chuckd(new_types[name], base_types[name])
        findings = parse_findings(output, name)
        if not findings and returncode != 0:
            raise RuntimeError(f'chuckd failed on type {name!r} (exit {returncode}):\n{output}')
        type_hard, type_soft = classify(findings)
        hard.extend(type_hard)
        soft.extend(type_soft)
    for removed in sorted(base_types.keys() - new_types.keys()):
        detail = f'type {removed!r} present in the baseline but removed from the bundle'
        hard.append(Finding(removed, 'TYPE_REMOVED', detail))
    return hard, soft


def _require_ref(ref: str) -> None:
    """Fail loud unless ``ref`` resolves to a commit.

    Separates a genuinely-new domain (path absent at a *valid* baseline, a correct
    skip in ``_git_show``) from an unresolvable baseline ref (typo, unfetched
    branch, detached state) — an operational error that must not degrade to a
    silent green pass.
    """
    result = subprocess.run(  # noqa: S603
        ['git', 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}'],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f'baseline ref {ref!r} does not resolve to a commit (unfetched branch, typo, or detached?)')


def _git_show(ref: str, repo_rel_path: str) -> str | None:
    """Return the file's content at ``<ref>:<path>``, or ``None`` if absent there.

    Assumes ``ref`` is already known to resolve (see ``_require_ref``), so a
    non-zero exit means the path doesn't exist at that ref — a brand-new domain
    with nothing to be incompatible with.
    """
    result = subprocess.run(  # noqa: S603
        ['git', 'show', f'{ref}:{repo_rel_path}'],  # noqa: S607
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--baseline-ref',
        required=True,
        help='git ref holding the released schemas to diff against (e.g. origin/main).',
    )
    args = parser.parse_args()

    if shutil.which('docker') is None:
        raise SystemExit('docker not found; chuckd is shipped only as a Docker image')

    _require_ref(args.baseline_ref)

    schemas = sorted(_JSONSCHEMA_DIR.glob('*.schema.json'))
    if not schemas:
        raise SystemExit(f'no committed schemas under {_JSONSCHEMA_DIR}')

    hard_total = 0
    for path in schemas:
        repo_rel = str(path.relative_to(_REPO_ROOT))
        baseline_text = _git_show(args.baseline_ref, repo_rel)
        if baseline_text is None:
            print(f'compat: {path.name}: not present at {args.baseline_ref}; no baseline, skipping')
            continue
        hard, soft = diff_bundles(json.loads(path.read_text()), json.loads(baseline_text))
        for finding in soft:
            print(
                f'::warning::compat {path.name} [{finding.type_name}]: '
                f'tolerated on open content model: {finding.detail}'
            )
        for finding in hard:
            print(f'::error::compat {path.name} [{finding.type_name}]: {finding.detail}')
        hard_total += len(hard)

    if hard_total:
        raise SystemExit(f'compat gate: {hard_total} incompatible change(s) — additive-only evolution, no override')
    print('compat gate: every committed schema is backward-compatible with its baseline')
    return 0


if __name__ == '__main__':
    sys.exit(main())
