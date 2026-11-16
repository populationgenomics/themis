"""Convert-worker entrypoint: an HTTP endpoint that runs a pushed full-text conversion.

A Cloud Task delivers `POST /convert` with a `WorkRequest`; the handler calls the litcache producer to
bring that paper up to date, step by step, off the read service's request path (architecture B,
`docs/design/evidence-fulltext.md`). The bucket comes from `THEMIS_FULLTEXT_BUCKET` (the same litcache
bucket the read service serves). The PDF branch transcribes on Claude (`litcache.anthropic_ocr`),
authenticated as the worker's runtime service account by workload identity federation — no stored key
(`docs/runbooks/claude-api-wif.md`). The bucket name and the four federation identifiers are read at
startup and fail loud there, so a misconfigured revision never serves rather than spending a paper's
retry budget per task; a transcription settles only on an `ocr.OcrError`, so an auth failure at call
time surfaces as an `anthropic` error, is retried, and leaves the paper PENDING.
Each transcription's token usage goes to the request-token counter, exported periodically off the
request path (`themis/telemetry`), so a failed export is logged and never fails a conversion. `PORT` is
the Cloud Run convention; `/healthz` reports liveness.

The port opens only once startup has finished, so the startup probe waits on every step of it. A
`startup.StartupClock` logs how long each step took, and `variant_reference` how long each store and genome
took to open. Each cold start is also a trace of its own, at the ratio `THEMIS_TRACE_SAMPLE_RATIO` names
(`themis/telemetry/tracing.py`): a `startup` span from the point the trace pipeline is installed, with the
bucket check, the open of the reference stores and the metrics pipeline as spans beneath it, and each store
and genome opened a span beneath that.

`THEMIS_CONVERT_CONCURRENCY` is how many requests the service lets one instance serve at once, and
sizes the thread pool each request's producer runs on (`handler.process_conversion`). It is required:
a pool smaller than the service's limit would cap the instance below it without saying so.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import functools
import logging
import os

from aiohttp import web
from google.api_core import exceptions as api_exceptions
from google.cloud import storage
from opentelemetry.sdk import metrics as sdk_metrics

from themis.clients import anthropic_wif
from themis.litcache import (
    anthropic_ocr,
    figure_annotate,
    ocr,
    pedigree_extract,
    variant_extract,
    variant_reference,
    variant_validate,
)
from themis.litcache import produce as produce_mod
from themis.litcache.models import variants_pb2
from themis.services.convert_worker import handler as handler_mod
from themis.telemetry import metrics as telemetry_metrics
from themis.telemetry import names, request_tokens, startup, tracing

_CLOCK: web.AppKey[startup.StartupClock] = web.AppKey('clock', startup.StartupClock)
_BUCKET: web.AppKey[storage.Bucket] = web.AppKey('bucket', storage.Bucket)
_CONVERT_PDF: web.AppKey[ocr.PdfConverter] = web.AppKey('convert_pdf')
_VARIANTS: web.AppKey[produce_mod.Variants] = web.AppKey('variants', produce_mod.Variants)
_FIGURES: web.AppKey[produce_mod.Figures] = web.AppKey('figures', produce_mod.Figures)
_PEDIGREES: web.AppKey[produce_mod.Pedigrees] = web.AppKey('pedigrees', produce_mod.Pedigrees)
_METER_PROVIDER: web.AppKey[sdk_metrics.MeterProvider] = web.AppKey('meter_provider', sdk_metrics.MeterProvider)
_EXECUTOR: web.AppKey[concurrent.futures.ThreadPoolExecutor] = web.AppKey(
    'executor', concurrent.futures.ThreadPoolExecutor
)

# The series' `job` label and the name Cloud Trace lists the spans under: the Cloud Run service's name, which
# Cloud Run sets in the environment, so the interactive and bulk services are told apart.
_SERVICE_NAME_ENV = 'K_SERVICE'
# Cloud Run allows an instance ten seconds after SIGTERM; the two final exports have to fit inside them.
_METRICS_SHUTDOWN_TIMEOUT = datetime.timedelta(seconds=6)
_TRACE_FLUSH_TIMEOUT = datetime.timedelta(seconds=2)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text='ok')


async def _convert(request: web.Request) -> web.Response:
    produce = functools.partial(
        produce_mod.run,
        convert_pdf=request.app[_CONVERT_PDF],
        variants=request.app[_VARIANTS],
        figures=request.app[_FIGURES],
        pedigrees=request.app[_PEDIGREES],
    )
    status = await handler_mod.process_conversion(
        request.app[_BUCKET],
        await request.read(),
        content_type=request.content_type,
        headers=request.headers,
        produce=produce,
        executor=request.app[_EXECUTOR],
    )
    return web.Response(status=status)


def _concurrency_from_env() -> int:
    value = _require('THEMIS_CONVERT_CONCURRENCY')
    if not value.isdecimal() or int(value) < 1:
        raise SystemExit(f'THEMIS_CONVERT_CONCURRENCY must be a positive integer, not {value!r}')
    return int(value)


def _readable_bucket(name: str) -> storage.Bucket:
    """The fulltext bucket `name`, on the process's one storage client, once a listing shows it is readable."""
    bucket = storage.Client().bucket(name)
    # Fail the startup probe on a missing/unreadable bucket rather than 500-ing every conversion: a
    # lazy handle would 404 on the first read inside a task. `objects.list` is what the worker SA's
    # objectUser grants (not `buckets.get`); an empty result is a valid not-yet-populated corpus.
    try:
        next(iter(bucket.list_blobs(prefix='papers/', max_results=1)), None)
    except api_exceptions.NotFound as e:
        raise SystemExit(f'THEMIS_FULLTEXT_BUCKET {name!r} does not exist or is not readable') from e
    return bucket


def _references(resources_bucket: str, client: storage.Client) -> variant_reference.References:
    """Open the pinned builds of the reference stores in `resources_bucket`, their checksums read through `client`."""
    root = f'gs://{resources_bucket}'
    try:
        return variant_reference.References(root, variant_reference.BUILDS, storage_client=client)
    except (FileNotFoundError, ValueError) as e:
        raise SystemExit(f'{root} lacks a pinned reference build: {e}') from e


def _variants(
    references: variant_reference.References,
    *,
    credentials: anthropic_ocr.CredentialsFactory,
    tokens: ocr.TokenCounter,
) -> produce_mod.Variants:
    """The variant steps' collaborators, over `references`."""

    def validate(
        mention_set: variants_pb2.MentionSet, text: str
    ) -> tuple[
        variants_pb2.MentionSet,
        variants_pb2.Verdicts,
        variants_pb2.VariantValidationConstruction,
    ]:
        return variant_validate.validate(mention_set, text=text, references=references)

    return produce_mod.Variants(
        extract=functools.partial(
            variant_extract.extract_markdown, references=references, tokens=tokens, credentials=credentials
        ),
        validate=validate,
        validation=variant_validate.construction(references),
    )


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


def _meter_provider(service: str) -> sdk_metrics.MeterProvider:
    """The metrics pipeline of the Cloud Run service `service`; a revision the detector cannot place fails here."""
    return telemetry_metrics.service_meter_provider(service)


async def _on_startup(app: web.Application) -> None:
    clock = app[_CLOCK]
    # Env reads before anything that reaches a network, so a missing variable is reported as itself
    # rather than behind a connection failure. The trace pipeline reads its own, before it reaches for anything.
    credentials = _credentials_from_env()
    concurrency = _concurrency_from_env()
    fulltext_bucket = _require('THEMIS_FULLTEXT_BUCKET')
    resources_bucket = _require('THEMIS_RESOURCES_BUCKET')
    service = _require(_SERVICE_NAME_ENV)
    clock.step('configuration read')
    tracing.install_from_env(service)
    clock.step('tracing installed')
    with startup.trace_root():
        with clock.span('fulltext bucket checked'):
            bucket = _readable_bucket(fulltext_bucket)
        app[_BUCKET] = bucket
        with clock.span('reference stores opened'):
            references = _references(resources_bucket, bucket.client)
        app[_EXECUTOR] = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='convert')
        with clock.span('metrics installed'):
            provider = _meter_provider(service)
        app[_METER_PROVIDER] = provider
        tokens = request_tokens.RequestTokens(provider.get_meter(names.METER_NAME))
        app[_CONVERT_PDF] = functools.partial(anthropic_ocr.convert_pdf, credentials=credentials, tokens=tokens)
        app[_VARIANTS] = _variants(references, credentials=credentials, tokens=tokens)
        app[_FIGURES] = produce_mod.Figures(
            annotate=functools.partial(figure_annotate.annotate_paper, tokens=tokens, credentials=credentials)
        )
        app[_PEDIGREES] = produce_mod.Pedigrees(
            read=functools.partial(pedigree_extract.read_paper, tokens=tokens, credentials=credentials)
        )
        clock.step('collaborators bound')


async def _on_cleanup(app: web.Application) -> None:
    # The token counts first: of the two exports, they are the one a stopping instance must not lose.
    app[_METER_PROVIDER].shutdown(timeout_millis=_METRICS_SHUTDOWN_TIMEOUT.total_seconds() * 1000)
    tracing.flush(_TRACE_FLUSH_TIMEOUT)
    app[_BUCKET].client.close()
    app[_EXECUTOR].shutdown(wait=False, cancel_futures=True)


def build_app(clock: startup.StartupClock) -> web.Application:
    """The worker's app, whose startup logs each of its steps on `clock`."""
    app = web.Application()
    app[_CLOCK] = clock
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get('/healthz', _healthz)
    app.router.add_post('/convert', _convert)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    web.run_app(build_app(startup.StartupClock()), port=int(os.environ.get('PORT', '8080')))


if __name__ == '__main__':
    main()
