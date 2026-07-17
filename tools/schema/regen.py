"""Regenerate the committed stubs from the hand-authored protos (proto.md).

Fully local — no BSR remote plugins:

1. ``buf export`` materializes the protos + their ``buf.lock``-pinned deps (``buf/validate``)
   into a temp tree. This is a one-time, cached module fetch, not a repeated remote-plugin
   execution — so it is not subject to the remote-plugin rate limit.
2. ``grpcio-tools``' ``protoc`` emits the Python stubs from that tree: message classes + ``.pyi``
   over every proto (plus the used ``buf/validate`` dep — the ``protovalidate`` wheels ship no
   Python stub); the gRPC stub + servicer base over the service protos only
   (``themis/rpc/``; a data proto such as litcache declares no service). Its bundled ``protoc``
   pins the generated-code version to the protobuf 6.x runtime. Well-known types
   (``google.protobuf.*``) resolve from ``grpcio-tools``' bundled includes and stay
   runtime-provided.
3. ``apps/web/buf.gen.yaml`` — protobuf-es (TypeScript) via the app's local
   ``@bufbuild/protoc-gen-es`` plugin, written to ``apps/web/src/gen/``.

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

from grpc_tools import protoc

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
# Service protos, relative to the export root: gRPC stubs are generated only for these.
_RPC_PREFIX = 'themis/rpc/'
# grpcio-tools' bundled well-known-type protos (descriptor.proto, timestamp.proto, …). The
# protoc.main() API — unlike the `python -m grpc_tools.protoc` CLI — does not add this itself.
_WELL_KNOWN = importlib.resources.files('grpc_tools') / '_proto'


def _protoc(include: pathlib.Path, protos: list[str], *, grpc: bool) -> None:
    """Run ``grpcio-tools`` protoc over ``protos`` (relative to ``include``), writing to the repo root.

    Emits ``_pb2.py`` + ``.pyi`` always; ``_pb2_grpc.py`` too when ``grpc`` is set.
    """
    args = [
        'protoc',
        f'--proto_path={include}',
        f'--proto_path={_WELL_KNOWN}',
        f'--python_out={_REPO_ROOT}',
        f'--pyi_out={_REPO_ROOT}',
    ]
    if grpc:
        args.append(f'--grpc_python_out={_REPO_ROOT}')
    args += protos
    if protoc.main(args) != 0:
        raise SystemExit(f'protoc failed for {protos}')


def main() -> int:
    if shutil.which('buf') is None:
        raise SystemExit('buf not found on PATH; install buf (https://buf.build) to regenerate stubs')

    with tempfile.TemporaryDirectory() as tmp:
        export = pathlib.Path(tmp)
        # Materialize the protos + buf.lock-pinned deps (buf/validate) for protoc's include path.
        subprocess.run(['buf', 'export', '.', '--output', str(export)], cwd=_REPO_ROOT, check=True)  # noqa: S603, S607
        protos = sorted(str(p.relative_to(export)) for p in export.rglob('*.proto'))

        print('schema/regen: python message + pyi stubs (all protos + used deps)')
        _protoc(export, protos, grpc=False)

        services = [p for p in protos if p.startswith(_RPC_PREFIX)]
        print('schema/regen: grpc stubs (service protos)')
        _protoc(export, services, grpc=True)

    print('schema/regen: protobuf-es stubs (apps/web)')
    es = ['buf', 'generate', '--template', 'apps/web/buf.gen.yaml', '--include-imports']
    subprocess.run(es, cwd=_REPO_ROOT, check=True)  # noqa: S603
    print('schema/regen: done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
