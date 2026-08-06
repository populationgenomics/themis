"""Server entrypoint: build the backend from the environment and serve the gRPC service.

``THEMIS_BACKEND`` selects the adapter (required — no silent default): ``fixture`` (in-memory, seeded
from ``THEMIS_EVIDENCE_FIXTURE``) or ``live`` (the litcache-reading backend over
``THEMIS_FULLTEXT_BUCKET``). ``PORT`` is the Cloud Run convention. A ``grpc.health.v1`` health
service reports SERVING alongside.
"""

from __future__ import annotations

import asyncio
import json
import os

import grpc.aio
from google.api_core import exceptions as api_exceptions
from google.cloud import storage
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from themis.rpc import literature_pb2, literature_pb2_grpc
from themis.services.evidence.literature import backend as literature_backend
from themis.services.evidence.literature import litcache as litcache_backend
from themis.services.evidence.literature import servicer as servicer_mod

_FILE_ROLES = {
    'FIGURE': literature_pb2.FILE_ROLE_FIGURE,
    'SUPPLEMENTARY': literature_pb2.FILE_ROLE_SUPPLEMENTARY,
}


def build_literature_backend() -> literature_backend.LiteratureBackend:
    backend = os.environ.get('THEMIS_BACKEND')
    if backend is None:
        raise SystemExit('THEMIS_BACKEND is required (expected "fixture" or "live")')
    if backend == 'fixture':
        return _fixture_backend_from_env()
    if backend == 'live':
        return _litcache_backend_from_env()
    raise SystemExit(f'unsupported THEMIS_BACKEND {backend!r} (expected "fixture" or "live")')


def _litcache_backend_from_env() -> litcache_backend.LitcacheBackend:
    """Build the litcache-reading backend over the ``THEMIS_FULLTEXT_BUCKET`` GCS bucket."""
    bucket_name = os.environ.get('THEMIS_FULLTEXT_BUCKET')
    if not bucket_name:
        raise SystemExit('THEMIS_FULLTEXT_BUCKET is required for the live backend (the litcache bucket)')
    bucket = storage.Client().bucket(bucket_name)
    # A bucket handle is lazy: a wrong/uncreated name would 404 every read, and _download can't tell
    # "no such object" from "no such bucket", so the service would answer NOT_FOUND for every paper —
    # the "empty corpus reads as genuinely absent" fault the fixture path fails loud on at startup. List
    # once so a bad bucket fails the startup probe instead. `objects.list` is what the runtime SA's
    # objectViewer grants (not `buckets.get`, so `bucket.exists()` would 403 on a correct deploy); an
    # empty result is a valid not-yet-populated corpus. A 403 raises Forbidden, already loud.
    try:
        next(iter(bucket.list_blobs(prefix='papers/', max_results=1)), None)
    except api_exceptions.NotFound as e:
        raise SystemExit(f'THEMIS_FULLTEXT_BUCKET {bucket_name!r} does not exist or is not readable') from e
    return litcache_backend.LitcacheBackend(bucket)


def _fixture_backend_from_env() -> literature_backend.FixtureBackend:
    """Build the offline backend from ``THEMIS_EVIDENCE_FIXTURE``.

    A JSON object mapping each canonical doc_id to a paper:

        {"<doc_id>": {
            "title": "...",
            "markdown": {"gcs_uri": "gs://...", "from_xml": true},   // optional
            "pdf": {"gcs_uri": "gs://..."},                          // optional
            "files": [{"name": "f1.png", "role": "FIGURE", "media_type": "image/png",
                       "gcs_uri": "gs://..."}],
            "markdown_locations": {"<quote>": [start, end]},
            "pdf_locations": {"<quote>": {"page": 0, "rects": [[x, y, w, h]]}}
        }}

    Required — an unset var is an operator error; pass ``{}`` for an explicit empty corpus.
    """
    raw = os.environ.get('THEMIS_EVIDENCE_FIXTURE')
    if raw is None:
        raise SystemExit(
            'THEMIS_EVIDENCE_FIXTURE is required for the fixture backend: a JSON object of '
            'doc_id -> paper, or "{}" for an explicit empty corpus'
        )
    try:
        seeds = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE is not valid JSON: {e}') from e
    if not isinstance(seeds, dict):
        raise SystemExit(
            f'THEMIS_EVIDENCE_FIXTURE must be a JSON object of doc_id -> paper, got {type(seeds).__name__}'
        )
    return literature_backend.FixtureBackend({doc_id: _parse_paper(doc_id, paper) for doc_id, paper in seeds.items()})


def _parse_paper(doc_id: str, paper: object) -> literature_backend.SeededPaper:
    if not isinstance(paper, dict):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} must be a JSON object')
    unknown = set(paper) - {'title', 'files', 'markdown', 'markdown_locations', 'pdf', 'pdf_locations'}
    if unknown:
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} has unknown field(s) {sorted(unknown)}')
    title = paper.get('title')
    if not isinstance(title, str) or not title:
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} must set a non-empty "title"')
    return literature_backend.SeededPaper(
        title=title,
        files=tuple(_parse_file(doc_id, f) for f in _as_list(doc_id, 'files', paper.get('files', []))),
        markdown_gcs_uri=_rendering_uri(doc_id, 'markdown', paper.get('markdown')),
        markdown_from_xml=_markdown_from_xml(doc_id, paper.get('markdown')),
        pdf_gcs_uri=_rendering_uri(doc_id, 'pdf', paper.get('pdf')),
        markdown_locations=_parse_offsets(doc_id, paper.get('markdown_locations', {})),
        pdf_locations=_parse_pdf_locations(doc_id, paper.get('pdf_locations', {})),
    )


def _as_list(doc_id: str, key: str, value: object) -> list[object]:
    if not isinstance(value, list):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} field {key!r} must be a JSON array')
    return value


def _parse_file(doc_id: str, f: object) -> literature_backend.SeededFile:
    if not isinstance(f, dict):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} file must be a JSON object')
    role_name = f.get('role')
    if role_name not in _FILE_ROLES:
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} file "role" must be one of {sorted(_FILE_ROLES)}')
    for field in ('name', 'media_type', 'gcs_uri'):
        if not isinstance(f.get(field), str) or not f[field]:
            raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} file must set a non-empty {field!r}')
    return literature_backend.SeededFile(
        name=f['name'], role=_FILE_ROLES[role_name], media_type=f['media_type'], gcs_uri=f['gcs_uri']
    )


def _rendering_uri(doc_id: str, key: str, rendering: object) -> str | None:
    if rendering is None:
        return None
    if not isinstance(rendering, dict) or not isinstance(rendering.get('gcs_uri'), str):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} {key!r} must be an object with a "gcs_uri"')
    allowed = {'gcs_uri', 'from_xml'} if key == 'markdown' else {'gcs_uri'}
    unknown = set(rendering) - allowed
    if unknown:
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} {key!r} has unknown field(s) {sorted(unknown)}')
    return rendering['gcs_uri']


def _markdown_from_xml(doc_id: str, markdown: object) -> bool:
    if markdown is None:
        return False
    if not isinstance(markdown, dict):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} "markdown" must be a JSON object')
    from_xml = markdown.get('from_xml', False)
    if not isinstance(from_xml, bool):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} markdown "from_xml" must be a boolean')
    return from_xml


def _parse_offsets(doc_id: str, locations: object) -> dict[str, tuple[int, int]]:
    if not isinstance(locations, dict):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} "markdown_locations" must be a JSON object')
    parsed: dict[str, tuple[int, int]] = {}
    for quote, offsets in locations.items():
        if not isinstance(offsets, list) or len(offsets) != 2 or not all(isinstance(n, int) for n in offsets):
            raise SystemExit(
                f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} markdown_locations[{quote!r}] must be [start, end]'
            )
        parsed[quote] = (offsets[0], offsets[1])
    return parsed


def _parse_pdf_locations(doc_id: str, locations: object) -> dict[str, literature_backend.SeededPdfLocation]:
    if not isinstance(locations, dict):
        raise SystemExit(f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} "pdf_locations" must be a JSON object')
    parsed: dict[str, literature_backend.SeededPdfLocation] = {}
    for quote, location in locations.items():
        if not isinstance(location, dict) or not isinstance(location.get('page'), int):
            raise SystemExit(
                f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} pdf_locations[{quote!r}] must set an integer "page"'
            )
        rects_raw = location.get('rects', [])
        if not isinstance(rects_raw, list):
            raise SystemExit(
                f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} pdf_locations[{quote!r}] "rects" must be an array'
            )
        rects: list[tuple[float, float, float, float]] = []
        for rect in rects_raw:
            if not isinstance(rect, list) or len(rect) != 4 or not all(isinstance(n, (int, float)) for n in rect):
                raise SystemExit(
                    f'THEMIS_EVIDENCE_FIXTURE paper {doc_id!r} pdf_locations[{quote!r}] rect must be [x, y, w, h]'
                )
            rects.append((float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])))
        parsed[quote] = literature_backend.SeededPdfLocation(page=location['page'], rects=tuple(rects))
    return parsed


async def _serve() -> None:
    server = grpc.aio.server()
    literature_pb2_grpc.add_LiteratureServicer_to_server(servicer_mod.Servicer(build_literature_backend()), server)
    # grpc_health ships no py.typed; `health.aio` is a runtime re-export pyright can't see.
    health_servicer = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
    await health_servicer.set('', health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port(f'[::]:{os.environ.get("PORT", "8080")}')  # TLS terminated by Cloud Run
    await server.start()
    await server.wait_for_termination()


def main() -> None:
    asyncio.run(_serve())


if __name__ == '__main__':
    main()
