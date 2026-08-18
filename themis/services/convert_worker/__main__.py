"""Convert-worker entrypoint: an HTTP endpoint that runs a pushed full-text conversion.

A Cloud Task delivers `POST /convert {"doc_id": ...}`; the handler calls the litcache producer to
resolve that paper's full text off the read service's request path (architecture B,
`docs/design/evidence-fulltext.md`). The bucket comes from `THEMIS_FULLTEXT_BUCKET` (the same litcache
bucket the read service serves), and fails loud at startup rather than per task. The PDF branch has no
model backend wired yet, so it raises instead of transcribing (`convert_worker.pdf`): the task is retried and the
paper stays PENDING, never settled. `PORT` is the Cloud Run convention; `/healthz` reports liveness.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web
from google.api_core import exceptions as api_exceptions
from google.cloud import storage

from themis.litcache import outcome
from themis.litcache import produce as produce_mod
from themis.services.convert_worker import handler as handler_mod
from themis.services.convert_worker import pdf as pdf_mod

_BUCKET: web.AppKey[storage.Bucket] = web.AppKey('bucket', storage.Bucket)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text='ok')


async def _convert(request: web.Request) -> web.Response:
    status = await handler_mod.process_conversion(request.app[_BUCKET], await request.read(), produce=_produce)
    return web.Response(status=status)


def _bucket_from_env() -> storage.Bucket:
    bucket_name = _require('THEMIS_FULLTEXT_BUCKET')
    bucket = storage.Client().bucket(bucket_name)
    # Fail the startup probe on a missing/unreadable bucket rather than 500-ing every conversion: a
    # lazy handle would 404 on the first read inside a task. `objects.list` is what the worker SA's
    # objectUser grants (not `buckets.get`); an empty result is a valid not-yet-populated corpus.
    try:
        next(iter(bucket.list_blobs(prefix='papers/', max_results=1)), None)
    except api_exceptions.NotFound as e:
        raise SystemExit(f'THEMIS_FULLTEXT_BUCKET {bucket_name!r} does not exist or is not readable') from e
    return bucket


async def _produce(bucket: storage.Bucket, doc_id: str) -> outcome.Readiness:
    return await produce_mod.produce_full_text(bucket, doc_id, convert_pdf=pdf_mod.unconfigured_convert_pdf)


async def _on_startup(app: web.Application) -> None:
    app[_BUCKET] = _bucket_from_env()


async def _on_cleanup(app: web.Application) -> None:
    app[_BUCKET].client.close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get('/healthz', _healthz)
    app.router.add_post('/convert', _convert)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    web.run_app(build_app(), port=int(os.environ.get('PORT', '8080')))


if __name__ == '__main__':
    main()
