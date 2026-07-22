"""Determine a paper's identity from its bucket key and Docling origin.

The seed bucket (`ingest/`) holds the existing PDF corpus, converted to Docling json
on ingestion; each object is named by a URL-encoded identifier: a DOI (`10.x/…`), a
bare-digit PMID, an Elsevier PII, or an opaque key — sometimes double-encoded
(`%252F`). This module decodes and classifies that key, harvests the second id
Docling records in `origin.filename` (often a PMID when the key is a DOI, itself
sometimes a re-encoded id), and produces the external-id set the crosswalk mints
against (`litcache.crosswalk`). The Docling origin is the seed's own metadata; how
future sources are transcribed is the converter's concern, not identity's.

The id set is `{scheme}:{value}` keys — `doi:`, `pmcid:`, `pmid:`, `pii:`, or,
when no external scheme is recognised, a `binhash:` content-hash fallthrough
derived from Docling's `origin.binary_hash`. A paper with neither an external id
nor a binary hash cannot be placed and fails loud.
"""

from __future__ import annotations

import dataclasses
import json
import re
import urllib.parse
from collections.abc import Sequence

# Object names URL-encode `/` so it is safe in a flat key; a DOI's slash becomes
# %2F, double-encoded as %252F. Decode until stable (bounded) to undo either
# depth — the encoding only ever escapes the structural slash, so repeated
# unquoting cannot corrupt a real identifier.
_MAX_DECODE_DEPTH = 4

_DOI_RE = re.compile(r'^10\.\d+/')
_PMCID_RE = re.compile(r'^PMC\d+$', re.IGNORECASE)
_PMID_RE = re.compile(r'^\d+$')
# Elsevier deposits as `1-s2.0-<PII>-main`; the PII is the inner token.
_PII_WRAPPER_RE = re.compile(r'^1-s2\.0-(.+?)-main$')

# The highest-precedence scheme present becomes the claim_key (the mint primary).
_SCHEME_PRECEDENCE = ('doi', 'pmcid', 'pmid', 'pii', 'binhash')

_KEY_SUFFIXES = ('.json', '.pdf')


@dataclasses.dataclass(frozen=True)
class ExternalId:
    """One classified external identifier.

    Attributes:
        scheme: The id scheme — `doi`, `pmcid`, `pmid`, `pii`, or `binhash`.
        value: The decoded identifier, without the scheme prefix.
    """

    scheme: str
    value: str

    @property
    def key(self) -> str:
        """The `{scheme}:{value}` crosswalk mint key."""
        return f'{self.scheme}:{self.value}'


@dataclasses.dataclass(frozen=True)
class DoclingOrigin:
    """The identity-bearing fields of a Docling document's `origin`.

    Attributes:
        filename: `origin.filename` — the source-file name, often a second id.
        binary_hash: `origin.binary_hash` as a string, the source-PDF hash.
    """

    filename: str | None
    binary_hash: str | None


@dataclasses.dataclass(frozen=True)
class Identity:
    """A paper's resolved identity for minting and manifest writing.

    Attributes:
        external_ids: The classified ids, deduped and ordered by mint key. They
            are claimed together in one crosswalk transaction.
        claim_key: The precedence-primary mint key (`{scheme}:{value}`) — the
            manifest's `claim_key`.
        binary_hash: Docling's `origin.binary_hash` (source-PDF hash) as a
            string, or None when the Docling json records no origin hash.
        content_addressed: True when no external scheme was recognised and
            identity fell through to the `binhash:` content hash.
    """

    external_ids: tuple[ExternalId, ...]
    claim_key: str
    binary_hash: str | None
    content_addressed: bool

    @property
    def mint_keys(self) -> tuple[str, ...]:
        """The crosswalk mint keys (`{scheme}:{value}`), one per external id."""
        return tuple(eid.key for eid in self.external_ids)


def read_docling_origin(docling_json: bytes | str) -> DoclingOrigin:
    """Harvest the `origin` identity fields from a Docling json document.

    Args:
        docling_json: The raw DoclingDocument json (bytes or str).

    Returns:
        The `DoclingOrigin`; its fields are None when the document records no
        `origin` (or omits a field).

    Raises:
        ValueError: If `docling_json` is not valid JSON.
    """
    try:
        doc = json.loads(docling_json)
    except json.JSONDecodeError as e:
        raise ValueError('docling json is not valid JSON') from e
    origin = doc.get('origin') if isinstance(doc, dict) else None
    if not isinstance(origin, dict):
        return DoclingOrigin(filename=None, binary_hash=None)
    filename = origin.get('filename')
    binary_hash = origin.get('binary_hash')
    return DoclingOrigin(
        filename=filename if isinstance(filename, str) else None,
        binary_hash=str(binary_hash) if binary_hash is not None else None,
    )


def determine_identity(bucket_key: str, origin: DoclingOrigin, *, extra_candidates: Sequence[str] = ()) -> Identity:
    """Resolve a seed object's identity from its bucket key, Docling origin, and extras.

    Decodes and classifies the bucket key, the origin filename, and any
    `extra_candidates` (e.g. a DOI harvested from the pdf's embedded metadata), then
    falls through to a `binhash:` content hash when none names a recognised external
    scheme.

    Args:
        bucket_key: The `ingest/` object name (with or without a `.json`/`.pdf`
            suffix), URL-encoded as stored.
        origin: The harvested Docling origin (see `read_docling_origin`).
        extra_candidates: Further id candidates to classify — an id the seed carries
            outside its key/origin (e.g. `pdf.doi_from_metadata`). Unrecognised
            candidates are ignored, exactly like an unrecognised key.

    Returns:
        The `Identity`, carrying at least one external id.

    Raises:
        ValueError: If no external id is recognised and there is no
            `origin.binary_hash` to content-address against.
    """
    ids: dict[str, ExternalId] = {}
    for raw in (bucket_key, origin.filename, *extra_candidates):
        if raw is None:
            continue
        eid = _classify(_decode(_strip_suffix(raw)))
        if eid is not None:
            ids[eid.key] = eid

    content_addressed = False
    if not ids:
        if origin.binary_hash is None:
            raise ValueError(f'cannot determine identity for {bucket_key!r}: no external id and no origin.binary_hash')
        binhash = ExternalId(scheme='binhash', value=origin.binary_hash)
        ids[binhash.key] = binhash
        content_addressed = True

    external_ids = tuple(sorted(ids.values(), key=lambda e: e.key))
    return Identity(
        external_ids=external_ids,
        claim_key=_claim_key(external_ids),
        binary_hash=origin.binary_hash,
        content_addressed=content_addressed,
    )


def _strip_suffix(key: str) -> str:
    for suffix in _KEY_SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def _decode(key: str) -> str:
    decoded = key
    for _ in range(_MAX_DECODE_DEPTH):
        nxt = urllib.parse.unquote(decoded)
        if nxt == decoded:
            return nxt
        decoded = nxt
    return decoded


def _classify(decoded: str) -> ExternalId | None:
    if _DOI_RE.match(decoded):
        return ExternalId(scheme='doi', value=decoded)
    if _PMCID_RE.match(decoded):
        return ExternalId(scheme='pmcid', value=decoded.upper())
    if _PMID_RE.match(decoded):
        return ExternalId(scheme='pmid', value=decoded)
    pii = _PII_WRAPPER_RE.match(decoded)
    if pii is not None:
        return ExternalId(scheme='pii', value=pii.group(1))
    return None


def _claim_key(external_ids: tuple[ExternalId, ...]) -> str:
    def precedence(eid: ExternalId) -> int:
        if eid.scheme in _SCHEME_PRECEDENCE:
            return _SCHEME_PRECEDENCE.index(eid.scheme)
        return len(_SCHEME_PRECEDENCE)

    return min(external_ids, key=precedence).key
