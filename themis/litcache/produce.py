"""Bring one paper up to date, step by step: the full-text worker's loop.

A paper's work is a set of steps (`docs/design/evidence-fulltext.md` § A paper's work is a set of steps). Each step
declares the inputs it reads off the manifest, whether its output is already there, which steps' output it reads, and
whether it spends. Three produce the main text:

- **xml**, free: fetch open-access XML off the litfetch ladder and render it with litdown, committing the XML and its
  rendering together (`writer.add_source_and_rendering`).
- **locate**, free: find an open-access PDF via Unpaywall and commit it as a source (`writer.add_source`).
- **transcription**, spends: transcribe the paper's PDF with the injected converter and commit the rendering
  (`writer.add_rendering`). It reads the xml and locate steps' output, so it runs after them.

and one fetches the paper's other files:

- **supplements**, free: fetch the supplementary files listed for the paper and commit them (`writer.add_files`).
  Today the only list is the PubMed Central open-access package, which exists for a paper the XML step served, so
  the step has an input only once the paper holds open-access XML, and runs after the XML step. litfetch reports a
  listing it could not make as an empty one, so a transient fault there settles the step as having nothing to
  fetch on that XML, as an empty answer from the ladder settles the XML step. A listed file that cannot be
  downloaded leaves the step undone for a later request, the sweep's included, and after
  `SUPPLEMENT_ATTEMPTS_LIMIT` such attempts on the same XML the step settles as failed: litfetch reports a dead link
  and a passing fault alike.

`run` works out which steps a request reaches from its targets, then runs, in declared order, every reached step with
work and then every free one, rereading the manifest after each, until none has work. A step with work is one whose
output is missing and whose job log (`joblog`) shows it neither settled on its current inputs nor running. A step that
ends with no record settles in the log; a spending step begins in the log first, so two requests cannot both pay for
it. Once every route to the main text has settled with no text, the worker writes the terminal marker readers see
(`outcome`).

The fetchers and the PDF converter are injected so the loop runs offline in tests.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import importlib.metadata
import logging
from collections.abc import Awaitable, Callable, Collection, Iterable

import litdown
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from litfetch import resolvers

from themis.litcache import identity, joblog, oa, ocr, outcome, pdf, records, work, writer
from themis.litcache.models import litcache_pb2, work_pb2

# The fetched XML lands as its own lineage; the litdown rendering is produced from it.
_XML_HANDLE = 'xml'
# A located PDF lands in the lineage a seed PDF would.
_PDF_HANDLE = 'pdf'
_LITDOWN_VERSION = importlib.metadata.version('litdown')

# ExternalIds' fields, each an id whose name is its `identity.ExternalId` scheme; read off the
# descriptor so an id field added to the proto reaches the fetch bundle without a change here.
# `oa.article_ids` keeps only the fetchable subset.
_ID_FIELDS = tuple(field.name for field in litcache_pb2.ExternalIds.DESCRIPTOR.fields)

_LOG = logging.getLogger(__name__)

XML_STEP = 'xml'
LOCATE_STEP = 'locate'
TRANSCRIPTION_STEP = 'transcription'
SUPPLEMENTS_STEP = 'supplements'
# Attempts on one XML revision that may fail to download a listed supplementary file before the step settles.
SUPPLEMENT_ATTEMPTS_LIMIT = 3

Fetcher = Callable[..., Awaitable[oa.OaSource | None]]
PdfFetcher = Callable[..., Awaitable[oa.OaPdf | None]]
SupplementFetcher = Callable[..., Awaitable[list[oa.SupplementaryFile]]]


class PaperNotInCorpusError(Exception):
    """The corpus holds no manifest for the doc_id.

    Distinguishes the one `NotFound` a retry cannot clear from every other missing object — a seed
    revision blob, a manifest that vanished mid-write — which are operational faults rather than a
    paper the corpus does not hold, and must stay retryable.
    """


class UnknownTargetError(ValueError):
    """A request names a target that is neither a step nor the main text."""


class _SettledError(Exception):
    """A step ended without a record, and would end the same way on every attempt on these inputs."""

    def __init__(self, settled_outcome: work_pb2.SettledOutcome, detail: str) -> None:
        super().__init__(detail)
        self.outcome = settled_outcome
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class _Run:
    """One request's run of the loop: the paper, who is asking, and what the steps call out to."""

    bucket: gcs.Bucket
    doc_id: str
    attempt: joblog.Attempt
    resolver: resolvers.Resolver | None
    fetch: Fetcher
    fetch_pdf: PdfFetcher
    fetch_supplementary: SupplementFetcher
    convert_pdf: ocr.PdfConverter
    now: Callable[[], datetime.datetime]
    deadline: datetime.timedelta

    def settle(self, step: str, inputs: Iterable[str], settled: _SettledError) -> None:
        joblog.append(
            self.bucket,
            self.doc_id,
            [
                self.attempt.entry(
                    at=self.now(),
                    step=step,
                    inputs=inputs,
                    event=work_pb2.JOB_EVENT_SETTLED,
                    outcome=settled.outcome,
                    detail=settled.detail,
                )
            ],
        )

    def refuse(self, step: str, inputs: Iterable[str], error: ocr.SpendRefusedError) -> None:
        """Log this attempt's `step` as refused at the spend limit: closed, unspent, and not settled."""
        entry = self.attempt.entry(
            at=self.now(), step=step, inputs=inputs, event=work_pb2.JOB_EVENT_REFUSED, detail=str(error)
        )
        joblog.append(self.bucket, self.doc_id, [entry])

    def fail(self, step: str, inputs: Iterable[str], error: BaseException, *, model_answered: bool) -> None:
        """Log `error` as closing this attempt's `step`, which the caller is about to re-raise.

        A log write that fails is logged, not raised, so the error the caller re-raises stays the one that
        stopped the step. The begun entry then stays open, and counts as spent once it is dead.
        """
        entry = self.attempt.entry(
            at=self.now(),
            step=step,
            inputs=inputs,
            event=work_pb2.JOB_EVENT_FAILED,
            model_answered=model_answered,
            detail=f'{type(error).__name__}: {error}',
        )
        try:
            joblog.append(self.bucket, self.doc_id, [entry])
        except (joblog.LogContentionError, api_exceptions.GoogleAPIError):
            _LOG.exception('could not log the failure of %s on %s', step, self.doc_id)


def _nothing(detail: str) -> _SettledError:
    return _SettledError(work_pb2.SETTLED_OUTCOME_NOTHING, detail)


def _permanent(detail: str) -> _SettledError:
    return _SettledError(work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE, detail)


@dataclasses.dataclass(frozen=True)
class _Step:
    """A kind of work on a paper.

    Attributes:
        name: The step's name, in the job log and as a request's target.
        spends: Whether running it costs model spend; only a request reaching it may run it.
        needs: The steps whose output it reads; a request reaching it reaches them.
        inputs: The hashes of what it would read now, off the manifest; None when it has nothing to work on.
        produced: Whether its output is present on its current inputs.
        run: Runs it on those inputs, committing its record or raising `_SettledError`.
    """

    name: str
    spends: bool
    needs: tuple[str, ...]
    inputs: Callable[[litcache_pb2.Manifest], tuple[str, ...] | None]
    produced: Callable[[litcache_pb2.Manifest], bool]
    run: Callable[[_Run, litcache_pb2.Manifest, tuple[str, ...]], Awaitable[None]]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _external_ids(external_ids: litcache_pb2.ExternalIds) -> list[identity.ExternalId]:
    return [
        identity.ExternalId(scheme=field, value=getattr(external_ids, field))
        for field in _ID_FIELDS
        if external_ids.HasField(field)
    ]


def _renderings_by(manifest: litcache_pb2.Manifest, converter: litcache_pb2.Converter) -> list[litcache_pb2.Record]:
    return [
        record
        for record in manifest.records.values()
        if record.WhichOneof('construction') == 'rendering' and record.rendering.converter == converter
    ]


def _commit(write: Callable[[], object]) -> None:
    """Run a manifest write, turning the writer's refusal into the settled failure it is.

    The writer raises `ManifestRefusedError` only for a write it will refuse on every attempt. A storage fault is
    not caught here, and stays retryable.
    """
    try:
        write()
    except writer.ManifestRefusedError as e:
        raise _permanent(f'the manifest refused the write: {e}') from e


def _current_revision(source: litcache_pb2.Source) -> litcache_pb2.Revision:
    """The latest revision of a lineage — recency is `captured_at`, never array order."""
    if not source.revisions:
        raise ValueError(f'source lineage {source.handle!r} has no revisions')
    return max(source.revisions, key=lambda r: r.captured_at.ToDatetime())


def _pdf_source(manifest: litcache_pb2.Manifest) -> litcache_pb2.Source | None:
    """The PDF lineage to transcribe when several exist (e.g. a seed PDF and a later publisher PDF).

    Picks the one whose newest revision is most recent, by `captured_at` — the same recency rule as
    `_current_revision`, never `manifest.sources` array order (which is ingestion order). Revision-less
    PDF lineages are ignored: there is nothing to download for them.
    """
    candidates = [
        s for s in manifest.sources if s.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF and s.revisions
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: max(r.captured_at.ToDatetime() for r in s.revisions))


# --- xml


def _xml_produced(manifest: litcache_pb2.Manifest) -> bool:
    return bool(_renderings_by(manifest, litcache_pb2.Converter.CONVERTER_LITDOWN))


async def _run_xml(run: _Run, manifest: litcache_pb2.Manifest, _inputs: tuple[str, ...]) -> None:
    article_ids = oa.article_ids(_external_ids(manifest.external_ids))
    if article_ids is None:
        raise _nothing('no id the ladder can fetch by')
    try:
        oa_source = await run.fetch(
            article_ids, resolver=run.resolver if run.resolver is not None else oa.default_resolver()
        )
    except oa.OaSourceError as e:
        # A real anomaly in the served body (unknown source kind / no access terms). Narrow to this
        # typed error, not a bare ValueError: litfetch's own dependencies raise ValueError (e.g. a
        # JSONDecodeError) on a transient upstream blip, which must propagate and retry.
        raise _permanent(str(e)) from e
    if oa_source is None:
        raise _nothing('the open-access ladder served no XML')
    try:
        markdown = litdown.convert(oa_source.content)
    except ValueError as e:
        raise _permanent(f'litdown cannot parse the served XML: {e}') from e  # permanent for this body
    if not markdown.strip():
        raise _nothing('the served XML rendered no text')
    captured = run.now()
    source = writer.SourceInput(
        handle=_XML_HANDLE,
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_XML,
        kind=oa_source.kind,
        data=oa_source.content,
        licence=oa_source.access.licence,
        licence_basis=oa_source.access.licence_basis,
        access=oa_source.access.access,
        captured_at=captured,
        origin_url=oa_source.origin_url,
    )
    rendering = writer.RenderingInput(
        construction=litcache_pb2.RenderingConstruction(
            converter=litcache_pb2.Converter.CONVERTER_LITDOWN, converter_version=_LITDOWN_VERSION
        ),
        from_revision=hashlib.sha256(oa_source.content).hexdigest(),
        created_at=captured,
        markdown=markdown,
    )
    _commit(lambda: writer.add_source_and_rendering(run.bucket, run.doc_id, source, rendering))


# --- locate


async def _run_locate(run: _Run, manifest: litcache_pb2.Manifest, _inputs: tuple[str, ...]) -> None:
    article_ids = oa.article_ids(_external_ids(manifest.external_ids))
    if article_ids is None:
        raise _nothing('no id Unpaywall can look up')
    try:
        located = await run.fetch_pdf(article_ids)
    except oa.OaSourceError as e:
        raise _permanent(str(e)) from e
    if located is None:
        raise _nothing('Unpaywall lists no open copy')
    source = writer.SourceInput(
        handle=_PDF_HANDLE,
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_UNPAYWALL,
        data=located.content,
        licence=located.access.licence,
        licence_basis=located.access.licence_basis,
        access=located.access.access,
        captured_at=run.now(),
        origin_url=located.origin_url,
        has_text_layer=pdf.probe_has_text_layer(located.content),
    )
    _commit(lambda: writer.add_source(run.bucket, run.doc_id, source))


# --- transcription


def _transcription_inputs(manifest: litcache_pb2.Manifest) -> tuple[str, ...] | None:
    source = _pdf_source(manifest)
    return None if source is None else (_current_revision(source).hash,)


def _transcription_produced(manifest: litcache_pb2.Manifest) -> bool:
    """Whether the text was converted from XML, or the current PDF is already transcribed."""
    inputs = _transcription_inputs(manifest)
    transcribed = any(
        inputs is not None and inputs[0] in record.deps
        for record in _renderings_by(manifest, litcache_pb2.Converter.CONVERTER_LLM_OCR)
    )
    return _xml_produced(manifest) or transcribed


async def _run_transcription(run: _Run, manifest: litcache_pb2.Manifest, inputs: tuple[str, ...]) -> None:
    verdict = joblog.begin(
        run.bucket,
        run.doc_id,
        step=TRANSCRIPTION_STEP,
        inputs=inputs,
        attempt=run.attempt,
        now=run.now(),
        deadline=run.deadline,
    )
    if verdict is joblog.Verdict.SPENT:
        raise _permanent(f'{joblog.SPENT_ATTEMPTS_LIMIT} attempts were paid for and committed nothing')
    if verdict is not joblog.Verdict.BEGUN:
        return  # settled or running since this request read the log; either way it is not this request's to run
    source = _pdf_source(manifest)
    if source is None:
        raise ValueError(f'transcription of {run.doc_id} began with no PDF')  # its inputs said there was one
    revision = _current_revision(source)
    try:
        pdf_bytes = run.bucket.blob(
            writer.source_revision_path(run.doc_id, source.handle, revision.hash, source.media_type)
        ).download_as_bytes()
        result = await run.convert_pdf(pdf_bytes)
    except ocr.OcrError as e:
        # A refusal or truncation: the model answered, and would answer the same way again.
        raise _permanent(str(e)) from e
    except ocr.SpendRefusedError as e:
        # Undone, not failed: the same call succeeds once the limit leaves room, and the queue's retries run out long
        # before a monthly limit resets, so the request ends here and the sweep asks again.
        run.refuse(TRANSCRIPTION_STEP, inputs, e)
        return
    except ocr.SpentError as e:
        run.fail(TRANSCRIPTION_STEP, inputs, e, model_answered=True)
        raise
    except Exception as e:
        run.fail(TRANSCRIPTION_STEP, inputs, e, model_answered=False)
        raise
    if not result.markdown.strip():
        raise _nothing('the transcription is empty')
    rendering = writer.RenderingInput(
        construction=litcache_pb2.RenderingConstruction(
            converter=litcache_pb2.Converter.CONVERTER_LLM_OCR,
            converter_version=result.converter_version,
            model=result.model,
        ),
        from_revision=revision.hash,
        created_at=run.now(),
        markdown=result.markdown,
    )
    try:
        _commit(lambda: writer.add_rendering(run.bucket, run.doc_id, rendering))
    except _SettledError:
        raise
    except Exception as e:
        run.fail(TRANSCRIPTION_STEP, inputs, e, model_answered=True)  # paid for, and not committed
        raise


# --- supplements


def _xml_source(manifest: litcache_pb2.Manifest) -> litcache_pb2.Source | None:
    candidates = [
        s for s in manifest.sources if s.media_type == litcache_pb2.SourceFormat.SOURCE_FORMAT_XML and s.revisions
    ]
    return max(candidates, key=lambda s: _current_revision(s).captured_at.ToDatetime(), default=None)


def _supplements_inputs(manifest: litcache_pb2.Manifest) -> tuple[str, ...] | None:
    source = _xml_source(manifest)
    return None if source is None else (_current_revision(source).hash,)


def _supplements_produced(manifest: litcache_pb2.Manifest) -> bool:
    supplementary = litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY
    return any(f.role == supplementary and f.hash for f in manifest.files)


async def _run_supplements(run: _Run, manifest: litcache_pb2.Manifest, inputs: tuple[str, ...]) -> None:
    article_ids = oa.article_ids(_external_ids(manifest.external_ids))
    if article_ids is None:
        raise _nothing('no id a file list is kept under')
    try:
        fetched = await run.fetch_supplementary(
            article_ids, resolver=run.resolver if run.resolver is not None else oa.default_resolver()
        )
    except oa.OaSourceError as e:
        raise _permanent(str(e)) from e
    except oa.UpstreamUnavailableError as e:
        log = joblog.read(run.bucket, run.doc_id)
        attempts = joblog.failed_attempts(log, step=SUPPLEMENTS_STEP, inputs=inputs) + 1
        if attempts >= SUPPLEMENT_ATTEMPTS_LIMIT:
            raise _permanent(f'{attempts} attempts could not download every listed file; the last: {e}') from e
        run.fail(SUPPLEMENTS_STEP, inputs, e, model_answered=False)
        return  # undone: a later request tries again
    if not fetched:
        raise _nothing('no supplementary file is listed')
    captured = run.now()
    files = [
        writer.FileInput(
            role=litcache_pb2.AssociatedFileRole.ASSOCIATED_FILE_ROLE_SUPPLEMENTARY,
            name=f.filename,
            source_url=f.origin_url,
            data=f.content,
            captured_at=captured,
        )
        for f in fetched
    ]
    _commit(lambda: writer.add_files(run.bucket, run.doc_id, files))


# Declared so that every step comes after the steps whose output it reads.
STEPS: tuple[_Step, ...] = (
    _Step(XML_STEP, spends=False, needs=(), inputs=lambda _m: (), produced=_xml_produced, run=_run_xml),
    _Step(
        LOCATE_STEP,
        spends=False,
        needs=(),
        inputs=lambda _m: (),
        produced=lambda m: _pdf_source(m) is not None,
        run=_run_locate,
    ),
    _Step(
        TRANSCRIPTION_STEP,
        spends=True,
        needs=(XML_STEP, LOCATE_STEP),
        inputs=_transcription_inputs,
        produced=_transcription_produced,
        run=_run_transcription,
    ),
    _Step(
        SUPPLEMENTS_STEP,
        spends=False,
        needs=(XML_STEP,),
        inputs=_supplements_inputs,
        produced=_supplements_produced,
        run=_run_supplements,
    ),
)
_STEPS_BY_NAME = {step.name: step for step in STEPS}

# The main text's routes, in order of preference; it is met by any rendering, whichever made it.
_MAIN_TEXT_ROUTES = (XML_STEP, TRANSCRIPTION_STEP)

TARGETS = frozenset({work.MAIN_TEXT_TARGET, *_STEPS_BY_NAME})


def _main_text_met(manifest: litcache_pb2.Manifest) -> bool:
    return records.MARKDOWN_REF in manifest.refs


def reach(targets: Collection[str], manifest: litcache_pb2.Manifest) -> frozenset[str]:
    """The steps a request naming `targets` reaches on `manifest`: the targets and what they read, back to a met step.

    Raises:
        UnknownTargetError: If a target is neither a step nor the main text.
    """
    reached: set[str] = set()

    def visit(name: str) -> None:
        if name == work.MAIN_TEXT_TARGET:
            if not _main_text_met(manifest):
                for route in _MAIN_TEXT_ROUTES:
                    visit(route)
            return
        step = _STEPS_BY_NAME[name]
        if name in reached or step.produced(manifest):
            return
        reached.add(name)
        for need in step.needs:
            visit(need)

    unknown = set(targets) - TARGETS
    if unknown:
        raise UnknownTargetError(f'no step is named {sorted(unknown)}')
    for target in targets:
        visit(target)
    return frozenset(reached)


def _step_has_work(
    step: _Step,
    manifest: litcache_pb2.Manifest,
    log: work_pb2.JobLog,
    *,
    attempt: joblog.Attempt,
    now: datetime.datetime,
    deadline: datetime.timedelta,
) -> bool:
    inputs = step.inputs(manifest)
    if inputs is None or step.produced(manifest):
        return False
    verdict = joblog.assess(log, step=step.name, inputs=inputs, attempt=attempt, now=now, deadline=deadline)
    return verdict in (joblog.Verdict.WORK, joblog.Verdict.SPENT, joblog.Verdict.BEGUN)


def _runs(step: _Step, reached: Collection[str]) -> bool:
    """Whether a request reaching `reached` runs `step` when it has work: a reached step, or any free one."""
    return step.name in reached or not step.spends


def _has_work(run: _Run, step: _Step, manifest: litcache_pb2.Manifest, log: work_pb2.JobLog) -> bool:
    return _step_has_work(step, manifest, log, attempt=run.attempt, now=run.now(), deadline=run.deadline)


# An attempt that is no delivery: assessing as it, a begun entry is never its own nor an earlier delivery's.
_OBSERVER = joblog.Attempt(id='00000000-0000-0000-0000-000000000000', queue='', task='', retry_count=0)


@dataclasses.dataclass(frozen=True)
class Outstanding:
    """What a paper still needs, as a request arriving now would find it.

    Attributes:
        free: A free step has work.
        main_text: The main text is not met, and a step towards it has work: one of its routes, or locating
            the PDF its transcription reads.
    """

    free: bool
    main_text: bool


def outstanding(
    manifest: litcache_pb2.Manifest, log: work_pb2.JobLog, *, now: datetime.datetime, deadline: datetime.timedelta
) -> Outstanding:
    """What `manifest`'s paper still needs, by the same step tests `run` chooses its steps with.

    A step running in a live attempt has no work here: that attempt's own task picks it up if it dies.
    """

    def has_work(step: _Step) -> bool:
        return _step_has_work(step, manifest, log, attempt=_OBSERVER, now=now, deadline=deadline)

    towards_main_text = (_STEPS_BY_NAME[name] for name in (XML_STEP, LOCATE_STEP, TRANSCRIPTION_STEP))
    return Outstanding(
        free=any(has_work(step) for step in STEPS if not step.spends),
        main_text=not _main_text_met(manifest) and any(has_work(step) for step in towards_main_text),
    )


def would_run(
    targets: Collection[str],
    manifest: litcache_pb2.Manifest,
    log: work_pb2.JobLog,
    *,
    now: datetime.datetime,
    deadline: datetime.timedelta,
) -> frozenset[str]:
    """The steps with work that a request naming `targets` would run on the paper as it stands.

    One step's output can change what a later one finds: XML found leaves the transcription without work, and a
    PDF located gives it some.

    Raises:
        UnknownTargetError: If a target is neither a step nor the main text.
    """
    reached = reach(targets, manifest)
    return frozenset(
        step.name
        for step in STEPS
        if _runs(step, reached) and _step_has_work(step, manifest, log, attempt=_OBSERVER, now=now, deadline=deadline)
    )


def _read_manifest(bucket: gcs.Bucket, doc_id: str) -> litcache_pb2.Manifest:
    try:
        return litcache_pb2.Manifest.FromString(bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())
    except api_exceptions.NotFound as e:
        raise PaperNotInCorpusError(doc_id) from e


def _next_step(
    run: _Run, targets: Collection[str], manifest: litcache_pb2.Manifest, ran: Collection[str]
) -> _Step | None:
    """The first reached step with work, else the first free one: a request's targets go first.

    A step this request has run is not run again: it committed, settled, or found the step another attempt's, and a
    log read since then can only show that attempt dying or giving up, which is its own task's to pick up.
    """
    log = joblog.read(run.bucket, run.doc_id)
    reached = reach(targets, manifest)
    with_work = [
        step for step in STEPS if step.name not in ran and _runs(step, reached) and _has_work(run, step, manifest, log)
    ]
    return next((s for s in with_work if s.name in reached), next(iter(with_work), None))


def _route_settlement(manifest: litcache_pb2.Manifest, log: work_pb2.JobLog) -> list[work_pb2.JobLogEntry] | None:
    """The settled entries closing every route to the main text, or None while one has not settled.

    The transcription route has nothing to work on while the paper holds no PDF, which settles it once locating one
    has settled.
    """
    settled: list[work_pb2.JobLogEntry] = []
    for name in (XML_STEP, LOCATE_STEP, TRANSCRIPTION_STEP):
        step = _STEPS_BY_NAME[name]
        inputs = step.inputs(manifest)
        if name == LOCATE_STEP and _pdf_source(manifest) is not None:
            continue  # a PDF is held, so the transcription's own settlement is what counts
        if inputs is None:
            continue  # no PDF: the locate step's settlement, above, stands for it
        entry = joblog.settlement(log, step=name, inputs=inputs)
        if entry is None:
            return None
        settled.append(entry)
    return settled


def awaits_marker(manifest: litcache_pb2.Manifest, log: work_pb2.JobLog) -> bool:
    """Whether the paper is not READY and every route to its main text has settled, so its marker is due."""
    return not _main_text_met(manifest) and _route_settlement(manifest, log) is not None


def _settle_readiness(run: _Run) -> outcome.Readiness:
    """The paper's readiness once the loop stops, writing the terminal marker if every route settled with no text."""
    manifest = _read_manifest(run.bucket, run.doc_id)
    if _main_text_met(manifest):
        return outcome.Readiness.READY
    settled = _route_settlement(manifest, joblog.read(run.bucket, run.doc_id))
    if settled is None:
        return outcome.Readiness.PENDING
    failures = [e.detail for e in settled if e.outcome == work_pb2.SETTLED_OUTCOME_PERMANENT_FAILURE]
    kind = outcome.OutcomeKind.FAILED if failures else outcome.OutcomeKind.NO_FULL_TEXT
    marker = outcome.FetchOutcome(kind=kind, at=run.now(), error='; '.join(failures))
    held = outcome.read_outcome(run.bucket, run.doc_id)
    if held is None or (held.kind, held.error) != (marker.kind, marker.error):
        outcome.write_outcome(run.bucket, run.doc_id, marker)
    return outcome.terminal_readiness(marker)


async def run(
    bucket: gcs.Bucket,
    doc_id: str,
    *,
    targets: Collection[str],
    attempt: joblog.Attempt,
    convert_pdf: ocr.PdfConverter,
    resolver: resolvers.Resolver | None = None,
    fetch: Fetcher = oa.fetch_oa_source,
    fetch_pdf: PdfFetcher = oa.fetch_oa_pdf,
    fetch_supplementary: SupplementFetcher = oa.fetch_supplementary_resolving,
    now: Callable[[], datetime.datetime] = _utcnow,
    deadline: datetime.timedelta = work.DISPATCH_DEADLINE,
) -> outcome.Readiness:
    """Run every step `targets` reach and every free step, until none has work; return the paper's readiness.

    Each step is committed, or settled in the job log, before the next is chosen from a fresh read of the manifest,
    so a retry after a crash resumes where this left off. A spending step runs only when `targets` reach it.

    Args:
        bucket: The cache bucket holding the paper.
        doc_id: The paper.
        targets: The request's targets: step names, or the main text. Empty reaches no spending step.
        attempt: This request, as the job log names it.
        convert_pdf: The PDF → markdown converter the transcription step calls.
        resolver: The litfetch id resolver the XML fetch uses to fill missing ids; defaults to
            `oa.default_resolver` (doi/pmid → pmcid).
        fetch: The OA body fetcher. Injected so tests run offline.
        fetch_pdf: The open-access PDF locator. Injected so tests run offline.
        fetch_supplementary: The supplementary-file fetcher, given the paper's ids and `resolver`. Injected so
            tests run offline.
        now: The clock for log entries, captured/created times and the marker.
        deadline: The dispatch deadline, past which a begun attempt has been killed.

    Returns:
        READY once the main text is met; NO_FULL_TEXT or FAILED once every route to it has settled, the marker
        written; PENDING otherwise — a route running elsewhere, or not reached by this request.

    Raises:
        PaperNotInCorpusError: If the corpus holds no manifest for `doc_id` — settled, not retryable.
        UnknownTargetError: If a target is neither a step nor the main text.
        google.api_core.exceptions.NotFound: If an object the manifest names is absent (a seed
            revision blob, say). Operational, so it propagates and stays retryable.
        Exception: A transient failure from the converter's provider, from the OA fetch, or from locating a PDF
            (`oa.UpstreamUnavailableError`) propagates, so the worker returns 5xx and Cloud Tasks retries; a
            transcription it interrupts is logged failed first, as paid for when the converter says the model had
            answered (`ocr.SpentError`). A transcription refused at the spend limit is logged and left undone,
            and does not raise.
        writer.ConcurrentWriteError: A manifest write lost its generation race the whole retry budget (retryable).
        joblog.LogContentionError: A log append did the same (retryable).
    """
    work_run = _Run(
        bucket, doc_id, attempt, resolver, fetch, fetch_pdf, fetch_supplementary, convert_pdf, now, deadline
    )
    manifest = _read_manifest(bucket, doc_id)
    reach(targets, manifest)  # an unknown target fails before any work
    # Every write checks the whole manifest, so one already breaking its constraints refuses every write:
    # settle it before paying for a fetch or a transcription whose result could not be committed.
    try:
        writer.validate_manifest(manifest)
    except writer.ManifestRefusedError as e:
        if _main_text_met(manifest):
            return outcome.Readiness.READY
        outcome.write_outcome(
            bucket, doc_id, outcome.FetchOutcome(kind=outcome.OutcomeKind.FAILED, at=now(), error=str(e))
        )
        return outcome.Readiness.FAILED

    ran: set[str] = set()
    while (step := _next_step(work_run, targets, manifest, ran)) is not None:
        ran.add(step.name)
        inputs = step.inputs(manifest)
        if inputs is None:
            raise RuntimeError(f'{step.name} was chosen for {doc_id} with nothing to work on')
        try:
            await step.run(work_run, manifest, inputs)
        except _SettledError as settled:
            work_run.settle(step.name, inputs, settled)
        manifest = _read_manifest(bucket, doc_id)
    return _settle_readiness(work_run)
