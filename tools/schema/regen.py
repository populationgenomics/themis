"""Regenerate the committed code-generation artifacts from the TypeSpec sources.

Two kinds of domain, discovered two ways:

**Registered domains** (``_DOMAINS``) — at-rest data and the feature corpus — each
carry their own committed-output locations and target set, so they are listed
explicitly. Each runs the pipeline:

1. **compile** with ``@typespec/json-schema`` to one bundled 2020-12 file, all
   types under ``$defs``;
2. **normalize** that bundle into the local-ref single-file form (see
   ``tools.schema.normalize`` — a #4084 workaround) — the committed
   ``<domain>.schema.json``; an ``at_rest`` domain is additionally sealed to a
   closed content model;
3. **Pydantic** v2 models from the normalized schema via
   ``datamodel-code-generator``;
4. **Zod** schemas direct from the ``.tsp`` via ``typespec-zod``, then
   topologically reordered (see ``tools.schema.zod_reorder`` — an ordering-bug
   workaround) — the committed ``<domain>.ts``; skipped for a domain with no wire
   consumer (``zod_out=None``).

Zod is emitted straight from the ``.tsp``, not via the JSON Schema: it is
frontend-wire-only and gains nothing from sharing the at-rest schema (see
``docs/design/typespec.md``). The synthetic feature-coverage corpus
(``schema/tests/fixtures/features/``) emits all three targets to verify every
construct round-trips and stays **open** (no consumer; wire-style); an
at-rest-only domain (litcache — GCS artifacts, no wire/frontend consumer) emits
JSON Schema + Pydantic only.

**gRPC service domains** (``_service_domains``) are glob-discovered — every
``schema/<name>/main.tsp`` not claimed by a registered domain — because a service
carries no custom output locations (the committed proto path is derived from its
``@package`` name). Each compiles with ``@typespec/protobuf`` to the committed
``schema/proto/themis/rpc/<name>.proto`` (the contract, the ``buf breaking``
baseline), then ``protoc`` (grpcio-tools) emits the committed
``themis/rpc/<name>_pb2.py`` + ``<name>_pb2_grpc.py`` stubs (messages + gRPC stub
+ servicer base; ``docs/design/services.md``).

Run with ``uv run --group codegen python -m tools.schema.regen``. The TypeSpec
toolchain under ``schema/`` must be installed with Bun (``bun install`` from
``schema/``; ``tsp`` runs on the Bun runtime) and ``datamodel-code-generator`` +
``grpcio-tools`` available (the ``codegen`` uv group); this fails loudly if any is
missing.

This is the pure-generation step. Verification (metaschema validity, model
import, ``tsc`` compile of the Zod, backward compatibility) lives in tests and
CI gates, not here.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

from tools.schema import normalize, zod_reorder

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _REPO_ROOT / 'schema'
_TSP_BIN = _SCHEMA_DIR / 'node_modules' / '.bin' / 'tsp'
_TSP_CONFIG = _SCHEMA_DIR / 'tspconfig.yaml'

_JSON_SCHEMA_EMITTER = '@typespec/json-schema'
_PROTOBUF_EMITTER = '@typespec/protobuf'
_ZOD_EMITTER = 'typespec-zod'

# gRPC service domains: the committed .proto lands under schema/proto/ (laid out
# by the proto package), the generated stubs under themis/rpc/.
_PROTO_DIR = _SCHEMA_DIR / 'proto'
_RPC_DIR = _REPO_ROOT / 'themis' / 'rpc'
_TESTS_DIR_NAME = 'tests'


@dataclasses.dataclass(frozen=True)
class Domain:
    """A registered schema domain and the committed artifacts it emits.

    Each domain sits under ``schema/`` so the toolchain's ``node_modules`` stays
    an ancestor of its ``.tsp`` sources (TypeSpec resolves emitters by walking up
    from the source file). Paths are absolute; outputs may live outside ``schema/``
    (a real domain's models go with their consumer).

    Attributes:
        name: The domain name; the bundle is ``<name>.schema.json``.
        main_tsp: The domain's ``main.tsp`` entry point.
        schema_out: The committed normalized JSON Schema.
        pydantic_out: The committed Pydantic v2 module.
        zod_out: The committed Zod module, or ``None`` to skip Zod (an at-rest
            domain with no wire consumer).
        at_rest: Seal the JSON Schema to a closed content model
            (``additionalProperties: false``); leave open (wire-style) if false.
    """

    name: str
    main_tsp: pathlib.Path
    schema_out: pathlib.Path
    pydantic_out: pathlib.Path
    zod_out: pathlib.Path | None
    at_rest: bool


@dataclasses.dataclass(frozen=True)
class ServiceDomain:
    """A gRPC service domain: a ``main.tsp`` emitting a proto contract + stubs.

    Unlike ``Domain`` these carry no custom output locations (the committed proto
    path is derived from the ``.tsp``'s ``@package`` name), so they are
    glob-discovered rather than registered — see ``_service_domains``.

    Attributes:
        name: The domain name (informational; the committed proto path follows the
            ``@package`` name in the ``.tsp``).
        main_tsp: The domain's ``main.tsp`` entry point.
    """

    name: str
    main_tsp: pathlib.Path


_DOMAINS: list[Domain] = [
    # Feature-coverage corpus: emits all three targets to verify every construct
    # round-trips; open (no consumer, wire-style) so its committed artifacts are
    # unaffected by the at-rest seal.
    Domain(
        name='features',
        main_tsp=_SCHEMA_DIR / 'tests' / 'fixtures' / 'features' / 'main.tsp',
        schema_out=_SCHEMA_DIR / 'tests' / 'jsonschema' / 'features.schema.json',
        pydantic_out=_SCHEMA_DIR / 'tests' / 'pydantic' / 'features.py',
        zod_out=_SCHEMA_DIR / 'tests' / 'zod' / 'features.ts',
        at_rest=False,
    ),
]


def _service_domains() -> list[ServiceDomain]:
    """Every ``schema/<name>/main.tsp`` not claimed by a registered ``Domain``.

    A service carries no per-domain output config, so it is discovered by glob
    rather than registered. A registered at-rest ``Domain`` (litcache) shares the
    ``schema/<name>/`` layout, so its ``main.tsp`` is excluded here — the two kinds
    are told apart by registration, not by path.
    """
    registered = {domain.main_tsp for domain in _DOMAINS}
    return [
        ServiceDomain(name=main_tsp.parent.name, main_tsp=main_tsp)
        for main_tsp in sorted(_SCHEMA_DIR.glob('*/main.tsp'))
        if main_tsp.parent.name != _TESTS_DIR_NAME and main_tsp not in registered
    ]


def _emit_schema(domain: Domain) -> None:
    """Compile a domain's ``main.tsp`` to its normalized, committed JSON Schema.

    Normalizes the #4084 ``$id``-relative refs; an at-rest domain is additionally
    sealed to a closed content model.
    """
    bundle_name = f'{domain.name}.schema.json'
    out_dir = domain.schema_out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    entrypoint = domain.main_tsp.relative_to(_SCHEMA_DIR)
    cmd = [
        'bun',
        str(_TSP_BIN),
        'compile',
        str(entrypoint),
        '--config',
        str(_TSP_CONFIG),
        '--option',
        f'{_JSON_SCHEMA_EMITTER}.bundleId={bundle_name}',
        '--option',
        f'{_JSON_SCHEMA_EMITTER}.emitter-output-dir={out_dir}',
    ]
    # cwd is schema/ so the local node_modules resolves.
    subprocess.run(cmd, cwd=_SCHEMA_DIR, check=True)  # noqa: S603

    schema = normalize.normalize(json.loads(domain.schema_out.read_text()))
    if domain.at_rest:
        schema = normalize.seal(schema)
    # Trailing newline so the committed bytes match end-of-file-fixer and the
    # freshness gate (S0.4) stays green.
    domain.schema_out.write_text(json.dumps(schema, indent=4) + '\n')


def _emit_pydantic(domain: Domain) -> None:
    """Generate Pydantic v2 models for a domain from its normalized JSON Schema."""
    model_path = domain.pydantic_out
    cmd = [
        'datamodel-codegen',
        '--input',
        str(domain.schema_out),
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
        '--field-constraints',  # int = Field(ge=…, le=…), not conint(…): a call in
        # type position that pyright (standard) rejects
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
    model_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)  # noqa: S603


def _emit_zod(domain: Domain) -> None:
    """Emit a domain's Zod schemas direct from its ``.tsp``, reordered.

    ``typespec-zod`` writes a fixed ``models.ts`` whose declarations are not
    dependency-ordered; emit to a temp dir, reorder (see
    ``tools.schema.zod_reorder``), and write the committed ``<domain>.ts``. A
    no-op for a domain with ``zod_out=None`` (no wire consumer).
    """
    if domain.zod_out is None:
        return
    entrypoint = domain.main_tsp.relative_to(_SCHEMA_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            'bun',
            str(_TSP_BIN),
            'compile',
            str(entrypoint),
            '--emit',
            _ZOD_EMITTER,
            '--option',
            f'{_ZOD_EMITTER}.emitter-output-dir={tmp}',
        ]
        # cwd is schema/ so the local node_modules resolves. No --config: that
        # would also run the json-schema emit; Zod is a separate compile.
        subprocess.run(cmd, cwd=_SCHEMA_DIR, check=True)  # noqa: S603
        emitted = pathlib.Path(tmp) / 'models.ts'
        reordered = zod_reorder.reorder(emitted.read_text())

    domain.zod_out.parent.mkdir(parents=True, exist_ok=True)
    domain.zod_out.write_text(reordered)


def _emit_proto(service: ServiceDomain) -> pathlib.Path:
    """Compile a service domain's ``main.tsp`` to its committed ``.proto`` (the contract).

    ``@typespec/protobuf`` writes ``<package-path>.proto`` (the ``@package`` name,
    e.g. ``themis.rpc.auth`` -> ``themis/rpc/auth.proto``); emit to a temp dir, then
    copy it under ``schema/proto/`` at the same relative path. Returns that path.
    """
    entrypoint = service.main_tsp.relative_to(_SCHEMA_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            'bun',
            str(_TSP_BIN),
            'compile',
            str(entrypoint),
            '--emit',
            _PROTOBUF_EMITTER,
            '--option',
            f'{_PROTOBUF_EMITTER}.emitter-output-dir={tmp}',
        ]
        # cwd is schema/ so the local node_modules resolves.
        subprocess.run(cmd, cwd=_SCHEMA_DIR, check=True)  # noqa: S603
        emitted = sorted(pathlib.Path(tmp).rglob('*.proto'))
        if len(emitted) != 1:
            raise SystemExit(f'expected exactly one .proto from {entrypoint}, got {[str(p) for p in emitted]}')
        relpath = emitted[0].relative_to(tmp)
        text = emitted[0].read_text()
    proto_path = _PROTO_DIR / relpath
    proto_path.parent.mkdir(parents=True, exist_ok=True)
    # Strip per-line trailing whitespace and end with one newline: the emitter renders a
    # multi-paragraph doc-comment's blank line as a trailing-space `// `, which the
    # trailing-whitespace hook would rewrite — keep the committed proto byte-stable for the
    # freshness gate.
    proto_path.write_text('\n'.join(line.rstrip() for line in text.splitlines()) + '\n')
    return proto_path


def _emit_grpc_stubs(proto_path: pathlib.Path) -> None:
    """Generate the committed gRPC Python stubs from a committed ``.proto``.

    ``protoc`` (grpcio-tools) reads it with ``--proto_path=schema/proto`` so the
    proto's package path (``themis/rpc/<domain>.proto``) becomes the Python import
    ``themis.rpc.<domain>_pb2``. The repo-root outputs are ``<domain>_pb2.py`` (the
    dynamically-built message classes) + ``<domain>_pb2.pyi`` (their static types, so
    an importer such as the servicer type-checks) + ``<domain>_pb2_grpc.py`` (the stub
    + servicer base). The stubs self-mark as generated.
    """
    _RPC_DIR.mkdir(parents=True, exist_ok=True)
    relpath = proto_path.relative_to(_PROTO_DIR)
    cmd = [
        sys.executable,
        '-m',
        'grpc_tools.protoc',
        f'--proto_path={_PROTO_DIR}',
        f'--python_out={_REPO_ROOT}',
        f'--pyi_out={_REPO_ROOT}',
        f'--grpc_python_out={_REPO_ROOT}',
        str(relpath),
    ]
    subprocess.run(cmd, cwd=_REPO_ROOT, check=True)  # noqa: S603


def main() -> int:
    if not _TSP_BIN.exists():
        raise SystemExit(f'tsp binary not found at {_TSP_BIN}; run `bun install` in schema/ first')
    if shutil.which('datamodel-codegen') is None:
        raise SystemExit('datamodel-codegen not found; run via `uv run --group codegen python -m tools.schema.regen`')
    if importlib.util.find_spec('grpc_tools') is None:
        raise SystemExit('grpcio-tools not found; run via `uv run --group codegen python -m tools.schema.regen`')

    for domain in _DOMAINS:
        targets = f'{domain.schema_out.name} + {domain.pydantic_out.name}'
        if domain.zod_out is not None:
            targets += f' + {domain.zod_out.name}'
        print(f'schema/regen: {domain.name} -> {targets}')
        _emit_schema(domain)
        _emit_pydantic(domain)
        _emit_zod(domain)

    services = _service_domains()
    for service in services:
        print(f'schema/regen: {service.name} (service) -> proto + grpc stubs')
        proto_path = _emit_proto(service)
        _emit_grpc_stubs(proto_path)

    print(f'schema/regen: emitted {len(_DOMAINS)} domain(s) + {len(services)} service domain(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
