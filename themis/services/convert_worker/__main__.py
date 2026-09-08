"""Convert-worker entrypoint: an HTTP endpoint that runs a pushed full-text conversion.

A Cloud Task delivers `POST /convert {"doc_id": ...}`; the handler calls the litcache producer to
resolve that paper's full text off the read service's request path (architecture B,
`docs/design/evidence-fulltext.md`). The bucket comes from `THEMIS_FULLTEXT_BUCKET` (the same litcache
bucket the read service serves). The PDF branch transcribes on Claude (`litcache.anthropic_ocr`),
authenticated as the worker's runtime service account by workload identity federation — no stored key
(`docs/runbooks/claude-api-wif.md`). The bucket name and the four federation identifiers are read at
startup and fail loud there, so a misconfigured revision never serves rather than spending a paper's
retry budget per task; a converter failure settles the paper only when it is an `ocr.OcrError`, so an
auth failure at call time surfaces as an `anthropic` error, is retried, and leaves the paper PENDING.
Each transcription's token usage goes to the request-token counter, exported periodically off the
request path (`themis/telemetry`), so a failed export is logged and never fails a conversion. `PORT` is
the Cloud Run convention; `/healthz` reports liveness.
"""

from __future__ import annotations

import datetime
import functools
import logging
import os

from aiohttp import web
from google.api_core import exceptions as api_exceptions
from google.cloud import storage
from opentelemetry.sdk import metrics as sdk_metrics
from opentelemetry.sdk.metrics import export as metrics_export

from themis.clients import anthropic_wif
from themis.litcache import anthropic_ocr, ocr
from themis.litcache import produce as produce_mod
from themis.services.convert_worker import handler as handler_mod
from themis.telemetry import metrics as telemetry_metrics
from themis.telemetry import names, request_tokens

_BUCKET: web.AppKey[storage.Bucket] = web.AppKey('bucket', storage.Bucket)
_CONVERT_PDF: web.AppKey[ocr.PdfConverter] = web.AppKey('convert_pdf')
_METER_PROVIDER: web.AppKey[sdk_metrics.MeterProvider] = web.AppKey('meter_provider', sdk_metrics.MeterProvider)

# The series' `job` label; the Cloud Run service's name.
_SERVICE_NAME = 'themis-convert-worker'
_EXPORT_INTERVAL = datetime.timedelta(seconds=60)
_EXPORT_TIMEOUT = datetime.timedelta(seconds=30)
# Cloud Run allows an instance ten seconds after SIGTERM; the final export has to fit inside them.
_SHUTDOWN_TIMEOUT = datetime.timedelta(seconds=8)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text='ok')


async def _convert(request: web.Request) -> web.Response:
    produce = functools.partial(produce_mod.produce_full_text, convert_pdf=request.app[_CONVERT_PDF])
    status = await handler_mod.process_conversion(request.app[_BUCKET], await request.read(), produce=produce)
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


def _credentials_from_env() -> anthropic_ocr.CredentialsFactory:
    """Bind the worker's Anthropic federation identity, for the Claude converter to authenticate with.

    The four identifiers are plaintext ids, not credentials (`docs/runbooks/claude-api-wif.md`). They
    are read here rather than at the call so a revision missing one fails its startup probe. A metadata
    server failure at the exchange is not an `anthropic` error, so the SDK wraps it as
    `anthropic.APIConnectionError`, which is transient — the paper is retried, not settled.
    """
    return functools.partial(
        anthropic_wif.credentials,
        federation_rule_id=_require('ANTHROPIC_FEDERATION_RULE_ID'),
        organization_id=_require('ANTHROPIC_ORGANIZATION_ID'),
        service_account_id=_require('ANTHROPIC_SERVICE_ACCOUNT_ID'),
        workspace_id=_require('ANTHROPIC_WORKSPACE_ID'),
    )


def _meter_provider() -> sdk_metrics.MeterProvider:
    """The worker's metrics pipeline: the Telemetry API exporter on a periodic reader, placed by detection.

    On Cloud Run the GCP detector reads the region and instance off the metadata server and the service
    name off `K_SERVICE`; a revision the detector cannot place fails its startup probe here rather than
    have every point it writes dropped.
    """
    credentials, project = telemetry_metrics.application_default_credentials()
    resource = telemetry_metrics.workload_resource(
        project=project, service_name=_SERVICE_NAME, detectors=telemetry_metrics.google_cloud_detectors()
    )
    reader = metrics_export.PeriodicExportingMetricReader(
        telemetry_metrics.telemetry_api_exporter(credentials, timeout=_EXPORT_TIMEOUT),
        export_interval_millis=_EXPORT_INTERVAL.total_seconds() * 1000,
        export_timeout_millis=_EXPORT_TIMEOUT.total_seconds() * 1000,
    )
    return sdk_metrics.MeterProvider(metric_readers=[reader], resource=resource)


async def _on_startup(app: web.Application) -> None:
    # Env reads before anything that reaches a network, so a missing identifier is reported as itself
    # rather than behind a connection failure.
    credentials = _credentials_from_env()
    app[_BUCKET] = _bucket_from_env()
    provider = _meter_provider()
    app[_METER_PROVIDER] = provider
    tokens = request_tokens.RequestTokens(provider.get_meter(names.METER_NAME))
    app[_CONVERT_PDF] = functools.partial(anthropic_ocr.convert_pdf, credentials=credentials, tokens=tokens)


async def _on_cleanup(app: web.Application) -> None:
    app[_METER_PROVIDER].shutdown(timeout_millis=_SHUTDOWN_TIMEOUT.total_seconds() * 1000)
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
