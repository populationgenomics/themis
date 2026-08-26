"""The status-to-taxonomy rule itself, apart from the adapters that apply it."""

from __future__ import annotations

import json
import urllib.parse

import httpx
import pytest

from themis.services.evidence import errors


def _response(status: int, *, body: str = 'upstream said no') -> httpx.Response:
    return httpx.Response(status, text=body, request=httpx.Request('GET', 'https://upstream.example/q'))


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 422])
def test_a_non_429_client_error_is_a_refusal(status: int) -> None:
    with pytest.raises(errors.InvalidRequestError):
        errors.raise_for_status(_response(status), upstream='Source', subject="'v'")


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_a_throttle_or_a_server_fault_stays_retryable(status: int) -> None:
    """429 sits inside the 4xx range but is about the caller's rate, so it is the rule's one carve-out."""
    with pytest.raises(httpx.HTTPStatusError):
        errors.raise_for_status(_response(status), upstream='Source', subject="'v'")


@pytest.mark.parametrize('status', [200, 201, 204])
def test_a_success_passes_through(status: int) -> None:
    errors.raise_for_status(_response(status), upstream='Source', subject="'v'")


def test_the_upstreams_own_explanation_reaches_the_message() -> None:
    """The status says a retry is pointless; only the body says what to change."""
    with pytest.raises(errors.InvalidRequestError, match='Unable to parse HGVS notation'):
        errors.raise_for_status(_response(400, body='Unable to parse HGVS notation'), upstream='Source', subject="'v'")


def _trailer_cost(text: str) -> int:
    """What a message costs as `grpc-message`, which carries percent-encoded UTF-8."""
    return len(urllib.parse.quote(text, errors='replace'))


# ASCII, and a 4-byte character that percent-encodes to twelve — the case a character bound misses.
_OVERSIZED = ['x' * 40_000, '\U0001f9ec' * 40_000]


@pytest.mark.parametrize('filler', _OVERSIZED)
@pytest.mark.parametrize('status', [400, 503])
def test_neither_interpolated_part_can_blow_the_trailer_budget(status: int, filler: str) -> None:
    """The message becomes a gRPC trailer, and an over-limit trailer is dropped whole — losing the fault.

    Both parts are caller-sized: a batched E-utilities subject carries hundreds of joined UIDs, and
    an upstream that answers a 4xx with an HTML error page carries the page.
    """
    caught: pytest.ExceptionInfo[Exception]
    with pytest.raises((errors.InvalidRequestError, httpx.HTTPStatusError)) as caught:
        errors.raise_for_status(_response(status, body=filler), upstream='Source', subject=filler)
    assert _trailer_cost(str(caught.value)) < 2_000


@pytest.mark.parametrize('filler', _OVERSIZED)
@pytest.mark.parametrize('error', [errors.InvalidRequestError, errors.UnknownVariantError])
def test_a_taxonomy_error_is_bounded_wherever_it_is_raised(error: type[Exception], filler: str) -> None:
    """The bound is a property of the type, not of `raise_for_status`, and is counted in wire bytes.

    Most of these are raised straight from a precondition or a parser, echoing back a request field
    or a value read off an upstream body — neither bounded, and neither guaranteed ASCII. A
    per-call-site rule holds only until the next raise site forgets it; a character bound holds only
    until the first non-ASCII input.
    """
    assert _trailer_cost(str(error(f'the caller sent {filler}'))) < 2_100


@pytest.mark.parametrize('limit', [16, 512])
def test_the_cut_marker_is_inside_the_budget_not_added_to_it(limit: int) -> None:
    assert _trailer_cost(errors.clipped('\U0001f9ec' * 500, limit)) <= limit


def test_a_body_that_will_not_encode_still_produces_a_message() -> None:
    r"""A JSON body can decode to a lone surrogate: `json.loads('{"e": "\\ud800"}')` yields one.

    Encoding it raises, so a message *about* a bad payload would become an uncaught exception —
    UNKNOWN, with the diagnosis lost, which is what clipping exists to prevent.
    """
    lone_surrogate = json.loads('{"error": "bad \\ud800 payload"}')['error']
    assert errors.clipped(lone_surrogate)
    assert str(errors.InvalidRequestError(lone_surrogate * 5_000))
