"""Regenerate the committed code-generation artifacts from the TypeSpec sources.

Discovers every domain (a directory holding a ``main.tsp`` entry point) and runs
each through the pipeline:

1. **compile** with ``@typespec/json-schema`` to one bundled 2020-12 file, all
   types under ``$defs``;
2. **normalize** that bundle into the local-ref single-file form (see
   ``tools.schema.normalize`` — a #4084 workaround) — this is the committed
   ``<domain>.schema.json``;
3. **Pydantic** v2 models from the normalized schema via
   ``datamodel-code-generator``.

In Stage 0 the only sources are the synthetic feature-coverage corpus under
``schema/tests/fixtures/``; their schemas land in ``schema/tests/jsonschema/`` and
their models in ``schema/tests/pydantic/``. Real domains (Stage 1+, e.g.
``schema/litcache/``) get added as further roots when they land.

Run with ``uv run --group codegen python -m tools.schema.regen``. The Node
toolchain under ``schema/`` must be installed (``npm --prefix schema ci``) and
``datamodel-code-generator`` available (the ``codegen`` uv group); this fails
loudly if either is missing.

This is the pure-generation step. Verification (metaschema validity, model
import, backward compatibility) lives in tests and CI gates, not here. The direct
Zod emitter is the next slice (S0.3).
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

from tools.schema import normalize

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _REPO_ROOT / 'schema'
_TSP_BIN = _SCHEMA_DIR / 'node_modules' / '.bin' / 'tsp'
_TSP_CONFIG = _SCHEMA_DIR / 'tspconfig.yaml'

# Stage-0 corpus: source domains and the matching committed artifacts. All sit
# under schema/ so the toolchain's node_modules stays an ancestor of every
# .tsp source (TypeSpec resolves emitters by walking up from the source file).
_CORPUS_DIR = _SCHEMA_DIR / 'tests' / 'fixtures'
_OUTPUT_DIR = _SCHEMA_DIR / 'tests' / 'jsonschema'
_PYDANTIC_DIR = _SCHEMA_DIR / 'tests' / 'pydantic'

_JSON_SCHEMA_EMITTER = '@typespec/json-schema'


def _discover_domains() -> list[str]:
    """Return the names of every domain directory holding a ``main.tsp``."""
    domains = sorted(path.parent.name for path in _CORPUS_DIR.glob('*/main.tsp'))
    if not domains:
        raise SystemExit(f'no domains found under {_CORPUS_DIR} (expected at least one <domain>/main.tsp)')
    return domains


def _emit_schema(domain: str) -> pathlib.Path:
    """Compile a domain's ``main.tsp`` to its normalized, committed JSON Schema.

    Returns the path of the written ``<domain>.schema.json``.
    """
    bundle_name = f'{domain}.schema.json'
    out_path = _OUTPUT_DIR / bundle_name
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

    schema = normalize.normalize(json.loads(out_path.read_text()))
    # Trailing newline so the committed bytes match end-of-file-fixer and the
    # freshness gate (S0.4) stays green.
    out_path.write_text(json.dumps(schema, indent=4) + '\n')
    return out_path


def _emit_pydantic(domain: str, schema_path: pathlib.Path) -> None:
    """Generate Pydantic v2 models for a domain from its normalized JSON Schema."""
    model_path = _PYDANTIC_DIR / f'{domain}.py'
    cmd = [
        'datamodel-codegen',
        '--input',
        str(schema_path),
        '--input-file-type',
        'jsonschema',
        '--output',
        str(model_path),
        '--output-model-type',
        'pydantic_v2.BaseModel',
        '--target-python-version',
        '3.13',
        '--use-standard-collections',  # list[X], not typing.List[X]
        '--use-union-operator',  # X | None, not Optional[X]
        '--use-specialized-enum',  # StrEnum / IntEnum, not bare Enum
        # Don't emit a model for the root schema element. Our bundle root is always
        # a $defs-only container (S0.1) with no type, so datamodel-codegen would
        # otherwise emit a meaningless `class Model(RootModel[Any])` for it. Real
        # types — including named-union RootModels — live under $defs, unaffected.
        '--skip-root-model',
        '--disable-timestamp',  # deterministic header for the freshness gate
        '--formatters',  # pin formatters: silences the opt-in-default warning
        'black',
        'isort',
    ]
    _PYDANTIC_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)  # noqa: S603


def main() -> int:
    if not _TSP_BIN.exists():
        raise SystemExit(f'tsp binary not found at {_TSP_BIN}; run `npm --prefix schema ci` first')
    if shutil.which('datamodel-codegen') is None:
        raise SystemExit('datamodel-codegen not found; run via `uv run --group codegen python -m tools.schema.regen`')

    domains = _discover_domains()
    for domain in domains:
        print(f'schema/regen: {domain} -> {_OUTPUT_DIR.name}/{domain}.schema.json + {_PYDANTIC_DIR.name}/{domain}.py')
        schema_path = _emit_schema(domain)
        _emit_pydantic(domain, schema_path)

    print(f'schema/regen: emitted {len(domains)} schema(s) + Pydantic models')
    return 0


if __name__ == '__main__':
    sys.exit(main())
