"""The status-to-taxonomy rule itself, apart from the adapters that apply it."""

from __future__ import annotations

import json
import urllib.parse

import httpx2
import pytest

from themis.common import trailer
from themis.services.evidence import errors


def _response(status: int, *, body: str = 'upstream said no') -> httpx2.Response:
    return httpx2.Response(status, text=body, request=httpx2.Request('GET', 'https://upstream.example/q'))


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 422])
def test_a_non_429_client_error_is_a_refusal(status: int) -> None:
    with pytest.raises(errors.InvalidRequestError):
        errors.raise_for_status(_response(status), upstream='Source', subject="'v'")


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_a_throttle_or_a_server_fault_stays_retryable(status: int) -> None:
    """429 sits inside the 4xx range but is about the caller's rate, so it is the rule's one carve-out."""
    with pytest.raises(errors.UpstreamStatusError):
        errors.raise_for_status(_response(status), upstream='Source', subject="'v'")


@pytest.mark.parametrize('status', [200, 201, 204])
def test_a_success_passes_through(status: int) -> None:
    errors.raise_for_status(_response(status), upstream='Source', subject="'v'")


def test_the_upstreams_own_explanation_reaches_the_message() -> None:
    """The status says a retry is pointless; only the body says what to change."""
    with pytest.raises(errors.InvalidRequestError, match='Unable to parse HGVS notation'):
        errors.raise_for_status(_response(400, body='Unable to parse HGVS notation'), upstream='Source', subject="'v'")


_PAGE = '<!doctype html><html><head><script>var x;</script></head><body>Oops</body></html>'


def _page(status: int, *, declared: bool) -> httpx2.Response:
    """An HTML error page, declared as one by its content type or served as `text/plain`."""
    request = httpx2.Request('GET', 'https://upstream.example/q')
    if declared:
        return httpx2.Response(status, html=_PAGE, request=request)
    return httpx2.Response(status, text=f'\n  {_PAGE}', request=request)


@pytest.mark.parametrize('declared', [True, False], ids=['text/html', 'sniffed'])
@pytest.mark.parametrize(
    ('status', 'raised'), [(404, errors.InvalidRequestError), (503, errors.UpstreamStatusError)], ids=['404', '503']
)
def test_an_html_error_page_is_named_not_quoted(status: int, raised: type[Exception], declared: bool) -> None:
    """A load balancer's or a web framework's error page says nothing a caller can act on, at trailer cost."""
    with pytest.raises(raised) as caught:
        errors.raise_for_status(_page(status, declared=declared), upstream='Source', subject="'v'")
    assert str(caught.value).endswith(': an HTML error page')


def test_an_html_page_an_adapter_passes_as_its_detail_is_named_too() -> None:
    """An adapter's detail is its own reading of the body, which falls back to the body where no envelope held one."""
    with pytest.raises(errors.UpstreamStatusError) as caught:
        errors.raise_for_status(_page(502, declared=False), upstream='Source', subject="'v'", detail=_PAGE)
    assert str(caught.value) == "Source returned 502 for 'v': an HTML error page"


def test_ncbis_rate_limit_answer_loses_the_address_it_names() -> None:
    """Measured: with no key sent, NCBI names the caller's own address under `api-key`, which is our egress."""
    body = '{"error":"API rate limit exceeded","api-key":"203.0.113.7","count":"4","limit":"3"}'
    with pytest.raises(errors.UpstreamStatusError) as caught:
        errors.raise_for_status(_response(429, body=body), upstream='NCBI E-utilities', subject="'VCV000042875'")
    message = str(caught.value)
    assert '203.0.113.7' not in message
    assert '"api-key":"<withheld>"' in message
    assert 'API rate limit exceeded' in message


@pytest.mark.parametrize(
    ('quoted', 'masked'),
    [
        ('10.4.0.7', '<address>'),
        ('10.4.0.7:443', '<address>:443'),
        ('2001:db8::1', '<address>'),
        ('fe80::1ff:fe23:4567:890a', '<address>'),
        ('2001:0db8:85a3:0000:0000:8a2e:0370:7334', '<address>'),
        ('[2001:db8::1]:443', '[<address>]:443'),
        ('::ffff:10.0.0.1', '<address>'),
        ('(203.0.113.7).', '(<address>).'),
        ('IP:203.0.113.7', 'IP:<address>'),
        ('remote_addr:203.0.113.7', 'remote_addr:<address>'),
        ('x::ffff:10.0.0.1', 'x::ffff:<address>'),
    ],
)
def test_an_address_an_upstream_quotes_is_masked(quoted: str, masked: str) -> None:
    with pytest.raises(errors.UpstreamStatusError) as caught:
        errors.raise_for_status(_response(503, body=f'upstream {quoted} overloaded'), upstream='S', subject="'v'")
    assert str(caught.value) == f"S returned 503 for 'v': upstream {masked} overloaded"


@pytest.mark.parametrize(
    'text',
    [
        'down since 10:12:45',
        'VEP 116.0.1',
        'v1.2.3.4',
        'EC 3.1.1.7',
        'Bio::EnsEMBL::Variation',
        'std::bad_alloc',
        'NC_012920.1:3242::G',
        'release 1.2.3.4.5',
    ],
)
def test_text_that_only_resembles_an_address_is_not_masked(text: str) -> None:
    with pytest.raises(errors.UpstreamStatusError) as caught:
        errors.raise_for_status(_response(503, body=f'failed: {text}'), upstream='S', subject="'v'")
    assert str(caught.value).endswith(f'failed: {text}')


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
    with pytest.raises((errors.InvalidRequestError, httpx2.HTTPStatusError)) as caught:
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
    assert _trailer_cost(trailer.clipped('\U0001f9ec' * 500, limit)) <= limit


def test_a_body_that_will_not_encode_still_produces_a_message() -> None:
    r"""A JSON body can decode to a lone surrogate: `json.loads('{"e": "\\ud800"}')` yields one.

    Encoding it raises, so a message *about* a bad payload would become an uncaught exception —
    UNKNOWN, with the diagnosis lost, which is what clipping exists to prevent.
    """
    lone_surrogate = json.loads('{"error": "bad \\ud800 payload"}')['error']
    assert trailer.clipped(lone_surrogate)
    assert str(errors.InvalidRequestError(lone_surrogate * 5_000))
