"""Tests for the /convert worker: the body→producer→status decision and the entrypoint's env wiring.

The status mapping runs offline against a fake producer (no GCS, no network). One integration test
drives the real producer through the handler over a fake-gcs bucket (Docker-gated) with the fetch and
PDF converter injected, proving the handler→producer→GCS path writes a rendering end to end; another
drives the assembled aiohttp app to pin the routing and the load-bearing producer-raise→500 mapping.

The Claude converter is never called: the tests assert what the entrypoint binds and what it hands
the producer, and the transcription itself is `tests/litcache/test_anthropic_ocr.py`'s. Building the
federation credential touches no network, so binding it needs no fake.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import json
import uuid

import aiohttp.test_utils
import aiohttp.web
import pytest
from anthropic.lib import credentials as anthropic_credentials
from google.api_core import exceptions as api_exceptions
from google.auth import credentials
from google.cloud import storage
from pubmed_proto import pubmed_pb2

from themis.litcache import anthropic_ocr, ocr, writer
from themis.litcache import outcome as outcome_mod
from themis.litcache import produce as produce_mod
from themis.litcache.models import litcache_pb2
from themis.services.convert_worker import __main__ as main_mod
from themis.services.convert_worker import handler as handler_mod

_DOC_ID = '9f3a0000-0000-4000-8000-000000000010'
_CAPTURED_AT = datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC)
_PDF_BYTES = b'%PDF-1.7 seed'

# The worker's Anthropic federation identity: plaintext ids, not credentials, so the deployed values
# differ only in being real (docs/runbooks/claude-api-wif.md).
_FEDERATION_ENV = {
    'ANTHROPIC_FEDERATION_RULE_ID': 'fdrl_test',
    'ANTHROPIC_ORGANIZATION_ID': '00000000-0000-4000-8000-000000000000',
    'ANTHROPIC_SERVICE_ACCOUNT_ID': 'svac_test',
    'ANTHROPIC_WORKSPACE_ID': 'wrkspc_test',
}


def _set_federation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FEDERATION_ENV.items():
        monkeypatch.setenv(name, value)


def _lazy_bucket() -> storage.Bucket:
    """A bucket handle the fake producer never touches — no network, so the status tests need no GCS."""
    client = storage.Client(project='themis-test', credentials=credentials.AnonymousCredentials())
    return client.bucket('unused')


def _producer(result: outcome_mod.Readiness, seen: list[str] | None = None) -> handler_mod.Producer:
    async def produce(_bucket: storage.Bucket, doc_id: str) -> outcome_mod.Readiness:
        if seen is not None:
            seen.append(doc_id)
        return result

    return produce


def _raising() -> handler_mod.Producer:
    async def produce(_bucket: storage.Bucket, _doc_id: str) -> outcome_mod.Readiness:
        raise RuntimeError('transient fetch failure')

    return produce


def _run(body: bytes, produce: handler_mod.Producer) -> int:
    return asyncio.run(handler_mod.process_conversion(_lazy_bucket(), body, produce=produce))


def test_ready_conversion_is_2xx() -> None:
    seen: list[str] = []
    status = _run(json.dumps({'doc_id': _DOC_ID}).encode(), _producer(outcome_mod.Readiness.READY, seen))
    assert status == 200
    assert seen == [_DOC_ID]  # the parsed doc_id reaches the producer


def test_no_full_text_terminal_is_also_2xx() -> None:
    # A terminal NO_FULL_TEXT is settled — Cloud Tasks must stop retrying, so it is 2xx like READY.
    status = _run(json.dumps({'doc_id': _DOC_ID}).encode(), _producer(outcome_mod.Readiness.NO_FULL_TEXT))
    assert status == 200


def test_non_json_body_is_bad_request() -> None:
    status = _run(b'not json', _producer(outcome_mod.Readiness.READY))
    assert status == 400


def test_body_without_doc_id_is_bad_request() -> None:
    status = _run(json.dumps({'not_doc_id': 'x'}).encode(), _producer(outcome_mod.Readiness.READY))
    assert status == 400


def test_empty_doc_id_is_bad_request() -> None:
    status = _run(json.dumps({'doc_id': ''}).encode(), _producer(outcome_mod.Readiness.READY))
    assert status == 400


def test_non_object_body_is_bad_request() -> None:
    status = _run(json.dumps(['doc_id']).encode(), _producer(outcome_mod.Readiness.READY))
    assert status == 400


def test_non_utf8_body_is_bad_request() -> None:
    # json.loads on non-UTF-8 bytes raises UnicodeDecodeError, not JSONDecodeError; both are malformed.
    status = _run(b'\xff\xfe\xff', _producer(outcome_mod.Readiness.READY))
    assert status == 400


def test_producer_failure_propagates_for_cloud_tasks_to_retry() -> None:
    # A raised producer error is not mapped to a status; it propagates so the HTTP layer 500s and
    # Cloud Tasks retries rather than the worker swallowing a transient failure as "done".
    with pytest.raises(RuntimeError, match='transient'):
        _run(json.dumps({'doc_id': _DOC_ID}).encode(), _raising())


def test_an_unknown_paper_is_settled_at_2xx() -> None:
    async def produce(_bucket: storage.Bucket, doc_id: str) -> outcome_mod.Readiness:
        raise produce_mod.UnknownPaperError(doc_id)

    assert _run(json.dumps({'doc_id': _DOC_ID}).encode(), produce) == 200


def test_a_missing_object_other_than_the_manifest_is_retried_not_dropped() -> None:
    # The seed blob a manifest names, absent from the bucket: a live paper, not an unknown one. It
    # must reach the transport as a 500 — settling it at 200 deletes the task and strands the paper
    # PENDING with no marker.
    async def produce(_bucket: storage.Bucket, _doc_id: str) -> outcome_mod.Readiness:
        raise api_exceptions.NotFound('sources/pdf/<hash>.pdf')

    with pytest.raises(api_exceptions.NotFound):
        _run(json.dumps({'doc_id': _DOC_ID}).encode(), produce)


# --- integration: the real producer through the handler over fake-gcs ---


def _pdf_source() -> writer.SourceInput:
    return writer.SourceInput(
        handle='pdf',
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
        data=_PDF_BYTES,
        licence='',
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
        access=litcache_pb2.Access(unknown=litcache_pb2.UnknownAccess()),
        captured_at=_CAPTURED_AT,
    )


def _write_pdf_paper(bucket: storage.Bucket) -> None:
    article = pubmed_pb2.PubmedArticle()
    article.medline_citation.pmid.value = '29089047'
    writer.write_paper(
        bucket,
        writer.PaperInput(
            doc_id=_DOC_ID,
            external_ids=litcache_pb2.ExternalIds(doi='10.1/abc'),
            claim_key='doi:10.1/abc',
            equivalence=litcache_pb2.Equivalence(edges=[], canonical_doc_id=_DOC_ID),
            retraction=litcache_pb2.Retraction(),
            sources=[_pdf_source()],
            renderings=[],
            metadata=article.SerializeToString(),
        ),
    )


def test_handler_runs_the_real_producer_and_commits_a_rendering(gcs_bucket: storage.Bucket) -> None:
    _write_pdf_paper(gcs_bucket)

    async def fetch(_article_ids: object, **_kwargs: object) -> None:
        return None  # no OA XML: force the PDF LLM-OCR branch

    async def convert(_pdf_bytes: bytes) -> ocr.OcrRendering:
        return ocr.OcrRendering(markdown='# OCR full text\n', model='claude-sonnet-5', converter_version='1.1')

    produce = functools.partial(
        produce_mod.produce_full_text, fetch=fetch, convert_pdf=convert, now=lambda: _CAPTURED_AT
    )
    status = asyncio.run(
        handler_mod.process_conversion(gcs_bucket, json.dumps({'doc_id': _DOC_ID}).encode(), produce=produce)
    )
    assert status == 200
    assert outcome_mod.read_readiness(gcs_bucket, _DOC_ID) is outcome_mod.Readiness.READY
    manifest = litcache_pb2.Manifest.FromString(gcs_bucket.blob(writer.manifest_path(_DOC_ID)).download_as_bytes())
    rendering = next(iter(manifest.renderings.values()))
    assert rendering.converter == litcache_pb2.Converter.CONVERTER_LLM_OCR


def test_the_assembled_app_serves_its_routes_over_the_startup_bucket(
    gcs_bucket: storage.Bucket, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drives the app `main` actually builds, startup included, so the wiring between build_app and
    # _on_startup is covered: a revision that never binds the bucket passes its startup probe and
    # then 500s every task, because /healthz answers without touching it.
    _set_federation_env(monkeypatch)
    monkeypatch.setattr(main_mod, '_bucket_from_env', lambda: gcs_bucket)
    app = main_mod.build_app()

    async def drive() -> tuple[int, int, int]:
        async with aiohttp.test_utils.TestClient(aiohttp.test_utils.TestServer(app)) as client:
            healthz = await client.get('/healthz')
            malformed = await client.post('/convert', data=b'not json')
            unknown = await client.post('/convert', data=json.dumps({'doc_id': _DOC_ID}).encode())
            return healthz.status, malformed.status, unknown.status

    healthz_status, malformed_status, unknown_status = asyncio.run(drive())

    assert healthz_status == 200
    assert malformed_status == 400
    # The corpus holds no such paper: settled, not retried, so the budget is spent only on failures
    # a later attempt could clear.
    assert unknown_status == 200


def test_a_producer_failure_reaches_the_transport_as_a_500(gcs_bucket: storage.Bucket) -> None:
    # The retry contract rests on aiohttp turning a producer raise into the 500 Cloud Tasks retries.
    app = aiohttp.web.Application()
    app[main_mod._BUCKET] = gcs_bucket

    async def convert(request: aiohttp.web.Request) -> aiohttp.web.Response:
        status = await handler_mod.process_conversion(
            request.app[main_mod._BUCKET], await request.read(), produce=_raising()
        )
        return aiohttp.web.Response(status=status)

    app.router.add_post('/convert', convert)

    async def drive() -> int:
        async with aiohttp.test_utils.TestClient(aiohttp.test_utils.TestServer(app)) as client:
            response = await client.post('/convert', data=json.dumps({'doc_id': _DOC_ID}).encode())
            return response.status

    assert asyncio.run(drive()) == 500


# --- entrypoint env wiring (fail loud) ---


def test_startup_requires_the_fulltext_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_federation_env(monkeypatch)  # valid, so startup reaches the bucket read
    monkeypatch.delenv('THEMIS_FULLTEXT_BUCKET', raising=False)
    with pytest.raises(SystemExit, match='THEMIS_FULLTEXT_BUCKET'):
        asyncio.run(main_mod._on_startup(main_mod.build_app()))


def test_startup_refuses_a_bucket_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, gcs_client: storage.Client
) -> None:
    # A named-but-absent bucket has to fail the startup probe: /healthz never touches the bucket, so a
    # revision that starts anyway goes live and 500s every task until the queue drops it.
    _set_federation_env(monkeypatch)
    monkeypatch.setenv('THEMIS_FULLTEXT_BUCKET', f'themis-absent-{uuid.uuid4().hex}')
    monkeypatch.setattr(main_mod.storage, 'Client', lambda: gcs_client)
    with pytest.raises(SystemExit, match='does not exist or is not readable'):
        asyncio.run(main_mod._on_startup(main_mod.build_app()))


@pytest.mark.parametrize('missing', sorted(_FEDERATION_ENV))
def test_startup_requires_each_anthropic_federation_id(missing: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The property the deleted unconfigured-converter stub used to hold, moved a stage earlier: a
    # worker that cannot authenticate must never serve, rather than burning a retry budget per paper.
    # Checked before the bucket read, so the missing identifier is what the operator is told.
    _set_federation_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(SystemExit, match=missing):
        asyncio.run(main_mod._on_startup(main_mod.build_app()))


# --- the Claude converter the entrypoint binds ---


def _bound_converter(monkeypatch: pytest.MonkeyPatch) -> functools.partial[object]:
    """The converter `_on_startup` binds, with the bucket read stubbed out."""
    _set_federation_env(monkeypatch)
    monkeypatch.setattr(main_mod, '_bucket_from_env', _lazy_bucket)
    app = main_mod.build_app()
    asyncio.run(main_mod._on_startup(app))
    converter = app[main_mod._CONVERT_PDF]
    assert isinstance(converter, functools.partial)
    return converter


def test_startup_binds_the_claude_converter_on_a_federation_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    converter = _bound_converter(monkeypatch)
    assert converter.func is anthropic_ocr.convert_pdf

    # A provider per call, not one shared: the client closes the provider it is handed, so a shared
    # one would be spent after the first transcription.
    with converter.keywords['credentials']() as first, converter.keywords['credentials']() as second:
        assert isinstance(first, anthropic_credentials.WorkloadIdentityCredentials)
        assert first is not second


def test_each_federation_id_reaches_the_credential_argument_it_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two of the four transposed is the worst failure this wiring has: every id is present and
    # well-formed, the revision deploys healthy, and only the first token exchange fails.
    federation = _bound_converter(monkeypatch).keywords['credentials']
    assert isinstance(federation, functools.partial)
    assert federation.keywords == {
        'identity_token_provider': main_mod._identity_token,
        'federation_rule_id': _FEDERATION_ENV['ANTHROPIC_FEDERATION_RULE_ID'],
        'organization_id': _FEDERATION_ENV['ANTHROPIC_ORGANIZATION_ID'],
        'service_account_id': _FEDERATION_ENV['ANTHROPIC_SERVICE_ACCOUNT_ID'],
        'workspace_id': _FEDERATION_ENV['ANTHROPIC_WORKSPACE_ID'],
    }


def test_the_identity_token_is_minted_for_the_claude_api(monkeypatch: pytest.MonkeyPatch) -> None:
    # The audience is half of what the federation rule matches on, and the only input to the exchange
    # that no environment variable carries — a wrong one is refused at the exchange, not at deploy.
    seen: list[str] = []

    def fetch_id_token(_request: object, audience: str) -> str:
        seen.append(audience)
        return 'header.payload.signature'

    monkeypatch.setattr(main_mod.id_token, 'fetch_id_token', fetch_id_token)

    assert main_mod._identity_token() == 'header.payload.signature'
    assert seen == ['https://api.anthropic.com']


def test_the_convert_route_hands_the_producer_the_bound_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The binding is only worth anything if it reaches the producer: /convert has to pass the app's
    # converter as produce_full_text's `convert_pdf`, not fall back to a default.
    _set_federation_env(monkeypatch)
    monkeypatch.setattr(main_mod, '_bucket_from_env', _lazy_bucket)
    app = main_mod.build_app()
    seen: list[ocr.PdfConverter] = []

    async def produce(_bucket: storage.Bucket, _doc_id: str, *, convert_pdf: ocr.PdfConverter, **_kw: object) -> None:
        seen.append(convert_pdf)
        raise produce_mod.UnknownPaperError(_doc_id)

    monkeypatch.setattr(produce_mod, 'produce_full_text', produce)

    async def drive() -> int:
        async with aiohttp.test_utils.TestClient(aiohttp.test_utils.TestServer(app)) as client:
            response = await client.post('/convert', data=json.dumps({'doc_id': _DOC_ID}).encode())
            return response.status

    assert asyncio.run(drive()) == 200
    assert seen == [app[main_mod._CONVERT_PDF]]


def test_a_converter_failure_that_is_not_terminal_leaves_the_paper_pending(gcs_bucket: storage.Bucket) -> None:
    # A revoked or misconfigured federation rule fails the token exchange, which is not an OcrError:
    # it propagates, the worker returns 5xx, Cloud Tasks retries, and the paper stays PENDING. The
    # producer settles a paper terminally for OcrError alone, and a marker would short-circuit
    # produce_full_text before it re-walks the OA ladder — litfetch reports a transient fetch failure
    # and an absent body identically, so a paper whose XML was always available would be written off
    # on the strength of our own misconfiguration.
    _write_pdf_paper(gcs_bucket)

    async def no_oa(_article_ids: object, **_kwargs: object) -> None:
        return None  # no OA XML: reach the PDF branch

    async def refused(_pdf_bytes: bytes) -> ocr.OcrRendering:
        raise anthropic_credentials.WorkloadIdentityError('invalid_grant', status_code=400)

    assert not issubclass(anthropic_credentials.WorkloadIdentityError, ocr.OcrError)
    with pytest.raises(anthropic_credentials.WorkloadIdentityError):
        asyncio.run(
            produce_mod.produce_full_text(
                gcs_bucket, _DOC_ID, fetch=no_oa, convert_pdf=refused, now=lambda: _CAPTURED_AT
            )
        )

    assert outcome_mod.read_outcome(gcs_bucket, _DOC_ID) is None
    assert outcome_mod.read_readiness(gcs_bucket, _DOC_ID) is outcome_mod.Readiness.PENDING
