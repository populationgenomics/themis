"""Tests for `themis.litcache.pdf` over the committed pdf fixtures.

The text-layer probe distinguishes a pdf with a recoverable text layer
(`nonoa/source.pdf`) from an image-only pdf (`image_only/source.pdf`, text drawn
as pixels). The DOI harvester reads a publisher-embedded DOI out of a pdf's
document-info metadata; those cases use a hand-built minimal pdf so the metadata is
controlled (the committed fixtures carry none).
"""

from __future__ import annotations

import concurrent.futures
import datetime
import os
import pathlib
import signal
import time
from collections.abc import Callable, Iterator
from typing import override

import pypdfium2
import pytest

from themis.litcache import anthropic_ocr, pdf

_FIXTURES = pathlib.Path(__file__).resolve().parents[1] / 'fixtures' / 'litcache'
_TEXT_PDF = _FIXTURES / 'nonoa' / 'source.pdf'
_IMAGE_ONLY_PDF = _FIXTURES / 'image_only' / 'source.pdf'


def _pdf_with_metadata(**info: str) -> bytes:
    """A minimal one-page pdf whose Info dictionary carries the given fields."""
    objs = [
        b'<</Type/Catalog/Pages 2 0 R>>',
        b'<</Type/Pages/Kids[3 0 R]/Count 1>>',
        b'<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>',
        ('<<' + ''.join(f'/{key}({value})' for key, value in info.items()) + '>>').encode('latin-1'),
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref = len(out)
    size = len(objs) + 1
    out += f'xref\n0 {size}\n'.encode() + b'0000000000 65535 f \n'
    for offset in offsets:
        out += f'{offset:010d} 00000 n \n'.encode()
    out += f'trailer\n<</Size {size}/Root 1 0 R/Info 4 0 R>>\nstartxref\n{xref}\n%%EOF'.encode()
    return bytes(out)


def _pdf_with_xmp(xmp_inner: str, **info: str) -> bytes:
    """A minimal one-page pdf with an XMP `/Metadata` packet (plus optional Info fields)."""
    xmp = (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
        'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<rdf:Description>{xmp_inner}</rdf:Description>'
        '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    ).encode('latin-1')
    info_obj = ('<<' + ''.join(f'/{key}({value})' for key, value in info.items()) + '>>').encode('latin-1')
    objs = [
        b'<</Type/Catalog/Pages 2 0 R/Metadata 5 0 R>>',
        b'<</Type/Pages/Kids[3 0 R]/Count 1>>',
        b'<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>',
        info_obj,
        b'<</Type/Metadata/Subtype/XML/Length ' + str(len(xmp)).encode() + b'>>\nstream\n' + xmp + b'\nendstream',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref = len(out)
    size = len(objs) + 1
    out += f'xref\n0 {size}\n'.encode() + b'0000000000 65535 f \n'
    for offset in offsets:
        out += f'{offset:010d} 00000 n \n'.encode()
    out += f'trailer\n<</Size {size}/Root 1 0 R/Info 4 0 R>>\nstartxref\n{xref}\n%%EOF'.encode()
    return bytes(out)


def test_text_pdf_has_text_layer() -> None:
    assert pdf.probe_has_text_layer(_TEXT_PDF.read_bytes()).result() is True


def test_image_only_pdf_has_no_text_layer() -> None:
    assert pdf.probe_has_text_layer(_IMAGE_ONLY_PDF.read_bytes()).result() is False


def test_malformed_pdf_fails_loud() -> None:
    with pytest.raises(pypdfium2.PdfiumError):
        pdf.probe_has_text_layer(b'not a pdf').result()


def test_leading_text_reads_a_pdf_shorter_than_asked_whole() -> None:
    assert pdf.leading_text(_TEXT_PDF.read_bytes(), pages=5).result().startswith('Synthetic Non-OA Fixture')


def test_leading_text_of_an_image_only_pdf_is_empty() -> None:
    assert pdf.leading_text(_IMAGE_ONLY_PDF.read_bytes(), pages=2).result() == ''


def test_doi_from_metadata_reads_an_elsevier_style_subject() -> None:
    # Elsevier stamps the DOI into the Subject; the `doi:` prefix is stripped.
    pdf_bytes = _pdf_with_metadata(Subject='Stem Cell Research 86 2025. doi:10.1016/j.scr.2025.103712', Title='A paper')
    assert pdf.doi_from_metadata(pdf_bytes).result() == '10.1016/j.scr.2025.103712'


def test_doi_from_metadata_reads_xmp_prism_doi() -> None:
    pdf_bytes = _pdf_with_xmp('<prism:doi>10.1182/blood.2025031593</prism:doi>')
    assert pdf.doi_from_metadata(pdf_bytes).result() == '10.1182/blood.2025031593'


def test_doi_from_metadata_reads_xmp_dc_identifier() -> None:
    # dc:identifier carries the `doi:` prefix, which is stripped.
    pdf_bytes = _pdf_with_xmp('<dc:identifier>doi:10.1234/dc.test.001</dc:identifier>')
    assert pdf.doi_from_metadata(pdf_bytes).result() == '10.1234/dc.test.001'


def test_doi_from_metadata_unescapes_an_xmp_sici_doi() -> None:
    sici = '10.1002/(SICI)1097-4636(199701)35:1&lt;3::AID-JBM1&gt;3.0.CO;2-M'
    pdf_bytes = _pdf_with_xmp(f'<prism:doi>{sici}</prism:doi>')

    assert pdf.doi_from_metadata(pdf_bytes).result() == '10.1002/(SICI)1097-4636(199701)35:1<3::AID-JBM1>3.0.CO;2-M'


def test_doi_from_metadata_prefers_xmp_over_the_info_dict() -> None:
    # The authoritative XMP DOI wins over a document-info string.
    pdf_bytes = _pdf_with_xmp('<prism:doi>10.9991/xmp.a</prism:doi>', Subject='doi:10.9992/info.b')
    assert pdf.doi_from_metadata(pdf_bytes).result() == '10.9991/xmp.a'


def test_doi_from_metadata_none_when_no_field_carries_one() -> None:
    assert pdf.doi_from_metadata(_pdf_with_metadata(Title='A paper', Subject='no identifier here')).result() is None


def test_doi_from_metadata_none_for_empty_bytes() -> None:
    assert pdf.doi_from_metadata(b'').result() is None


def test_doi_from_metadata_none_for_the_text_fixture() -> None:
    # The committed text fixture carries no embedded DOI metadata.
    assert pdf.doi_from_metadata(_TEXT_PDF.read_bytes()).result() is None


def test_doi_from_metadata_malformed_pdf_fails_loud() -> None:
    with pytest.raises(pypdfium2.PdfiumError):
        pdf.doi_from_metadata(b'not a pdf').result()


def test_concurrent_pdfium_use_agrees_with_serial_use() -> None:
    # pdfium is not thread-safe, and the pipeline submits to it from a Beam worker thread pool, so an
    # answer must not depend on how many threads submit at once. Both public entry points
    # open a document and production reaches them from different stages, so the workload interleaves
    # them. 16 threads over 32 rounds is the contention needed to fault reliably when unserialised.
    rounds = 32
    text_pdf, image_pdf = _TEXT_PDF.read_bytes(), _IMAGE_ONLY_PDF.read_bytes()
    calls: list[tuple[Callable[[bytes], object], bytes]] = [
        (lambda b: pdf.probe_has_text_layer(b).result(), text_pdf),
        (lambda b: pdf.probe_has_text_layer(b).result(), image_pdf),
        (lambda b: pdf.doi_from_metadata(b).result(), text_pdf),
        (lambda b: pdf.doi_from_metadata(b).result(), image_pdf),
    ]
    expected = [call(data) for call, data in calls]

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda c: c[0](c[1]), calls * rounds))

    assert results == expected * rounds


def _exit_the_process(_pdf_bytes: bytes) -> int:
    """A job that kills the process running it, as a pdfium segfault would."""
    os._exit(1)


def _sigkill_self(_pdf_bytes: bytes) -> int:
    """A job killed outright every time, as a render that exhausts memory would be."""
    os.kill(os.getpid(), signal.SIGKILL)
    return 0


def _sigkill_first_time(marker: pathlib.Path) -> int:
    """A job killed outright on its first run only, as one caught in someone else's memory pressure would be."""
    if not marker.exists():
        marker.touch()
        os.kill(os.getpid(), signal.SIGKILL)
    return 7


def _sleep(seconds: float) -> int:
    time.sleep(seconds)
    return 0


class _Unbuildable:
    """An argument that pickles here and fails to unpickle in the pdfium process."""

    @override
    def __reduce__(self) -> tuple[Callable[[], object], tuple[()]]:
        return (_refuse_to_rebuild, ())


def _refuse_to_rebuild() -> object:
    raise ValueError('cannot be rebuilt')


def _die_before_ready(_conn: object) -> None:
    """A child that fails while it starts, as one with a broken environment would."""
    os._exit(3)


@pytest.fixture
def pdfium(monkeypatch: pytest.MonkeyPatch) -> Iterator[pdf._PdfiumProcess]:  # pyright: ignore[reportPrivateUsage]
    """A fresh pdfium runner of the module's own, so a test that breaks its child breaks nothing else."""
    runner = pdf._PdfiumProcess()  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(pdf, '_PDFIUM', runner)
    yield runner
    if runner._child is not None:  # pyright: ignore[reportPrivateUsage]
        runner._child.stop(exiting=False)  # pyright: ignore[reportPrivateUsage]


def test_a_job_that_kills_pdfium_fails_alone_and_the_next_job_gets_a_fresh_process(
    pdfium: pdf._PdfiumProcess,  # pyright: ignore[reportPrivateUsage]
) -> None:
    text_pdf = _TEXT_PDF.read_bytes()
    before = pdf.page_count(text_pdf)
    crashed = pdfium.submit(_exit_the_process, text_pdf)
    after = [pdf.page_count(text_pdf) for _ in range(3)]

    with pytest.raises(pdf.PdfiumCrashedError, match='died running this job'):
        crashed.result(timeout=60)
    assert {f.result(timeout=60) for f in [before, *after]} == {before.result()}


def test_a_job_killed_outright_once_is_run_again_rather_than_blamed(
    pdfium: pdf._PdfiumProcess,  # pyright: ignore[reportPrivateUsage]
    tmp_path: pathlib.Path,
) -> None:
    assert pdfium.submit(_sigkill_first_time, tmp_path / 'killed-once').result(timeout=120) == 7


def test_a_job_killed_outright_every_time_is_blamed(
    pdfium: pdf._PdfiumProcess,  # pyright: ignore[reportPrivateUsage]
) -> None:
    with pytest.raises(pdf.PdfiumCrashedError, match='died running this job'):
        pdfium.submit(_sigkill_self, b'').result(timeout=120)


def test_a_job_that_hangs_pdfium_is_blamed_and_the_next_job_runs(
    pdfium: pdf._PdfiumProcess,  # pyright: ignore[reportPrivateUsage]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, '_JOB_TIMEOUT', datetime.timedelta(seconds=1))
    with pytest.raises(pdf.PdfiumCrashedError, match='did not finish'):
        pdfium.submit(_sleep, 60).result(timeout=120)
    assert pdf.page_count(_TEXT_PDF.read_bytes()).result(timeout=60) > 0


@pytest.mark.usefixtures('pdfium')
def test_a_child_that_fails_to_start_says_nothing_about_the_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A broken environment must fail the request to be retried; were it a PdfiumError, every paper
    # asked for would settle as an unreadable pdf.
    monkeypatch.setattr(pdf, '_CHILD_TARGET', _die_before_ready)
    with pytest.raises(pdf.PdfiumUnavailableError) as raised:
        pdf.page_count(_TEXT_PDF.read_bytes()).result(timeout=120)
    assert not isinstance(raised.value, pypdfium2.PdfiumError)


@pytest.mark.usefixtures('pdfium')
def test_a_failure_to_start_a_child_leaves_the_runner_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = pdf._Child.start  # pyright: ignore[reportPrivateUsage]

    def refuse() -> pdf._Child:  # pyright: ignore[reportPrivateUsage]
        raise pdf.PdfiumUnavailableError('no processes left')

    monkeypatch.setattr(pdf._Child, 'start', refuse)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(pdf.PdfiumUnavailableError):
        pdf.page_count(_TEXT_PDF.read_bytes()).result(timeout=60)
    monkeypatch.setattr(pdf._Child, 'start', original)  # pyright: ignore[reportPrivateUsage]
    assert pdf.page_count(_TEXT_PDF.read_bytes()).result(timeout=60) > 0


@pytest.mark.usefixtures('pdfium')
def test_a_job_that_will_not_pickle_fails_alone() -> None:
    def closure_fit(_w: float, _h: float) -> tuple[int, int]:
        return (10, 10)

    with pytest.raises(Exception, match=r'pickle|local object'):
        pdf.render_pages(_TEXT_PDF.read_bytes(), fit=closure_fit).result(timeout=60)
    assert pdf.page_count(_TEXT_PDF.read_bytes()).result(timeout=60) > 0


@pytest.mark.usefixtures('pdfium')
def test_a_job_that_will_not_unpickle_in_the_child_fails_alone_and_is_not_blamed_on_the_pdf() -> None:
    with pytest.raises(RuntimeError, match='could not be rebuilt') as raised:
        pdf.render_pages(_TEXT_PDF.read_bytes(), fit=_Unbuildable()).result(timeout=60)  # pyright: ignore[reportArgumentType]
    assert not isinstance(raised.value, pypdfium2.PdfiumError)
    assert pdf.page_count(_TEXT_PDF.read_bytes()).result(timeout=60) > 0


def test_a_crash_is_a_pdfium_error_so_an_unloadable_pdf_s_handling_covers_it() -> None:
    assert issubclass(pdf.PdfiumCrashedError, pypdfium2.PdfiumError)


def test_every_job_and_its_answer_cross_the_process_boundary() -> None:
    # The fit travels to the pdfium process with the render job; a closure would not pickle.
    pages = pdf.render_pages(_TEXT_PDF.read_bytes(), fit=anthropic_ocr._PageFit(max_edge=400)).result()  # pyright: ignore[reportPrivateUsage]
    assert pages
    assert all(max(page.width, page.height) <= 400 for page in pages)
