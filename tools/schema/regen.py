"""Regenerate the committed code-generation artifacts from the TypeSpec sources.

Discovers every domain (a directory holding a ``main.tsp`` entry point) and
compiles each with ``@typespec/json-schema``, emitting one bundled
``<domain>.schema.json`` per domain — all types under ``$defs`` in a single
2020-12 file.

In Stage 0 the only sources are the synthetic feature-coverage corpus under
``schema/tests/fixtures/``; their schemas land in ``schema/tests/jsonschema/``.
Real domains (Stage 1+, e.g. ``schema/litcache/``) get added as further roots
when they land.

Run with ``uv run python -m tools.schema.regen``. The Node toolchain under
``schema/`` must be installed (``npm --prefix schema ci``); this fails loudly if
the ``tsp`` binary is missing.

This is the pure-generation step. Verification (metaschema validity, backward
compatibility) lives in tests and CI gates, not here. Later stages extend the
pipeline (normalize -> Pydantic, direct Zod); for now it stops at JSON Schema.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _REPO_ROOT / 'schema'
_TSP_BIN = _SCHEMA_DIR / 'node_modules' / '.bin' / 'tsp'
_TSP_CONFIG = _SCHEMA_DIR / 'tspconfig.yaml'

# Stage-0 corpus: source domains and the matching committed schemas. Both sit
# under schema/ so the toolchain's node_modules stays an ancestor of every
# .tsp source (TypeSpec resolves emitters by walking up from the source file).
_CORPUS_DIR = _SCHEMA_DIR / 'tests' / 'fixtures'
_OUTPUT_DIR = _SCHEMA_DIR / 'tests' / 'jsonschema'

_JSON_SCHEMA_EMITTER = '@typespec/json-schema'


def _discover_domains() -> list[str]:
    """Return the names of every domain directory holding a ``main.tsp``."""
    domains = sorted(path.parent.name for path in _CORPUS_DIR.glob('*/main.tsp'))
    if not domains:
        raise SystemExit(f'no domains found under {_CORPUS_DIR} (expected at least one <domain>/main.tsp)')
    return domains


def _compile_domain(domain: str) -> None:
    """Compile one domain's ``main.tsp`` to its bundled JSON Schema."""
    bundle_name = f'{domain}.schema.json'
    entrypoint = _CORPUS_DIR.relative_to(_SCHEMA_DIR) / domain / 'main.tsp'
    cmd = [
        str(_TSP_BIN),
        'compile',
        str(entrypoint),
        '--config',
        str(_TSP_CONFIG),
        '--option',
        f'{_JSON_SCHEMA_EMITTER}.bundleId={bundle_name}',
        '--option',
        f'{_JSON_SCHEMA_EMITTER}.emitter-output-dir={_OUTPUT_DIR}',
    ]
    # cwd is schema/ so the local node_modules resolves.
    subprocess.run(cmd, cwd=_SCHEMA_DIR, check=True)  # noqa: S603

    # The emitter writes no trailing newline; add one so the committed bytes
    # match what end-of-file-fixer enforces and the freshness gate stays green.
    out_path = _OUTPUT_DIR / bundle_name
    text = out_path.read_text()
    if not text.endswith('\n'):
        out_path.write_text(text + '\n')


def main() -> int:
    if not _TSP_BIN.exists():
        raise SystemExit(f'tsp binary not found at {_TSP_BIN}; run `npm --prefix schema ci` first')

    domains = _discover_domains()
    for domain in domains:
        print(f'schema/regen: compiling {domain} -> {_OUTPUT_DIR.name}/{domain}.schema.json')
        _compile_domain(domain)

    print(f'schema/regen: emitted {len(domains)} schema(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
