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
`PORT` is the Cloud Run convention; `/healthz` reports liveness.
"""

from __future__ import annotations

import functools
import logging
import os
import typing

import requests
from aiohttp import web
from anthropic.lib import credentials as anthropic_credentials
from google.api_core import exceptions as api_exceptions
from google.auth.transport import requests as google_auth_requests
from google.cloud import storage
from google.oauth2 import id_token

from themis.litcache import anthropic_ocr, ocr
from themis.litcache import produce as produce_mod
from themis.services.convert_worker import handler as handler_mod

_BUCKET: web.AppKey[storage.Bucket] = web.AppKey('bucket', storage.Bucket)
_CONVERT_PDF: web.AppKey[ocr.PdfConverter] = web.AppKey('convert_pdf')

# The Claude API audience: the identity token is minted for it, the federation rule matches on it,
# and the exchanged Anthropic token is bound to it.
_ANTHROPIC_AUDIENCE = 'https://api.anthropic.com'


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


def _identity_token() -> str:
    """Mint the Google identity token the SDK exchanges, from the Cloud Run metadata server.

    `fetch_id_token` reads the metadata identity endpoint with `format=full`, as the web app's Path B
    does (`docs/runbooks/claude-api-wif.md`).

    Raises:
        google.auth.exceptions.GoogleAuthError: No metadata server, or it was unreachable or refused.
            Not an `anthropic` error, so the SDK wraps it as `anthropic.APIConnectionError`, which is
            transient — the paper is retried, not settled.
    """
    # The session is owned rather than left to `Request.__del__`. The cast is google-auth's loose
    # return annotation: `fetch_id_token` either returns the token or raises.
    with requests.Session() as session:
        return typing.cast('str', id_token.fetch_id_token(google_auth_requests.Request(session), _ANTHROPIC_AUDIENCE))


def _converter_from_env() -> ocr.PdfConverter:
    """Bind the Claude converter to the worker's Anthropic federation identity.

    The four identifiers are plaintext ids, not credentials (`docs/runbooks/claude-api-wif.md`). They
    are read here rather than at the call so a revision missing one fails its startup probe.
    """
    credentials = functools.partial(
        anthropic_credentials.WorkloadIdentityCredentials,
        identity_token_provider=_identity_token,
        federation_rule_id=_require('ANTHROPIC_FEDERATION_RULE_ID'),
        organization_id=_require('ANTHROPIC_ORGANIZATION_ID'),
        service_account_id=_require('ANTHROPIC_SERVICE_ACCOUNT_ID'),
        workspace_id=_require('ANTHROPIC_WORKSPACE_ID'),
    )
    return functools.partial(anthropic_ocr.convert_pdf, credentials=credentials)


async def _on_startup(app: web.Application) -> None:
    # Env reads before the bucket probe, so a missing identifier is reported as itself rather than
    # behind a network failure.
    app[_CONVERT_PDF] = _converter_from_env()
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
