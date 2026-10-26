"""Tests for the supplied-paper deposit command (`tools.litcache.deposit`).

The fail-loud helpers (id resolution, licence basis, PDF probe, metadata build) run pure — no
network, no container. The end-to-end deposit runs against a real fake-gcs-server bucket (the
writer's own fixture) and a throwaway Postgres (the crosswalk's own pattern), so the
content-addressed write and the lookup-before-mint idempotence are exercised, not mocked. Both
integration paths are Docker-gated and skip without a daemon.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import pathlib
import typing

import pg8000.dbapi
import pypdfium2
import pytest
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.cloud import tasks_v2
from google.protobuf import json_format

from themis.litcache import crosswalk, enqueue, sourced_metadata, writer
from themis.litcache.models import litcache_pb2, work_pb2
from tools.litcache import deposit

_TITLE = 'A supplied paper about a variant'
_LICENCE = 'https://creativecommons.org/licenses/by/4.0/'


def _pdf_bytes(size: int = 200) -> bytes:
    """A genuinely loadable one-page PDF (no text layer), built through pdfium itself.

    `size` sets the page dimensions so two calls with different sizes yield distinct bytes.
    """
    document = pypdfium2.PdfDocument.new()
    document.new_page(size, size)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class _TasksClient:
    """Stands in for Cloud Tasks: records each task, and refuses a name it already holds."""

    def __init__(self, *, refusal: Exception | None = None) -> None:
        self.tasks: list[tasks_v2.Task] = []
        self._refusal = refusal

    def create_task(self, *, parent: str, task: tasks_v2.Task) -> tasks_v2.Task:  # noqa: ARG002 — the client's signature
        if self._refusal is not None:
            raise self._refusal
        if any(t.name == task.name for t in self.tasks):
            raise api_exceptions.AlreadyExists(task.name)
        self.tasks.append(task)
        return task


_TARGET = enqueue.ConversionTarget(
    queue_path='projects/p/locations/l/queues/themis-convert-bulk',
    worker_url='https://worker.example',
    invoker_service_account_email='invoker@p.iam.gserviceaccount.com',
)


def _metadata(title: str = _TITLE) -> bytes:
    return deposit._build_metadata(title=title, metadata_file=None)


def _deposit(
    bucket: gcs.Bucket,
    conn: crosswalk.Connection,
    *,
    ids: list[str],
    pdf: bytes,
    tasks: _TasksClient | None = None,
    licence: str = _LICENCE,
) -> deposit.DepositResult:
    client = _TasksClient() if tasks is None else tasks
    return deposit.deposit(
        bucket,
        conn,
        pdf_bytes=pdf,
        external_ids=ids,
        licence=licence,
        licence_basis=litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
        metadata=_metadata(),
        now=datetime.datetime.now(tz=datetime.UTC),
        enqueuer=enqueue.Enqueuer(typing.cast('tasks_v2.CloudTasksClient', client), _TARGET),
    )


# --- Pure fail-loud paths -------------------------------------------------------------------


def test_resolve_ids_requires_at_least_one() -> None:
    with pytest.raises(deposit.DepositError, match='at least one'):
        deposit._resolve_ids([])


def test_resolve_ids_rejects_an_unscheme_qualified_id() -> None:
    with pytest.raises(deposit.DepositError, match='scheme-qualified'):
        deposit._resolve_ids(['10.1016/j.ajhg.2021.03.001'])


def test_resolve_ids_rejects_an_unrecognised_id() -> None:
    with pytest.raises(deposit.DepositError, match='not a recognised'):
        deposit._resolve_ids(['doi:not-a-doi'])


def test_resolve_ids_rejects_a_scheme_the_value_contradicts() -> None:
    # A PMCID value under the pmid scheme: identity classifies it as pmcid, so the claim would be wrong.
    with pytest.raises(deposit.DepositError, match='recognised as pmcid'):
        deposit._resolve_ids(['pmid:PMC7'])


def test_resolve_ids_yields_identity_over_recognised_ids() -> None:
    ident = deposit._resolve_ids(['doi:10.1016/j.ajhg.2021.03.001', 'pmid:33811808'])
    assert set(ident.mint_keys) == {'doi:10.1016/j.ajhg.2021.03.001', 'pmid:33811808'}
    # DOI outranks PMID for the claim key (identity's precedence).
    assert ident.claim_key == 'doi:10.1016/j.ajhg.2021.03.001'


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('ARTIFACT', litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT),
        ('ASSERTED', litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED),
    ],
)
def test_licence_basis_maps_each_value(name: str, expected: litcache_pb2.LicenceBasis) -> None:
    assert deposit._licence_basis(name) == expected


def test_licence_basis_rejects_an_unknown_value() -> None:
    with pytest.raises(deposit.DepositError, match='ARTIFACT or ASSERTED'):
        deposit._licence_basis('PUBLIC_DOMAIN')


def test_pdf_probe_rejects_a_non_pdf() -> None:
    with pytest.raises(deposit.DepositError, match='not a PDF'):
        deposit._pdf_has_text_layer(b'this is not a pdf')


def test_pdf_probe_reads_a_loadable_pdf() -> None:
    # A page with no drawn text has no recoverable text layer; the point is it loads and reports.
    assert deposit._pdf_has_text_layer(_pdf_bytes()) is False


def test_resolve_ids_rejects_a_value_normalisation_would_alter() -> None:
    # A DOI ending in .pdf: identity's seed-key normaliser would strip the suffix and mint a
    # truncated id; the deposit refuses rather than silently claim the wrong DOI.
    with pytest.raises(deposit.DepositError, match='altered by normalisation'):
        deposit._resolve_ids(['doi:10.1/abc.pdf'])


@pytest.mark.parametrize('ids', [['doi:10.1/a', 'doi:10.2/b'], ['doi:10.1/ABC', 'doi:10.1/abc']])
def test_manifest_ids_refuse_two_ids_of_one_scheme(ids: list[str]) -> None:
    with pytest.raises(deposit.DepositError, match='two doi ids'):
        deposit._manifest_external_ids(deposit._resolve_ids(ids))


def test_build_metadata_from_title_names_the_paper() -> None:
    assert sourced_metadata.read_metadata(_metadata('The paper title')).title == 'The paper title'


def test_build_metadata_rejects_an_empty_title() -> None:
    with pytest.raises(deposit.DepositError, match='no title'):
        deposit._build_metadata(title='', metadata_file=None)


def test_build_metadata_requires_exactly_one_of_title_or_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(deposit.DepositError, match='exactly one'):
        deposit._build_metadata(title=None, metadata_file=None)
    with pytest.raises(deposit.DepositError, match='exactly one'):
        deposit._build_metadata(title='t', metadata_file=tmp_path / 'm.json')


@pytest.mark.parametrize('suffix', ['.json', '.pb'])
def test_build_metadata_fills_from_a_metadata_file(tmp_path: pathlib.Path, suffix: str) -> None:
    work = {
        'title': ['The <i>BRCA1</i> gene'],
        'author': [{'given': 'Jane', 'family': 'Doe'}],
        'issued': {'date-parts': [[2020]]},
    }
    source = litcache_pb2.MetadataSource(crossref_json=json.dumps(work))
    path = tmp_path / f'meta{suffix}'
    if suffix == '.json':
        path.write_text(json_format.MessageToJson(source), 'utf-8')
    else:
        path.write_bytes(source.SerializeToString())
    metadata = sourced_metadata.read_metadata(deposit._build_metadata(title=None, metadata_file=path))
    assert metadata.title == 'The BRCA1 gene'
    assert [a.name for a in metadata.authors] == ['Jane Doe']
    assert metadata.year == '2020'


def test_build_metadata_rejects_a_source_with_no_record(tmp_path: pathlib.Path) -> None:
    empty = tmp_path / 'empty.json'
    empty.write_text('{}', 'utf-8')
    with pytest.raises(deposit.DepositError, match='no index record'):
        deposit._build_metadata(title=None, metadata_file=empty)


def test_build_metadata_rejects_an_unknown_suffix(tmp_path: pathlib.Path) -> None:
    weird = tmp_path / 'meta.txt'
    weird.write_text('anything', 'utf-8')
    with pytest.raises(deposit.DepositError, match=r'\.json or \.pb'):
        deposit._build_metadata(title=None, metadata_file=weird)


def test_the_default_bucket_is_the_project_corpus() -> None:
    args = deposit._parse_args(['paper.pdf', '--project', 'cpg-themis-prod'])
    assert args.bucket == 'cpg-themis-prod-fulltext'


# --- End-to-end deposit (Docker-gated: fake-gcs-server bucket + throwaway Postgres) ---------


def _paper_dirs(bucket: gcs.Bucket) -> set[str]:
    """The distinct `papers/{doc_id}` prefixes that hold a committed manifest."""
    return {
        blob.name.rsplit('/', 1)[0]
        for blob in bucket.list_blobs(prefix=f'{writer.PAPERS_DIR}/')
        if blob.name.endswith(f'/{writer.MANIFEST_NAME}')
    }


def _crosswalk_rows(conn: pg8000.dbapi.Connection) -> dict[str, str]:
    with contextlib.closing(conn.cursor()) as cur:
        cur.execute('SELECT external_id, doc_id FROM litcache.crosswalk')
        return dict(cur.fetchall())


def test_deposit_writes_the_paper_directory(gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection) -> None:
    pdf = _pdf_bytes()
    result = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc', 'pmid:33811808'], pdf=pdf)

    assert result.minted is True
    assert result.written is True

    pdf_hash = hashlib.sha256(pdf).hexdigest()
    source_path = writer.source_revision_path(
        result.doc_id, 'pdf', pdf_hash, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF
    )
    assert gcs_bucket.blob(source_path).exists()
    assert gcs_bucket.blob(writer.metadata_path(result.doc_id)).exists()
    assert gcs_bucket.blob(writer.manifest_path(result.doc_id)).exists()

    manifest = result.manifest
    assert manifest.doc_id == result.doc_id
    assert manifest.claim_key == 'doi:10.1/abc'
    assert manifest.external_ids.doi == '10.1/abc'
    assert manifest.external_ids.pmid == '33811808'
    (source,) = manifest.sources
    assert source.access.WhichOneof('kind') == 'institution_captured'
    (revision,) = source.revisions
    assert revision.kind == litcache_pb2.SourceKind.SOURCE_KIND_UPLOAD
    assert revision.has_text_layer is False


def test_deposit_reuses_the_doc_id_and_does_not_remint(gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection) -> None:
    ids = ['doi:10.1/abc', 'pmid:33811808']
    pdf = _pdf_bytes()
    first = _deposit(gcs_bucket, conn, ids=ids, pdf=pdf)
    rows_after_first = _crosswalk_rows(conn)

    second = _deposit(gcs_bucket, conn, ids=ids, pdf=pdf)

    assert second.doc_id == first.doc_id
    assert second.minted is False  # the existing claim was reused, not re-minted
    assert second.written is False  # the manifest already existed
    assert _crosswalk_rows(conn) == rows_after_first  # no new claim rows
    assert _paper_dirs(gcs_bucket) == {writer.paper_dir(first.doc_id)}  # one paper directory


def test_deposit_mints_a_fresh_doc_id_for_new_ids(gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection) -> None:
    first = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=_pdf_bytes())
    second = _deposit(gcs_bucket, conn, ids=['doi:10.2/xyz'], pdf=_pdf_bytes())

    assert first.doc_id != second.doc_id
    assert first.minted is True
    assert second.minted is True
    assert _paper_dirs(gcs_bucket) == {writer.paper_dir(first.doc_id), writer.paper_dir(second.doc_id)}


def test_deposit_refuses_ids_that_bridge_two_papers(gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection) -> None:
    # Two separate deposits, then one whose ids span both: the deferred cross-paper equivalence path.
    first = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=_pdf_bytes())
    second = _deposit(gcs_bucket, conn, ids=['pmid:33811808'], pdf=_pdf_bytes())
    assert first.doc_id != second.doc_id

    with pytest.raises(deposit.DepositError, match=r'bridge separate works|multiple doc_ids'):
        _deposit(gcs_bucket, conn, ids=['doi:10.1/abc', 'pmid:33811808'], pdf=_pdf_bytes())


def test_deposit_refuses_two_ids_of_one_scheme_before_minting(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection
) -> None:
    with pytest.raises(deposit.DepositError, match='two doi ids'):
        _deposit(gcs_bucket, conn, ids=['doi:10.1/a', 'doi:10.2/b'], pdf=_pdf_bytes())
    assert not crosswalk.lookup(conn, ['doi:10.1/a', 'doi:10.2/b'])


def test_deposit_completes_an_interrupted_run(gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection) -> None:
    # An earlier run minted the claim but crashed before the manifest (an orphan crosswalk row).
    # A re-run adopts that doc_id, mints the not-yet-claimed ids onto it, and commits the manifest.
    orphan = crosswalk.mint(conn, ['doi:10.1/abc'])
    assert not gcs_bucket.blob(writer.manifest_path(orphan.doc_id)).exists()

    result = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc', 'pmid:33811808'], pdf=_pdf_bytes())

    assert result.doc_id == orphan.doc_id  # adopted the claimed doc_id, not a fresh one
    assert result.minted is False
    assert result.written is True  # the manifest is written this time
    assert result.manifest.external_ids.doi == '10.1/abc'
    assert result.manifest.external_ids.pmid == '33811808'
    assert set(_crosswalk_rows(conn).values()) == {orphan.doc_id}  # both ids under the one doc_id


def test_deposit_refuses_new_ids_on_a_committed_paper(gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection) -> None:
    # Adding an id to a committed paper would leave the crosswalk holding an id the manifest never
    # records — refused, so the crosswalk stays rebuildable from the manifests.
    first = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=_pdf_bytes())
    rows_after_first = _crosswalk_rows(conn)

    with pytest.raises(deposit.DepositError, match='already committed'):
        _deposit(gcs_bucket, conn, ids=['doi:10.1/abc', 'pmid:33811808'], pdf=_pdf_bytes())

    assert _crosswalk_rows(conn) == rows_after_first  # the new id was never claimed
    assert _paper_dirs(gcs_bucket) == {writer.paper_dir(first.doc_id)}


def test_a_pdf_deposited_for_a_committed_paper_joins_it(gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection) -> None:
    # The deposit a paper written off as having no text gets: same ids, a PDF it did not hold.
    pdfs = [_pdf_bytes(size=200), _pdf_bytes(size=300)]
    first = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=pdfs[0])
    second = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=pdfs[1])

    assert second.doc_id == first.doc_id
    assert second.written is False
    held = {r.hash for src in second.manifest.sources for r in src.revisions}
    assert {hashlib.sha256(pdf).hexdigest() for pdf in pdfs} <= held


def test_a_pdf_deposited_on_other_terms_joins_a_lineage_of_its_own(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection
) -> None:
    _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=_pdf_bytes(size=200))

    result = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=_pdf_bytes(size=300), licence='all rights reserved')

    assert sorted(s.handle for s in result.manifest.sources) == ['pdf', 'pdf-2']


def test_a_deposit_refuses_an_id_spelt_otherwise_than_the_committed_paper(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection
) -> None:
    # The crosswalk folds a DOI's case, so the claim finds the paper; its manifest spells the DOI otherwise.
    _deposit(gcs_bucket, conn, ids=['doi:10.1/ABC'], pdf=_pdf_bytes(size=200))
    tasks = _TasksClient()

    with pytest.raises(deposit.DepositError, match='other spellings'):
        _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=_pdf_bytes(size=300), tasks=tasks)
    assert not tasks.tasks


def test_a_deposit_the_worker_cannot_be_asked_for_says_which_paper(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection
) -> None:
    refusing = _TasksClient(refusal=api_exceptions.PermissionDenied('no enqueue grant'))

    with pytest.raises(deposit.DepositError, match='could not be asked') as raised:
        _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=_pdf_bytes(), tasks=refusing)
    (doc_id,) = crosswalk.lookup(conn, ['doi:10.1/abc']).values()
    assert doc_id in str(raised.value)


def test_a_deposit_asks_for_the_papers_main_text_under_the_generation_it_committed(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection
) -> None:
    tasks = _TasksClient()
    pdf = _pdf_bytes()

    result = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=pdf, tasks=tasks)
    again = _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=pdf, tasks=tasks)

    blob = gcs_bucket.get_blob(writer.manifest_path(result.doc_id))
    assert blob is not None
    (task,) = tasks.tasks
    assert task.name.endswith(f'/tasks/{result.doc_id}--main_text--gen-{blob.generation}')
    request = work_pb2.WorkRequest.FromString(task.http_request.body)
    assert list(request.targets) == ['main_text']
    assert (result.asked, again.asked) == (True, False)  # the same commit, asked for once
