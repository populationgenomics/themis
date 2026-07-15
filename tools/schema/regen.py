"""Regenerate the committed code-generation artifacts from the TypeSpec sources.

Every domain emits **proto** (``@typespec/protobuf``): the committed ``.proto`` is the
``buf breaking`` baseline and the ``protoc`` (grpcio-tools) source for the Python stubs
(ADR 0003). Two kinds, discovered two ways:

**Registered domains** (``_DOMAINS``) carry explicit committed-output locations (their
proto/stubs sit outside ``schema/proto/``). The feature-coverage corpus
(``schema/tests/fixtures/features/``) is the only one today: it emits proto + stubs to
verify every construct round-trips, and is a test fixture — its proto lives under
``schema/tests/``, not ``schema/proto/``, so ``buf`` does not gate it (its shape changes
freely as constructs are added).

**gRPC service domains** (``_service_domains``) are glob-discovered — every
``schema/<name>/main.tsp`` not claimed by a registered domain — because a service carries
no custom output locations (paths derive from its ``@package`` name). Each emits its
committed ``schema/proto/<pkg>.proto`` (the contract) and the ``themis/rpc/<name>_pb2*.py``
stubs, including the gRPC stub + servicer base (``docs/design/services.md``).

The browser-facing view model (bucket 2, ADR 0003) is deferred: nothing consumes it yet,
and proto and Zod diverge (integer vs string enums, no proto ``oneof``/literal), so the
target is chosen with the first real view model. No Zod is emitted here for now.

Run with ``uv run --group codegen python -m tools.schema.regen``. The TypeSpec toolchain
under ``schema/`` must be installed with Bun (``bun install`` from ``schema/``) and
``grpcio-tools`` available (the ``codegen`` uv group); this fails loudly if either is
missing.

This is the pure-generation step. Verification (proto validity, stub import, backward
compatibility) lives in tests and CI gates, not here.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import subprocess
import sys
import tempfile

from tools.schema import zod_canonicalize, zod_reorder

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCHEMA_DIR = _REPO_ROOT / 'schema'
_TSP_BIN = _SCHEMA_DIR / 'node_modules' / '.bin' / 'tsp'

_PROTOBUF_EMITTER = '@typespec/protobuf'
_ZOD_EMITTER = 'typespec-zod'

# Service domains: committed .proto under schema/proto/ (by package path), stubs under the
# repo root (the proto's themis/rpc/<name> package path lands them in themis/rpc/).
_PROTO_DIR = _SCHEMA_DIR / 'proto'
_TESTS_DIR_NAME = 'tests'


@dataclasses.dataclass(frozen=True)
class Domain:
    """A registered domain emitting proto + Python stubs to explicit locations.

    Attributes:
        name: The domain name.
        main_tsp: The domain's ``main.tsp`` entry point.
        proto_root: Committed-``.proto`` root; the emitter writes
            ``<proto_root>/<package-path>.proto``.
        stubs_root: Root the ``protoc`` ``_pb2`` stubs are written under (the proto's
            package path nests beneath it).
    """

    name: str
    main_tsp: pathlib.Path
    proto_root: pathlib.Path
    stubs_root: pathlib.Path
    zod_out: pathlib.Path | None = None


@dataclasses.dataclass(frozen=True)
class ServiceDomain:
    """A gRPC service domain: a ``main.tsp`` emitting a proto contract + stubs.

    Glob-discovered (see ``_service_domains``) — no custom output locations; the committed
    proto path derives from the ``.tsp``'s ``@package`` name.
    """

    name: str
    main_tsp: pathlib.Path


_DOMAINS: list[Domain] = [
    # Feature-coverage corpus: proto + stubs, verifying every construct round-trips. A test
    # fixture — its proto lives under schema/tests/, not schema/proto/, so buf does not gate
    # it (its shape changes freely as constructs are added). Its @package is flat
    # (`features`) so the generated stub does not collide with the real themis/ package.
    Domain(
        name='features',
        main_tsp=_SCHEMA_DIR / 'tests' / 'fixtures' / 'features' / 'main.tsp',
        proto_root=_SCHEMA_DIR / 'tests' / 'proto',
        stubs_root=_SCHEMA_DIR / 'tests' / 'proto',
        zod_out=_SCHEMA_DIR / 'tests' / 'zod' / 'features.ts',
    ),
    # litcache at-rest artifacts (the manifest). A real durable contract: its proto lands
    # under schema/proto/ so `buf breaking` gates it (ADR 0003). @package themis.litcache.models.litcache
    # -> committed schema/proto/themis/litcache/models/litcache.proto + themis/litcache/models/litcache_pb2.py
    # stubs (the domain library themis.litcache holds the hand-written layer). At-rest, so no Zod (the
    # browser view model is a separate concern; ADR 0004).
    Domain(
        name='litcache',
        main_tsp=_SCHEMA_DIR / 'litcache' / 'main.tsp',
        proto_root=_PROTO_DIR,
        stubs_root=_REPO_ROOT,
    ),
]


def _service_domains() -> list[ServiceDomain]:
    """Every ``schema/<name>/main.tsp`` not claimed by a registered ``Domain``.

    A service carries no per-domain output config, so it is discovered by glob rather than
    registered. A registered ``Domain`` sharing the ``schema/<name>/`` layout would be
    excluded here — the two kinds are told apart by registration, not by path.
    """
    registered = {domain.main_tsp for domain in _DOMAINS}
    return [
        ServiceDomain(name=main_tsp.parent.name, main_tsp=main_tsp)
        for main_tsp in sorted(_SCHEMA_DIR.glob('*/main.tsp'))
        if main_tsp.parent.name != _TESTS_DIR_NAME and main_tsp not in registered
    ]


def _emit_proto(main_tsp: pathlib.Path, proto_root: pathlib.Path) -> pathlib.Path:
    """Compile ``main.tsp`` to its committed ``.proto`` under ``proto_root``; return the path.

    ``@typespec/protobuf`` writes ``<package-path>.proto`` (the ``@package`` name, e.g.
    ``themis.rpc.auth`` -> ``themis/rpc/auth.proto``); emit to a temp dir, then copy it under
    ``proto_root`` at the same relative path.
    """
    entrypoint = main_tsp.relative_to(_SCHEMA_DIR)
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
    proto_path = proto_root / relpath
    proto_path.parent.mkdir(parents=True, exist_ok=True)
    # Strip per-line trailing whitespace and end with one newline: the emitter renders a
    # multi-paragraph doc-comment's blank line as a trailing-space `// `, which the
    # trailing-whitespace hook would rewrite — keep the committed proto byte-stable for the
    # freshness gate.
    proto_path.write_text('\n'.join(line.rstrip() for line in text.splitlines()) + '\n')
    return proto_path


def _emit_stubs(proto_path: pathlib.Path, proto_root: pathlib.Path, stubs_root: pathlib.Path, *, grpc: bool) -> None:
    """Generate the committed Python stubs from a committed ``.proto`` via ``protoc``.

    ``protoc`` (grpcio-tools) reads it with ``--proto_path=proto_root`` so the proto's package
    path becomes the Python import. Emits ``<name>_pb2.py`` (message classes) + ``<name>_pb2.pyi``
    (their static types). ``grpc`` additionally emits ``<name>_pb2_grpc.py`` (the stub + servicer
    base) — a service domain; a data domain declares no service, so it is skipped.
    """
    stubs_root.mkdir(parents=True, exist_ok=True)
    relpath = proto_path.relative_to(proto_root)
    cmd = [
        sys.executable,
        '-m',
        'grpc_tools.protoc',
        f'--proto_path={proto_root}',
        f'--python_out={stubs_root}',
        f'--pyi_out={stubs_root}',
    ]
    if grpc:
        cmd.append(f'--grpc_python_out={stubs_root}')
    cmd.append(str(relpath))
    subprocess.run(cmd, cwd=_REPO_ROOT, check=True)  # noqa: S603


def _emit_zod(main_tsp: pathlib.Path, proto_path: pathlib.Path, zod_out: pathlib.Path) -> None:
    """Emit a domain's Zod view model from its ``.tsp``: canonicalize, then reorder.

    ``typespec-zod`` writes a ``models.ts`` that is neither proto-aware (integer enums,
    well-known placeholders — see ``tools.schema.zod_canonicalize``) nor dependency-ordered
    (see ``tools.schema.zod_reorder``). Emit to a temp dir, canonicalize against the committed
    proto so the Zod validates the same proto3-JSON (ADR 0004), reorder, and write the file.
    """
    entrypoint = main_tsp.relative_to(_SCHEMA_DIR)
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
        # cwd is schema/ so the local node_modules resolves.
        subprocess.run(cmd, cwd=_SCHEMA_DIR, check=True)  # noqa: S603
        raw = (pathlib.Path(tmp) / 'models.ts').read_text()
    canonical = zod_canonicalize.canonicalize(raw, proto_path.read_text())
    zod_out.parent.mkdir(parents=True, exist_ok=True)
    zod_out.write_text(zod_reorder.reorder(canonical))


def main() -> int:
    if not _TSP_BIN.exists():
        raise SystemExit(f'tsp binary not found at {_TSP_BIN}; run `bun install` in schema/ first')
    if importlib.util.find_spec('grpc_tools') is None:
        raise SystemExit('grpcio-tools not found; run via `uv run --group codegen python -m tools.schema.regen`')

    for domain in _DOMAINS:
        print(f'schema/regen: {domain.name} -> proto + stubs')
        proto_path = _emit_proto(domain.main_tsp, domain.proto_root)
        _emit_stubs(proto_path, domain.proto_root, domain.stubs_root, grpc=False)
        if domain.zod_out is not None:
            print(f'schema/regen: {domain.name} -> zod {domain.zod_out.name}')
            _emit_zod(domain.main_tsp, proto_path, domain.zod_out)

    services = _service_domains()
    for service in services:
        print(f'schema/regen: {service.name} (service) -> proto + grpc stubs')
        proto_path = _emit_proto(service.main_tsp, _PROTO_DIR)
        _emit_stubs(proto_path, _PROTO_DIR, _REPO_ROOT, grpc=True)

    print(f'schema/regen: emitted {len(_DOMAINS)} domain(s) + {len(services)} service domain(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
