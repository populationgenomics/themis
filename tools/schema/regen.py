"""Regenerate the committed stubs from the hand-authored protos (proto.md).

Fully local — no BSR remote plugins:

1. The upstream record schemas our own protos embed are copied verbatim out of their published
   wheels into the module, each at the path the wheel's stub registers its descriptor under
   (``docs/design/proto.md``, "Generated upstream schemas").
2. ``buf export`` materializes the protos + their ``buf.lock``-pinned deps (``buf/validate``)
   into a temp tree. This is a one-time, cached module fetch, not a repeated remote-plugin
   execution — so it is not subject to the remote-plugin rate limit.
3. ``grpcio-tools``' ``protoc`` emits the Python stubs from that tree: message classes + ``.pyi``
   over every proto but the copied one, whose stubs the wheel itself ships (plus the used
   ``buf/validate`` dep — the ``protovalidate`` wheels ship no Python stub); the gRPC stub +
   servicer base over the service-declaring protos only (selected from the descriptor set — a proto
   that declares no service, data or options, gets none). Its bundled ``protoc`` pins the
   generated-code version to the protobuf 6.x runtime. Well-known types
   (``google.protobuf.*``) resolve from ``grpcio-tools``' bundled includes and stay
   runtime-provided. ``mypy-protobuf``'s ``protoc-gen-mypy_grpc`` adds the ``_pb2_grpc.pyi`` protoc
   itself does not emit (``--pyi_out`` covers messages only): without it a stub's methods are the
   dynamic ``channel.unary_unary(...)`` assignments in the ``.py``, which a type-checker cannot see,
   so a call to an rpc the proto no longer declares goes unnoticed (``docs/design/proto.md``,
   "Schema evolution").
4. ``apps/web/buf.gen.yaml`` — protobuf-es (TypeScript) via the app's local
   ``@bufbuild/protoc-gen-es`` plugin, written to ``apps/web/src/gen/``. The web tier has no wheel to
   read the copied schema's types from, so this pass covers it like any other proto.

``buf`` drives lint + breaking + the dep export; ``grpcio-tools`` and ``protoc-gen-es`` are the
(local) generators. Run with ``uv run --group codegen python -m tools.schema.regen``; ``buf``
must be on ``PATH`` and ``apps/web`` deps installed (``bun install``) for the es plugin.

Pure generation only — validity, ``buf lint``, ``buf breaking``, and stub-import checks live in
the tests and CI gates, not here.
"""

from __future__ import annotations

import importlib.resources
import pathlib
import shutil
import subprocess
import sys
import tempfile

from clinvar_proto import clinvar_pb2
from grpc_tools import protoc
from pubmed_proto import pubmed_pb2

from tools.schema import agent_exposed

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# grpcio-tools' bundled well-known-type protos (descriptor.proto, timestamp.proto, …). The
# protoc.main() API — unlike the `python -m grpc_tools.protoc` CLI — does not add this itself.
_WELL_KNOWN = importlib.resources.files('grpc_tools') / '_proto'
# The upstream record schemas our own protos embed, module-relative. Each wheel publishes its schema
# together with the stubs generated from it, and registers those stubs' descriptor under this path.
_WHEEL_PROTOS = {
    'clinvar_proto/clinvar.proto': clinvar_pb2,
    'pubmed_proto/pubmed.proto': pubmed_pb2,
}


def _copy_wheel_protos() -> None:
    """Refresh the module's copies of the upstream record schemas from their installed wheels.

    Raises:
        SystemExit: If a wheel ships no schema, or registers its stubs under a different path —
            protoc derives the generated import from the path, so a mismatch would compile our
            contract against a second registration of the same descriptor instead of the wheel's.
    """
    for wheel_proto, stub in _WHEEL_PROTOS.items():
        package, filename = wheel_proto.split('/')
        if stub.DESCRIPTOR.name != wheel_proto:
            raise SystemExit(
                f'{package} registers its descriptor as {stub.DESCRIPTOR.name!r}, not {wheel_proto!r}; '
                'the copy under schema/proto/ has to sit at the path the wheel registers'
            )
        source = importlib.resources.files(package) / filename
        if not source.is_file():
            raise SystemExit(
                f'the installed {package} ships no {filename}; pin a release that publishes it '
                '(pyproject, `codegen` and `evidence` groups)'
            )
        target = _REPO_ROOT / 'schema' / 'proto' / wheel_proto
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def _protoc(include: pathlib.Path, protos: list[str], *, grpc: bool) -> None:
    """Run ``grpcio-tools`` protoc over ``protos`` (relative to ``include``), writing to the repo root.

    Emits ``_pb2.py`` + ``.pyi`` always; ``_pb2_grpc.py`` and its ``.pyi`` too when ``grpc`` is set.
    """
    args = [
        'protoc',
        f'--proto_path={include}',
        f'--proto_path={_WELL_KNOWN}',
        f'--python_out={_REPO_ROOT}',
        f'--pyi_out={_REPO_ROOT}',
    ]
    if grpc:
        args += [
            f'--grpc_python_out={_REPO_ROOT}',
            f'--plugin=protoc-gen-mypy_grpc={_mypy_grpc_plugin()}',
            f'--mypy_grpc_out={_REPO_ROOT}',
        ]
    args += protos
    if protoc.main(args) != 0:
        raise SystemExit(f'protoc failed for {protos}')


def _mypy_grpc_plugin() -> str:
    """Absolute path to mypy-protobuf's gRPC stub generator.

    protoc resolves a bare `protoc-gen-<name>` off PATH, and `protoc.main` runs in-process without a
    shell, so the plugin is passed explicitly rather than assumed to be found.
    """
    plugin = shutil.which('protoc-gen-mypy_grpc')
    if plugin is None:
        raise SystemExit('protoc-gen-mypy_grpc not found; run under `uv run --group codegen`')
    return plugin


def main() -> int:
    if shutil.which('buf') is None:
        raise SystemExit('buf not found on PATH; install buf (https://buf.build) to regenerate stubs')

    # Compile the module once: the descriptor set both selects the service-bearing protos for the gRPC pass
    # (a proto that declares no service — a data or options proto — gets no `_pb2_grpc.py`) and drives the
    # agent-exposure surface below.
    print('schema/regen: upstream record schemas (clinvar_proto, pubmed_proto wheels)')
    _copy_wheel_protos()

    image = agent_exposed.build_image()
    service_files = agent_exposed.service_files(image)

    with tempfile.TemporaryDirectory() as tmp:
        export = pathlib.Path(tmp)
        # Materialize the protos + buf.lock-pinned deps (buf/validate) for protoc's include path.
        subprocess.run(['buf', 'export', '.', '--output', str(export)], cwd=_REPO_ROOT, check=True)  # noqa: S603, S607
        protos = sorted(str(p.relative_to(export)) for p in export.rglob('*.proto'))

        print('schema/regen: python message + pyi stubs (all protos + used deps)')
        # The copied schemas are compiled as dependencies but not generated for: each wheel's own
        # `*_pb2` is the module our contract's stubs import, and a second one here would shadow it.
        _protoc(export, [p for p in protos if p not in _WHEEL_PROTOS], grpc=False)

        services = [p for p in protos if p in service_files]
        print('schema/regen: grpc stubs (service protos)')
        _protoc(export, services, grpc=True)

    print('schema/regen: sandbox agent-exposure surface (hatch allowlist)')
    agent_exposed.generate(image)

    print('schema/regen: protobuf-es stubs (apps/web)')
    es = ['buf', 'generate', '--template', 'apps/web/buf.gen.yaml', '--include-imports']
    subprocess.run(es, cwd=_REPO_ROOT, check=True)  # noqa: S603
    print('schema/regen: done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
