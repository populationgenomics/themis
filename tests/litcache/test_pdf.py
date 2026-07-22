"""Tests for `themis.litcache.pdf` over the committed pdf fixtures.

The text-layer probe distinguishes a pdf with a recoverable text layer
(`nonoa/source.pdf`) from an image-only pdf (`image_only/source.pdf`, text drawn
as pixels). The DOI harvester reads a publisher-embedded DOI out of a pdf's
document-info metadata; those cases use a hand-built minimal pdf so the metadata is
controlled (the committed fixtures carry none).
"""

from __future__ import annotations

import pathlib

import pypdfium2
import pytest

from themis.litcache import pdf

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
    assert pdf.probe_has_text_layer(_TEXT_PDF.read_bytes()) is True


def test_image_only_pdf_has_no_text_layer() -> None:
    assert pdf.probe_has_text_layer(_IMAGE_ONLY_PDF.read_bytes()) is False


def test_malformed_pdf_fails_loud() -> None:
    with pytest.raises(pypdfium2.PdfiumError):
        pdf.probe_has_text_layer(b'not a pdf')


def test_doi_from_metadata_reads_an_elsevier_style_subject() -> None:
    # Elsevier stamps the DOI into the Subject; the `doi:` prefix is stripped.
    pdf_bytes = _pdf_with_metadata(Subject='Stem Cell Research 86 2025. doi:10.1016/j.scr.2025.103712', Title='A paper')
    assert pdf.doi_from_metadata(pdf_bytes) == '10.1016/j.scr.2025.103712'


def test_doi_from_metadata_reads_xmp_prism_doi() -> None:
    pdf_bytes = _pdf_with_xmp('<prism:doi>10.1182/blood.2025031593</prism:doi>')
    assert pdf.doi_from_metadata(pdf_bytes) == '10.1182/blood.2025031593'


def test_doi_from_metadata_reads_xmp_dc_identifier() -> None:
    # dc:identifier carries the `doi:` prefix, which is stripped.
    pdf_bytes = _pdf_with_xmp('<dc:identifier>doi:10.1234/dc.test.001</dc:identifier>')
    assert pdf.doi_from_metadata(pdf_bytes) == '10.1234/dc.test.001'


def test_doi_from_metadata_prefers_xmp_over_the_info_dict() -> None:
    # The authoritative XMP DOI wins over a document-info string.
    pdf_bytes = _pdf_with_xmp('<prism:doi>10.9991/xmp.a</prism:doi>', Subject='doi:10.9992/info.b')
    assert pdf.doi_from_metadata(pdf_bytes) == '10.9991/xmp.a'


def test_doi_from_metadata_none_when_no_field_carries_one() -> None:
    assert pdf.doi_from_metadata(_pdf_with_metadata(Title='A paper', Subject='no identifier here')) is None


def test_doi_from_metadata_none_for_empty_bytes() -> None:
    assert pdf.doi_from_metadata(b'') is None


def test_doi_from_metadata_none_for_the_text_fixture() -> None:
    # The committed text fixture carries no embedded DOI metadata.
    assert pdf.doi_from_metadata(_TEXT_PDF.read_bytes()) is None


def test_doi_from_metadata_malformed_pdf_fails_loud() -> None:
    with pytest.raises(pypdfium2.PdfiumError):
        pdf.doi_from_metadata(b'not a pdf')
