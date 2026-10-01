"""What `serving.EvidenceServicer` gives every interface that mixes it in: the deadline and the status mapping.

Driven over gnomad's servicer on a real, gated server, and gnomad stands for all of them: the base class
is what is under test, and every database-backed interface takes the whole of it. Admission is the
interceptor's and is pinned in `themis.clients.auth.tests`; nothing here asserts on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import ssl
from collections.abc import AsyncIterator
from typing import override

import grpc
import grpc.aio
import httpx2
import pytest

from themis.common import authored_status
from themis.rpc import gnomad_pb2, gnomad_pb2_grpc
from themis.services.evidence import errors, serving
from themis.services.evidence.gnomad import backend as gnomad_backend
from themis.services.evidence.gnomad import servicer as servicer_mod
from themis.services.evidence.tests import authz


class _RaisingBackend(gnomad_backend.GnomadBackend):
    """Fails its one call with `error`."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    @override
    async def describe_variant(self, request: gnomad_pb2.DescribeVariantRequest) -> gnomad_pb2.DescribeVariantResponse:
        raise self._error


class _StalledBackend(gnomad_backend.GnomadBackend):
    """Never answers, and records the cancellation the rpc's deadline delivers to it."""

    def __init__(self) -> None:
        self.cancelled = False

    @override
    async def describe_variant(self, request: gnomad_pb2.DescribeVariantRequest) -> gnomad_pb2.DescribeVariantResponse:
        stalled = asyncio.Event()  # nothing sets it
        try:
            while True:
                await stalled.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@contextlib.asynccontextmanager
async def _serving(backend: gnomad_backend.GnomadBackend) -> AsyncIterator[gnomad_pb2_grpc.GnomadAsyncStub]:
    def register(server: grpc.aio.Server) -> None:
        gnomad_pb2_grpc.add_GnomadServicer_to_server(servicer_mod.Servicer(backend), server)

    async with authz.gated(register) as channel:
        yield gnomad_pb2_grpc.GnomadStub(channel)


def _request() -> gnomad_pb2.DescribeVariantRequest:
    """A request the servicer's own preconditions accept, so every failure here is the base class's."""
    return gnomad_pb2.DescribeVariantRequest(gnomad_id='1-100-A-T', dataset='gnomad_r4')


def _refused(backend: gnomad_backend.GnomadBackend) -> grpc.aio.AioRpcError:
    """Drive one `DescribeVariant` that is expected to fail, and hand back the status it failed with."""

    async def run() -> grpc.aio.AioRpcError:
        async with _serving(backend) as stub:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await stub.DescribeVariant(_request())
            return caught.value

    return asyncio.run(run())


# The request an upstream failure below was raised for. Its query is what a message must never carry.
_UPSTREAM_REQUEST = httpx2.Request('GET', 'https://gnomad.broadinstitute.org/api?variant=1-100-A-T&secret=q')

# Each taxonomy error, with the status it has to reach a caller under. Two share
# FAILED_PRECONDITION: a request the sources cannot settle and one they settle inconsistently are
# both well-formed questions whose answer no reissue changes.
_TAXONOMY: list[tuple[Exception, grpc.StatusCode]] = [
    (errors.UnknownVariantError('gnomAD holds no record of 1-100-A-T'), grpc.StatusCode.NOT_FOUND),
    (errors.InvalidRequestError('gnomAD rejected 1-100-A-T (400)'), grpc.StatusCode.INVALID_ARGUMENT),
    (errors.UnresolvedEntityError('two curated entities sit under MONDO:0007254'), grpc.StatusCode.FAILED_PRECONDITION),
    (
        errors.InconsistentSourcesError("ClinVar holds no record under accession 'VCV000704508'"),
        grpc.StatusCode.FAILED_PRECONDITION,
    ),
    (
        errors.UpstreamStatusError(
            "gnomAD returned 503 for '1-100-A-T': busy",
            request=_UPSTREAM_REQUEST,
            response=httpx2.Response(503, request=_UPSTREAM_REQUEST),
            upstream='gnomAD',
        ),
        grpc.StatusCode.UNAVAILABLE,
    ),
]


@pytest.mark.parametrize(('error', 'code'), _TAXONOMY, ids=[type(error).__name__ for error, _ in _TAXONOMY])
def test_a_taxonomy_error_reaches_the_caller_as_its_own_status(error: Exception, code: grpc.StatusCode) -> None:
    """Each `errors` type carries a distinct meaning, and the status is what carries it over the wire.

    NOT_FOUND most of all: absence from gnomAD is the POP_FRQ rarity finding and absence from MaveDB
    is "no assay exists", so answering either with UNKNOWN reads as an outage and is retried against a
    question the source has already settled.
    """
    failure = _refused(_RaisingBackend(error))
    assert failure.code() == code
    assert str(error) in (failure.details() or '')


def test_every_taxonomy_error_is_mapped_to_a_status() -> None:
    """Listing the mapped types proves nothing on its own — nothing forces a new one into the list.

    So the set is derived from `errors` instead: a type added there and not mapped here surfaces as
    UNKNOWN, which a caller's retry helper reissues four times against an answer that cannot change.
    """
    defined = {
        value
        for name, value in vars(errors).items()
        if not name.startswith('_') and inspect.isclass(value) and issubclass(value, Exception)
    }
    assert defined == {type(error) for error, _ in _TAXONOMY}


def test_a_backend_that_never_answers_ends_as_this_rpcs_own_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overrun is this service's own status naming the rpc, and the work behind it is dropped.

    A caller that has given up is not owed the instance the composition still holds, so the backend
    has to see its cancellation rather than run on to completion.
    """
    monkeypatch.setattr(serving, '_RPC_DEADLINE_S', 0.05)
    backend = _StalledBackend()

    async def run() -> tuple[grpc.aio.AioRpcError, bool]:
        async with _serving(backend) as stub:
            with pytest.raises(grpc.aio.AioRpcError) as caught:
                await stub.DescribeVariant(_request())
            # Read while the loop still runs: `asyncio.run` cancels whatever is left at teardown, so a
            # flag read after it cannot tell a dropped backend from an abandoned one.
            return caught.value, backend.cancelled

    failure, cancelled = asyncio.run(run())
    assert failure.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    assert 'DescribeVariant' in (failure.details() or '')
    assert authored_status.authored(failure)
    assert cancelled


def _response(status: int, body: str = '') -> httpx2.Response:
    return httpx2.Response(status, text=body, request=_UPSTREAM_REQUEST)


def _raised(status: int, body: str) -> errors.UpstreamStatusError:
    """What `raise_for_status` raises for this status and body, over the query-carrying request."""
    with pytest.raises(errors.UpstreamStatusError) as caught:
        errors.raise_for_status(_response(status, body), upstream='gnomAD', subject="'1-100-A-T'")
    return caught.value


def _tls_refused() -> httpx2.ConnectError:
    """A ConnectError as httpx raises one for a certificate that fails verification: chained from the ssl error."""
    try:
        try:
            raise ssl.SSLCertVerificationError(1, 'certificate verify failed: certificate has expired')
        except ssl.SSLCertVerificationError as e:
            raise httpx2.ConnectError('certificate verify failed', request=_UPSTREAM_REQUEST) from e
    except httpx2.ConnectError as refused:
        return refused


# Each way an upstream fails that a reissue may clear, and what the message must state about it.
_TRANSIENT: list[tuple[Exception, str]] = [
    (
        httpx2.ReadTimeout('', request=_UPSTREAM_REQUEST),
        'gnomad.broadinstitute.org did not answer in time (ReadTimeout); a timeout is usually transient',
    ),
    (httpx2.ConnectError('refused', request=_UPSTREAM_REQUEST), 'the connection to gnomad.broadinstitute.org failed'),
    (
        httpx2.RemoteProtocolError('peer closed', request=_UPSTREAM_REQUEST),
        'the connection to gnomad.broadinstitute.org failed',
    ),
    (
        httpx2.HTTPStatusError('Server error', request=_UPSTREAM_REQUEST, response=_response(502)),
        'gnomad.broadinstitute.org returned 502; a 5xx is usually transient',
    ),
    (_raised(503, 'busy'), "gnomAD returned 503 for '1-100-A-T': busy; a 5xx is usually transient"),
    (_raised(429, 'slow down'), "gnomAD returned 429 for '1-100-A-T': slow down; a 429 is the upstream limiting"),
]


@pytest.mark.parametrize(('error', 'stated'), _TRANSIENT, ids=[type(error).__name__ for error, _ in _TRANSIENT])
def test_an_upstream_failure_a_reissue_may_clear_is_unavailable_stating_what_it_returned(
    error: Exception, stated: str
) -> None:
    """The caller learns which upstream failed and how, rather than an UNKNOWN naming nothing.

    The details are marked as the servicer's, which is what lets the sandbox hatch pass them to the
    guest. The request's URL stays out: every failure here was raised over one whose path and query
    are distinctive, so a message quoting it would carry them.
    """
    failure = _refused(_RaisingBackend(error))
    assert failure.code() == grpc.StatusCode.UNAVAILABLE
    details = failure.details() or ''
    assert details.startswith('DescribeVariant: ')
    assert stated in details
    assert 'secret' not in details
    assert '/api' not in details
    assert authored_status.authored(failure)


def test_our_egress_address_never_reaches_the_caller() -> None:
    """NCBI's rate-limit answer names the caller's address under `api-key` when no key is sent."""
    body = '{"error":"API rate limit exceeded","api-key":"203.0.113.7","count":"4","limit":"3"}'
    failure = _refused(_RaisingBackend(_raised(429, body)))
    details = failure.details() or ''
    assert failure.code() == grpc.StatusCode.UNAVAILABLE
    assert 'API rate limit exceeded' in details
    assert '203.0.113.7' not in details


# Upstream failures a reissue cannot clear, or that are ours: they stay uncharacterised.
_NOT_TRANSIENT: list[Exception] = [
    httpx2.HTTPStatusError('Not found', request=_UPSTREAM_REQUEST, response=_response(404)),
    errors.UpstreamStatusError(
        "gnomAD returned 302 for '1-100-A-T'", request=_UPSTREAM_REQUEST, response=_response(302), upstream='gnomAD'
    ),
    httpx2.LocalProtocolError('bad header', request=_UPSTREAM_REQUEST),
    httpx2.UnsupportedProtocol('no scheme', request=_UPSTREAM_REQUEST),
    httpx2.PoolTimeout('every connection is in use', request=_UPSTREAM_REQUEST),
    _tls_refused(),
]


@pytest.mark.parametrize('error', _NOT_TRANSIENT, ids=[type(error).__name__ for error in _NOT_TRANSIENT])
def test_an_upstream_failure_no_reissue_clears_stays_uncharacterised(error: Exception) -> None:
    """Reported as UNAVAILABLE, a fault of ours would read as an outage to wait out.

    A pool timeout is this service's own saturation, before any upstream was asked; a certificate
    that fails verification is our trust store or the upstream's certificate, and neither passes.
    """
    failure = _refused(_RaisingBackend(error))
    assert failure.code() == grpc.StatusCode.UNKNOWN
    assert not authored_status.authored(failure)


def test_a_failure_raised_with_no_request_is_still_reported() -> None:
    """An error raised with no request still reports: httpx raises RuntimeError for an unset `.request`."""
    failure = _refused(_RaisingBackend(httpx2.ReadTimeout('stalled')))
    assert failure.code() == grpc.StatusCode.UNAVAILABLE
    assert 'an upstream did not answer in time' in (failure.details() or '')


@pytest.mark.parametrize(('error', 'code'), _TAXONOMY, ids=[type(error).__name__ for error, _ in _TAXONOMY])
def test_every_status_the_servicer_writes_is_marked_as_its_own(error: Exception, code: grpc.StatusCode) -> None:
    """The hatch passes on only marked details, so an unmarked answer would reach the guest as a bare code."""
    failure = _refused(_RaisingBackend(error))
    assert failure.code() == code
    assert authored_status.authored(failure)


def _serving_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records if record.name == serving.__name__ and record.levelno == logging.WARNING
    ]


@pytest.mark.parametrize(
    ('error', 'upstream'),
    [
        (httpx2.ConnectError('refused', request=_UPSTREAM_REQUEST), 'gnomad.broadinstitute.org'),
        (_raised(503, 'busy'), 'gnomAD'),
    ],
    ids=['by-host', 'by-label'],
)
def test_an_upstream_fault_mapped_to_unavailable_is_logged_once_with_its_traceback(
    error: Exception, upstream: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Aborting ends the rpc cleanly, so grpc logs nothing: without this line an outage leaves no server-side trace."""
    caplog.set_level(logging.WARNING)
    _refused(_RaisingBackend(error))
    [record] = _serving_warnings(caplog)
    line = record.getMessage()
    assert line.startswith(f'DescribeVariant: upstream {upstream} failed ({type(error).__name__}): ')
    assert 'secret' not in line
    assert '/api' not in line
    assert record.exc_info is not None
    assert record.exc_info[1] is error


def test_the_logged_line_is_scrubbed_like_the_status_text(caplog: pytest.LogCaptureFixture) -> None:
    body = '{"error":"API rate limit exceeded","api-key":"203.0.113.7","count":"4","limit":"3"}'
    caplog.set_level(logging.WARNING)
    _refused(_RaisingBackend(_raised(429, body)))
    [record] = _serving_warnings(caplog)
    assert '203.0.113.7' not in record.getMessage()
    assert 'API rate limit exceeded' in record.getMessage()


@pytest.mark.parametrize('error', _NOT_TRANSIENT, ids=[type(error).__name__ for error in _NOT_TRANSIENT])
def test_a_fault_left_unknown_is_not_logged_here(error: Exception, caplog: pytest.LogCaptureFixture) -> None:
    """An unhandled servicer exception is already an ERROR with its traceback in grpc's log; this would double it."""
    caplog.set_level(logging.WARNING)
    _refused(_RaisingBackend(error))
    assert _serving_warnings(caplog) == []
