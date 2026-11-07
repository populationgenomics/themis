"""The per-paper ingestion core (runtime-agnostic), split around batched resolution.

A seed object flows through two per-paper halves with a batched resolution step
between them (the runtime wrapper, `themis.litcache.ingest_beam`, wires the batch):

- `extract_identity` — the local, network-free first half: classify identity
  (`themis.litcache.identity`) from the seed's key, docling origin + embedded pdf DOI. Its
  `claim_key`, with its PMID and DOI, keys the batched resolution the write half joins
  on.
- `ingest_paper` — the write half, given the paper's pre-resolved metadata: claim a
  `doc_id` (`themis.litcache.crosswalk`) → skip if the manifest already exists →
  fetch the OA body (`themis.litcache.oa`) → convert (`themis.litcache.convert`) →
  write the paper directory and commit the manifest (`themis.litcache.writer`).

Both halves talk only to a `google.cloud.storage.Bucket` and a Postgres connection,
so they run the same under a unit test, a DirectRunner, or Dataflow.

Bibliographic metadata is resolved in bulk *outside* this core (`resolve.resolve_batch`
in the batched stage), not per paper — one efetch/idconv call per 200 ids rather than
one per paper, collapsing the NCBI rate domain. `ingest_paper` receives the
`ResolvedPaper` and never calls the resolver ladder itself. The resolved cross-ids
also carry the ids the OA fetch keys on — the `pmcid` (efetch-harvested or
idconv-mapped) and, for a Bookshelf chapter, the `bookid` — so the OA fetch needs no
live resolver.

This branches on OA (literature-cache.md §Conversion): it attempts the litfetch ladder
for full-text XML, and on a hit renders it with litdown (`xml-faithful`), retaining the
seed pdf as a source alongside the fetched xml and taking licence / access from the
fetched artifact. Otherwise it renders the seed Docling json (`pdf-derived`) and probes
the pdf for char-addressability — the pdf is the source of truth there, so quote→bbox
recovery hinges on its character layer; on the OA branch the probe is skipped (the XML
is the source of truth).

A seed may be a pdf alone, with no Docling json. Its key is then all that names the paper,
so before anything is claimed or written the pdf is checked against the resolved record
(`SeedMismatchError`): a pdf that is another paper, a stub, or an issue's compilation fails
there. On the non-OA branch such a paper is committed with its pdf and no rendering; the
worker's transcription step makes its main text.

The manifest's `ExternalIds` are the ids that were minted: identity's, claimed before the
manifest-exists skip, plus one harvested id — a book record's Bookshelf accession — claimed
on the write path only (`_claim_accession`), so a committed paper never gains a crosswalk
row its manifest does not record. The resolver's other cross-ids are not minted: minting
them is what would make them crosswalk-consistent, and that minting can trip the
(deferred) equivalence path — so cross-id enrichment waits for that work.

A genuine cross-paper link (`mint` returning two or more incumbents — the paper's ids
bridge previously-separate works) writes the equivalence edge into every involved
manifest (`_link_equivalence`); the join lives in the manifests, never DB-only, and
the paper adopts the canonical (lowest) `doc_id` rather than minting.

The one caller input that remains (`LicenceFacts`) is the non-OA branch's `licence` /
`licence_basis` / `access`: the OA branch reads these from the fetched bytes, but a
non-OA paper's licence comes from litfetch's access authorities, and an unknown licence
has no honest `licence_basis` (the enum is `{artifact, asserted}`), so the pipeline must
not invent one.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence

import litfetch
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs

from themis.common import storage
from themis.litcache import (
    convert,
    crosswalk,
    identity,
    ingest,
    oa,
    pdf,
    records,
    resolve,
    sourced_metadata,
    writer,
)
from themis.litcache.models import litcache_pb2

# Source-lineage handles: the manifest's `Source.handle` and the on-disk
# `sources/{handle}/…` stem. The pdf lineage is always retained; the xml lineage
# is added only on the OA branch.
_PDF_HANDLE = 'pdf'
_XML_HANDLE = 'xml'

# A pdf-only seed is taken for its paper when this share of the title's words is on its first pages, in order and
# within this many times the title's length of each other.
_TITLE_PAGES = 2
_MIN_TITLE_COVERAGE = 0.8
_TITLE_SPREAD = 2
# Shorter words ("of", "in", "a", HGVS fragments) carry no signal and are often split or dropped by a text layer.
_MIN_TITLE_WORD_LEN = 3
# A hyphen ending a line: a word broken across lines, or a compound ("whole-genome") that happens to end one. Pdfium
# writes one it recognises as `\x02`, with no line break after it.
_LINE_END_HYPHEN = re.compile(r'\x02|[-\u00ad]\r?\n')
# A line break after a DOI's `.`, `/` or `-`, where a narrow column wraps it.
_WRAPPED_DOI = re.compile(r'(?<=[./-])\r?\n')


class SeedMismatchError(ValueError):
    """A pdf-only seed does not read as the paper its key names, so nothing of it is written."""


@dataclasses.dataclass(frozen=True)
class SeedObject:
    """One paper's bytes as they sit in the `ingest/` seed prefix.

    Attributes:
        bucket_key: The `ingest/` object name (URL-encoded identifier), the
            primary identity signal.
        docling_json: The seed `DoclingDocument` json — the identity origin and
            the markdown source on the non-OA branch — or None for a pdf-only seed.
        pdf: The seed pdf, retained verbatim as the revision's source bytes.
    """

    bucket_key: str
    docling_json: bytes | None
    pdf: bytes


@dataclasses.dataclass(frozen=True)
class LicenceFacts:
    """The non-OA branch's licence facts the pipeline receives rather than resolves.

    The OA branch reads licence/access from the fetched artifact; this is the
    fallback for the non-OA branch, where the licence comes from litfetch's access
    authorities. The caller cannot know in advance which branch a paper takes, so it
    always supplies these; on the OA branch they are unused. There are no defaults: a
    missing fact is a caller error, never a placeholder.

    Attributes:
        licence: The raw licence string (not an SPDX id); non-OA branch only.
        licence_basis: `LICENCE_BASIS_ARTIFACT` or `LICENCE_BASIS_ASSERTED`.
        access: The `Access` oneof; non-OA branch only.
    """

    licence: str
    licence_basis: litcache_pb2.LicenceBasis
    access: litcache_pb2.Access


@dataclasses.dataclass(frozen=True)
class IngestResult:
    """The outcome of ingesting one seed object.

    Attributes:
        doc_id: The claimed `doc_id` (minted fresh or adopted).
        minted: True if a fresh uuid4 was minted; False if an incumbent `doc_id`
            was adopted.
        written: True if this call wrote the paper; False if it skipped an
            already-committed paper.
        manifest: The committed manifest — assembled when newly written, loaded
            from storage when the paper was already cached.
        generation: The manifest's generation once this call returned, which this
            writer's request for the paper carries: the one its write left, or, for a
            paper already committed that this call skipped, the one it found. A skipped
            paper is still asked for, since a retried bundle skips the papers its failed
            attempt committed and may not have asked for; the task name dedupes the
            request when the paper was asked for at that generation.
    """

    doc_id: str
    minted: bool
    written: bool
    manifest: litcache_pb2.Manifest
    generation: int


def extract_identity(seed: SeedObject) -> identity.Identity:
    """Classify a seed object's identity — the local, network-free first half.

    Identity comes from the explicit bucket key and docling origin. The pdf's embedded
    DOI is only a *fallback*: consulted when the key and origin name no external id (an
    otherwise content-addressed deposit), never overriding or competing with an
    explicit id — publisher-populated pdf metadata is not reliable enough to override
    what a deposit explicitly declares. A pdf-only seed has no origin, so its key alone
    names it. No network or database: identity is a pure function of the seed.

    Args:
        seed: The paper's seed bytes (`bucket_key`, docling json, pdf).

    Returns:
        The classified `Identity` (mint keys, external ids, claim key).

    Raises:
        ValueError: If identity cannot be determined (no external id and no origin
            binary hash to content-address against — always the case for a pdf-only
            seed whose key names no external id).
    """
    if seed.docling_json is None:
        return identity.determine_identity(seed.bucket_key, identity.DoclingOrigin(filename=None, binary_hash=None))
    origin = identity.read_docling_origin(seed.docling_json)
    ident = identity.determine_identity(seed.bucket_key, origin)
    if not ident.content_addressed:
        return ident
    pdf_doi = pdf.doi_from_metadata(seed.pdf).result()
    if pdf_doi is None:
        return ident
    return identity.determine_identity(seed.bucket_key, origin, extra_candidates=[pdf_doi])


def ingest_paper(
    bucket: gcs.Bucket,
    mint: Callable[[Iterable[str]], crosswalk.MintResult],
    seed: SeedObject,
    ident: identity.Identity,
    resolved: resolve.ResolvedPaper,
    licence: LicenceFacts,
    *,
    now: datetime.datetime,
    fetchers: Sequence[litfetch.Fetcher] | None = None,
    file_sources: Sequence[litfetch.FileSource] | None = None,
) -> IngestResult:
    """Write half: claim a `doc_id`, skip or fetch-OA + convert + commit the paper.

    Idempotent and resume-safe: the claimed `doc_id` is reused across re-runs (the
    crosswalk row survives), and a paper whose manifest already exists is skipped
    before any fetch or conversion work. A crash before the manifest write leaves no
    manifest, so the next run re-completes the paper under the same `doc_id`; a crash
    between the commit and `metadata.pb` leaves the record to the skip, which writes it
    when this paper's ids are the manifest's (`writer.fill_absent_metadata`).

    Bibliographic metadata is resolved upstream (`resolve.resolve_batch`) and passed
    in; this half never calls the resolver ladder. The OA fetch reads the `pmcid` off
    `resolved.external_ids`, so it needs no live resolver either.

    Args:
        bucket: The cache bucket to write the paper directory into.
        mint: Claims the paper's external ids and returns the `MintResult` (the caller
            owns the connection + any concurrency control around it).
        seed: The paper's seed bytes (`bucket_key`, docling json, pdf).
        ident: The paper's identity (from `extract_identity`).
        resolved: The paper's pre-resolved bibliographic metadata + cross-ids.
        licence: The non-OA-branch licence fallback (the OA branch reads its own
            from the fetched bytes).
        now: Timezone-aware timestamp for the capture and rendering records.
        fetchers: The litfetch ladder for the OA-XML attempt; defaults to litfetch's
            own. Tests inject doubles (or `[]`) to stay offline.
        file_sources: litfetch file sources for the supplementary-file fetch;
            defaults to litfetch's own (the PMC OA source). Consulted only on the OA
            branch; tests inject `[]` to stay offline.

    Returns:
        The `IngestResult` for this object.

    Raises:
        SeedMismatchError: If `seed` is a pdf alone and does not read as the paper
            `resolved` describes (`_check_pdf_only_seed`); nothing is claimed or written.
        ValueError: If an OA body's source is not a known `SourceKind`, a cross-paper
            link reaches an incumbent with no manifest (an orphan, which cannot carry
            an equivalence edge), a book record's accession is already claimed by
            another paper (`_claim_accession`), or an input the writer rejects.
        pypdfium2.PdfiumError: If the seed pdf is a pdf alone, or is on the non-OA
            branch, and is not a loadable pdf (the char-addressability probe fails loud
            rather than report a degraded paper as image-only).
    """
    if seed.docling_json is None:
        _check_pdf_only_seed(seed, ident, resolved)
    mint_result = mint(ident.mint_keys)
    doc_id = mint_result.doc_id
    if mint_result.linked_doc_ids:
        # The ids bridge previously-separate works: record the equivalence edge
        # into every involved manifest. The paper adopts the canonical doc_id,
        # whose manifest already exists, so the skip below returns it.
        _link_equivalence(bucket, mint_result.linked_doc_ids)

    committed = _load_committed(bucket, doc_id)
    if committed is not None:
        existing, generation = committed
        writer.fill_absent_metadata(
            bucket, doc_id, existing, _manifest_external_ids(ident, resolved), resolved.metadata
        )
        return IngestResult(
            doc_id=doc_id, minted=mint_result.minted, written=False, manifest=existing, generation=generation
        )

    _claim_accession(mint, ident, resolved)
    oa_source, supplementary = asyncio.run(_fetch_oa(ident, resolved, fetchers=fetchers, file_sources=file_sources))
    branch = _convert_branch(seed, licence, oa_source, now=now)
    files = [
        writer.FileInput(
            role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY,
            name=s.filename,
            source_url=s.origin_url,
            data=s.content,
            captured_at=now,
        )
        for s in supplementary
    ]

    paper = writer.PaperInput(
        doc_id=doc_id,
        external_ids=_manifest_external_ids(ident, resolved),
        claim_key=ident.claim_key,
        equivalence=litcache_pb2.Equivalence(edges=[], canonical_doc_id=doc_id),
        retraction=litcache_pb2.Retraction(),
        sources=branch.sources,
        renderings=branch.renderings,
        metadata=resolved.metadata,
        files=files,
    )
    result = writer.write_paper(bucket, paper)
    return IngestResult(
        doc_id=doc_id,
        minted=mint_result.minted,
        written=result.written,
        manifest=result.manifest,
        generation=result.generation,
    )


def why_seed_not_held(bucket: gcs.Bucket, manifest: litcache_pb2.Manifest, seed: SeedObject) -> str | None:
    """Why the paper `manifest` commits does not hold its seed, or None when it does.

    A paper holds its seed when it stores the seed pdf's bytes as a source revision and, if the seed has a Docling
    json, has main text: the json only ever bootstraps markdown, so once the paper has text the pdf is all the seed
    adds. A pdf-only seed adds nothing beyond its pdf, so the revision alone holds it, and the worker's transcription
    makes the text. A paper ingested before, from another seed or another route, can hold neither.

    Args:
        bucket: The cache bucket.
        manifest: The paper's committed manifest, as `ingest_paper` returned it.
        seed: The seed.

    Returns:
        A reason for a person reading the run's dead letters, or None when the seed may be deleted.
    """
    if seed.docling_json is not None and records.markdown(manifest) is None:
        return 'the paper has no main text'
    pdf_hash = hashlib.sha256(seed.pdf).hexdigest()
    missing = []
    for source in manifest.sources:
        if any(revision.hash == pdf_hash for revision in source.revisions):
            name = writer.source_revision_path(manifest.doc_id, source.handle, pdf_hash, source.media_type)
            if bucket.blob(name).exists():
                return None
            missing.append(name)
    if missing:
        return f'its manifest records the seed pdf as {", ".join(missing)}, which does not exist'
    return f'the paper holds no source revision {pdf_hash}, so its seed pdf is another copy'


async def _fetch_oa(
    ident: identity.Identity,
    resolved: resolve.ResolvedPaper,
    *,
    fetchers: Sequence[litfetch.Fetcher] | None,
    file_sources: Sequence[litfetch.FileSource] | None,
) -> tuple[oa.OaSource | None, list[oa.SupplementaryFile]]:
    """Fetch the OA-XML body and its supplementary files (no metadata resolution).

    The id bundle is identity's fetchable ids plus the batch-resolved ids the fetchers key
    on — the `pmcid` for the PMC rungs, the `bookid` for the Bookshelf rung — so no live
    resolver runs (`resolver=None`); litfetch owns its own HTTP client and pacing through
    the `Session`. The OA work is skipped when the paper carries no fetchable id;
    supplementary files are fetched only when the OA body was served (the paper is in PMC
    OA), so a non-OA paper pays no extra listing call.
    """
    article_ids = oa.article_ids_for_fetch(ident.external_ids, resolved.external_ids)
    if article_ids is None:
        return None, []
    async with litfetch.Session() as session:
        oa_source = await oa.fetch_oa_source(article_ids, resolver=None, fetchers=fetchers, session=session)
        supplementary: list[oa.SupplementaryFile] = []
        if oa_source is not None:
            supplementary = await oa.fetch_supplementary(
                article_ids, sources=file_sources, session=session, skip_unfetchable=True
            )
    return oa_source, supplementary


def _claim_accession(
    mint: Callable[[Iterable[str]], crosswalk.MintResult], ident: identity.Identity, resolved: resolve.ResolvedPaper
) -> None:
    """Claim a book record's Bookshelf accession for the paper — on the write path only.

    The accession is the one harvested id that is minted, and it is claimed after the
    manifest-exists skip so a committed paper never gains a crosswalk row its manifest does
    not record (the manifests are what `rebuild` inverts). Identity's keys are presented with
    it so the claim attaches to the paper's own `doc_id`. The resolver's other cross-ids stay
    unminted: a DOI or PMCID harvested for one seed may already be claimed by another deposit
    of the same work, and claiming it would bridge the two — the equivalence path that is
    deferred. An accession names exactly the record it was read from, so the only other
    deposit that can carry it is a second copy of the chapter under a different identity —
    the same deferred path, refused here rather than committed as a manifest and a table
    that disagree.

    Raises:
        ValueError: If the accession is already claimed by another paper.
    """
    if not resolved.external_ids.HasField('bookid'):
        return
    key = identity.ExternalId(scheme='bookid', value=resolved.external_ids.bookid).key
    claim = mint((*ident.mint_keys, key))
    if claim.linked_doc_ids:
        raise ValueError(
            f'{key} is already claimed by another paper ({", ".join(claim.linked_doc_ids)}): a second deposit of '
            'one chapter under a different identity is the deferred equivalence path'
        )


def _manifest_external_ids(ident: identity.Identity, resolved: resolve.ResolvedPaper) -> litcache_pb2.ExternalIds:
    """Map the minted ids to the manifest `ExternalIds` (doi/pmid/pmcid, and a book record's bookid).

    Only the schemes the manifest models; `pii`/`binhash` have no field. The manifest carries
    exactly the ids the paper claimed — identity's and, on the write path, the accession
    `_claim_accession` minted — which is what keeps the crosswalk rebuildable from the
    manifests alone.
    """
    external_ids = ingest.manifest_external_ids(ident.external_ids)
    if resolved.external_ids.HasField('bookid'):
        external_ids.bookid = resolved.external_ids.bookid
    return external_ids


def _load_committed(bucket: gcs.Bucket, doc_id: str) -> tuple[litcache_pb2.Manifest, int] | None:
    """The committed manifest for `doc_id` and the generation it was read at, or None when the paper is uncached."""
    blob = bucket.blob(writer.manifest_path(doc_id))
    try:
        data = blob.download_as_bytes()
    except api_exceptions.NotFound:
        return None
    if blob.generation is None:
        raise RuntimeError(f'{blob.name} was read with no generation')
    return litcache_pb2.Manifest.FromString(data), blob.generation


def _load_manifest(bucket: gcs.Bucket, doc_id: str) -> litcache_pb2.Manifest | None:
    """Load the committed manifest for `doc_id`, or None when the paper is uncached."""
    blob = bucket.blob(writer.manifest_path(doc_id))
    if not blob.exists():
        return None
    return litcache_pb2.Manifest.FromString(blob.download_as_bytes())


def _link_equivalence(bucket: gcs.Bucket, linked_doc_ids: Sequence[str]) -> None:
    """Write the equivalence edge across an entire cross-paper-link class.

    `mint` reports the incumbents the paper's ids bridge directly; the true class
    may be larger if an incumbent already links to others, so the class is the
    transitive closure over existing manifest edges. Every member's manifest is
    rewritten with `edges` = the rest of the class and `canonical_doc_id` = the
    lowest member — the shape `rebuild` reconstructs by union-find (so the table
    stays rebuildable). The rewrite is a generation-preconditioned read-modify-write
    that parses the current manifest and sets only the `equivalence` field, so a
    newer writer's other fields (and any unknown fields) round-trip untouched.

    The bulk ingester's, not the on-demand path's: the request path only ever creates a
    paper and never rewrites a committed one, so a link it discovers is reported for a
    later sweep rather than repaired inline (`themis.litcache.ingest`).

    Args:
        bucket: The cache bucket.
        linked_doc_ids: The incumbent `doc_id`s `mint` reported (the link seeds).

    Raises:
        ValueError: If a member of the class has no manifest (an orphan incumbent:
            a claim row whose paper never committed) — an edge to a manifest-less
            `doc_id` is the inconsistency `rebuild` rejects, so fail loud and let
            the orphan's own re-ingestion heal it before the link is retried.
        themis.common.storage.StaleGenerationError: If a member's rewrite lost its
            generation race the whole attempt budget (contention — retryable).
    """
    component = _equivalence_closure(bucket, linked_doc_ids)
    canonical = min(component)
    members = set(component)
    for doc_id in component:
        edges = sorted(members - {doc_id})

        def mutate(data: bytes, edges: list[str] = edges) -> bytes:
            manifest = litcache_pb2.Manifest.FromString(data)
            del manifest.equivalence.edges[:]
            manifest.equivalence.edges.extend(edges)
            manifest.equivalence.canonical_doc_id = canonical
            return manifest.SerializeToString()

        storage.read_modify_write(bucket, writer.manifest_path(doc_id), mutate)


def _equivalence_closure(bucket: gcs.Bucket, seeds: Sequence[str]) -> dict[str, litcache_pb2.Manifest]:
    """Load the transitive equivalence class reachable from `seeds` via manifest edges."""
    found: dict[str, litcache_pb2.Manifest] = {}
    frontier = list(seeds)
    while frontier:
        doc_id = frontier.pop()
        if doc_id in found:
            continue
        manifest = _load_manifest(bucket, doc_id)
        if manifest is None:
            raise ValueError(f'cross-paper link reaches {doc_id} with no manifest (orphan incumbent)')
        found[doc_id] = manifest
        frontier.extend(edge for edge in manifest.equivalence.edges if edge not in found)
    return found


def _words(text: str) -> list[str]:
    """`text` as lower-case ascii words: punctuation and hyphens split words, accents are dropped."""
    letters = []
    for c in unicodedata.normalize('NFKD', text):
        category = unicodedata.category(c)[0]
        if category in 'LN':
            letters.append(c)
        elif category != 'M':  # a combining accent belongs to the letter before it
            letters.append(' ')
    return ''.join(letters).encode('ascii', 'ignore').decode().lower().split()


def _significant_words(text: str) -> list[str]:
    """`_words` of `text` long enough to tell one title from another."""
    return [w for w in _words(text) if len(w) >= _MIN_TITLE_WORD_LEN]


def _in_order(title: Sequence[str], window: Sequence[str]) -> int:
    """How many of `title`'s words `window` holds in the same order: their longest common subsequence."""
    previous = [0] * (len(window) + 1)
    for w in title:
        current = [0]
        for j, v in enumerate(window):
            current.append(previous[j] + 1 if w == v else max(previous[j + 1], current[j]))
        previous = current
    return previous[-1]


def _title_coverage(title: Sequence[str], text: Sequence[str]) -> float:
    """The largest share of `title`'s words in `text` in order, within `_TITLE_SPREAD` times the title's length."""
    wanted = set(title)
    span = _TITLE_SPREAD * len(title)
    best = 0
    for start, word in enumerate(text):
        if word in wanted:
            best = max(best, _in_order(title, text[start : start + span]))
            if best == len(title):
                break
    return best / len(title)


def _prints_doi(text: str, doi: str) -> bool:
    """Whether `text` prints `doi` whole (bare, in a URL, or after a `DOI` label), not as part of a longer DOI.

    A DOI wrapped across lines, at a `.`, `/` or hyphen, is read rejoined.
    """
    whole = re.compile(rf'(?:(?<![\w.])|(?<=doi)){re.escape(doi)}(?![\w/-]|\.\w)', re.IGNORECASE)
    rejoined = _WRAPPED_DOI.sub('', text).replace('\x02', '-')
    return any(whole.search(t) is not None for t in (text, rejoined))


def _check_pdf_only_seed(seed: SeedObject, ident: identity.Identity, resolved: resolve.ResolvedPaper) -> None:
    """Check a pdf-only seed reads as the paper its key names, the only thing that names it.

    Most of the resolved title's words must be on the pdf's first pages, in order and close together: that rules out
    another paper, a one-page stub, and an issue's compilation whose paper starts further in. It does not rule out a
    pdf whose first pages quote the title in order — a reply to the paper, or a contents page. The pdf's embedded
    DOI, where it has one, must be the paper's unless those pages print the paper's DOI and not the embedded one:
    publishers' metadata names another work's DOI often enough that the printed one is the better witness, but a pdf
    that prints the other DOI is that work, citing this one. A pdf with no text layer there is taken on an embedded
    DOI that is the paper's, and otherwise cannot be checked.

    Args:
        seed: The seed, a pdf alone.
        ident: The identity its key gives it.
        resolved: The record resolved for that identity.

    Raises:
        SeedMismatchError: If the pdf fails a check, or there is nothing to check it by: no text on its first pages
            and no embedded DOI that is the paper's, or no title word of three or more Latin letters or digits in the
            resolved record (a title in another script, say). A title of one or two such words is checked by them
            alone, so is weaker evidence than a long one.
        pypdfium2.PdfiumError: If the pdf is not loadable.
        ValueError: If `resolved.metadata` is not a valid `SourcedMetadata`.
    """
    stated = next((eid.value for eid in ident.external_ids if eid.scheme == 'doi'), None) or (
        resolved.external_ids.doi or None
    )
    embedded = pdf.doi_from_metadata(seed.pdf).result()
    doi_agrees = stated is not None and embedded is not None and embedded.lower() == stated.lower()
    raw = pdf.leading_text(seed.pdf, pages=_TITLE_PAGES).result()
    if not raw.strip():
        if doi_agrees:
            return
        no_text = f'the pdf has no text on its first {_TITLE_PAGES} pages'
        if stated is None:
            raise SeedMismatchError(f'{no_text}, and the paper has no DOI to check its metadata by')
        raise SeedMismatchError(f'{no_text}, and its metadata does not name the DOI {stated}')
    if embedded is not None and stated is not None and not doi_agrees:
        if not _prints_doi(raw, stated):
            raise SeedMismatchError(f'the pdf metadata names DOI {embedded}, and its first pages do not print {stated}')
        if _prints_doi(raw, embedded):
            raise SeedMismatchError(f'the pdf metadata names DOI {embedded}, and its first pages print it')
    title = sourced_metadata.parse(resolved.metadata).metadata.title
    words = _significant_words(title)
    if not words:
        raise SeedMismatchError(f'the resolved title {title!r} has no word to find in the pdf')
    coverage = max(_title_coverage(words, _significant_words(text)) for text in (raw, _LINE_END_HYPHEN.sub('', raw)))
    if coverage < _MIN_TITLE_COVERAGE:
        raise SeedMismatchError(
            f"{coverage:.0%} of the title's words are in order on the pdf's first {_TITLE_PAGES} pages: {title!r}"
        )


@dataclasses.dataclass(frozen=True)
class _Branch:
    """The conversion-branch outcome: the source lineages and the rendering, if the branch makes one.

    Licence/access live on each `SourceInput`; there is no per-revision term.
    """

    sources: list[writer.SourceInput]
    renderings: list[writer.RenderingInput]


def _convert_branch(
    seed: SeedObject,
    licence: LicenceFacts,
    oa_source: oa.OaSource | None,
    *,
    now: datetime.datetime,
) -> _Branch:
    """Render the paper on the OA or non-OA branch and assemble the source lineages.

    OA (an XML body was fetched): convert the XML (`xml-faithful`), retain the seed
    pdf as a lineage alongside the fetched xml, and take licence/access from the
    fetched artifact. The retained seed pdf carries no machine-readable licence, so
    it is asserted to share the work's terms (`licence_basis=asserted`). Non-OA:
    render the seed Docling json (`pdf-derived`) — or, for a pdf-only seed, render
    nothing — take licence/access from the `LicenceFacts` fallback, and probe the pdf
    for char-addressability (skipped on the OA branch — the XML is the source of
    truth a quote maps back to).
    """
    if oa_source is not None:
        xml_hash = hashlib.sha256(oa_source.content).hexdigest()
        conversion = convert.convert_jats(oa_source.content)
        xml_source = writer.SourceInput(
            handle=_XML_HANDLE,
            media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML,
            kind=oa_source.kind,
            data=oa_source.content,
            licence=oa_source.access.licence,
            licence_basis=oa_source.access.licence_basis,
            access=oa_source.access.access,
            captured_at=now,
            origin_url=oa_source.origin_url,
        )
        pdf_source = writer.SourceInput(
            handle=_PDF_HANDLE,
            media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
            kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
            data=seed.pdf,
            licence=oa_source.access.licence,
            licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
            access=oa_source.access.access,
            captured_at=now,
        )
        return _Branch(
            sources=[pdf_source, xml_source],
            renderings=[
                writer.RenderingInput(
                    construction=conversion.construction,
                    from_revision=xml_hash,
                    created_at=now,
                    markdown=conversion.markdown,
                )
            ],
        )

    pdf_source = writer.SourceInput(
        handle=_PDF_HANDLE,
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_SEED,
        data=seed.pdf,
        licence=licence.licence,
        licence_basis=licence.licence_basis,
        access=licence.access,
        captured_at=now,
        has_text_layer=pdf.probe_has_text_layer(seed.pdf).result(),
    )
    if seed.docling_json is None:
        return _Branch(sources=[pdf_source], renderings=[])
    conversion = convert.convert_docling(seed.docling_json)
    return _Branch(
        sources=[pdf_source],
        renderings=[
            writer.RenderingInput(
                construction=conversion.construction,
                from_revision=hashlib.sha256(seed.pdf).hexdigest(),
                created_at=now,
                markdown=conversion.markdown,
            )
        ],
    )
