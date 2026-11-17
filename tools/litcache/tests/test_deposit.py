"""Tests for the supplied-paper deposit command (`tools.litcache.deposit`).

CLI validation refusals are exercised through ``deposit.main()`` before any cloud client opens.
End-to-end deposits run through ``deposit.main()`` and ``deposit.deposit()`` against a real
``fake-gcs-server`` bucket and a throwaway Postgres instance.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import io
import json
import pathlib
import typing
import uuid
from collections.abc import Callable

import pg8000.dbapi
import pypdfium2
import pytest
from google.api_core import exceptions as api_exceptions
from google.auth import credentials as google_credentials
from google.cloud import storage as gcs
from google.cloud import tasks_v2
from google.protobuf import json_format

from themis.litcache import crosswalk, enqueue, metadata_fields, sourced_metadata, writer
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


class _TrackingPool:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


_TARGET = enqueue.ConversionTarget(
    queue_path='projects/p/locations/l/queues/themis-convert-bulk',
    worker_url='https://worker.example',
    invoker_service_account_email='invoker@p.iam.gserviceaccount.com',
)


def _metadata(title: str = _TITLE) -> bytes:
    source = litcache_pb2.MetadataSource(crossref_json=json.dumps({'title': [title]}))
    sourced = litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
    return sourced_metadata.to_canonical_bytes(sourced)


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


def _wire_main_backends(
    monkeypatch: pytest.MonkeyPatch,
    gcs_client: gcs.Client,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> tuple[_TasksClient, _TrackingPool, list[tuple[str, str, str]]]:
    tasks = _TasksClient()
    pool = _TrackingPool()
    sql_dials: list[tuple[str, str, str]] = []

    def dial_sql(_pool: object, *, connection_name: str, database: str, db_user: str) -> pg8000.dbapi.Connection:
        sql_dials.append((connection_name, database, db_user))
        return crosswalk_connect()

    monkeypatch.setattr(deposit.gcs, 'Client', lambda project: gcs_client)  # noqa: ARG005
    monkeypatch.setattr(deposit.clu, 'impersonated', lambda _sa: google_credentials.AnonymousCredentials())
    monkeypatch.setattr(
        deposit.clu, 'run_service_url', lambda service, *, project: f'https://{service}.{project}.run.app'
    )
    monkeypatch.setattr(deposit.tasks_v2, 'CloudTasksClient', lambda **_kwargs: tasks)
    monkeypatch.setattr(deposit.connector, 'Connector', lambda **_kwargs: pool)
    monkeypatch.setattr(deposit.sql, 'iam_connect', dial_sql)
    return tasks, pool, sql_dials


# --- CLI validation refusals via deposit.main() ---------------------------------------------


@pytest.mark.parametrize(
    ('extra_args', 'expected_reason'),
    [
        (
            ['--licence', _LICENCE, '--licence-basis', 'ASSERTED', '--title', _TITLE],
            'at least one --id is required',
        ),
        (
            ['--id', 'doi:10.1/abc', '--licence', '   ', '--licence-basis', 'ASSERTED', '--title', _TITLE],
            '--licence is required',
        ),
        (
            ['--id', 'doi:10.1/abc', '--licence', _LICENCE, '--title', _TITLE],
            '--licence-basis is required',
        ),
        (
            ['--id', 'doi:10.1/abc', '--licence', _LICENCE, '--licence-basis', 'PUBLIC_DOMAIN', '--title', _TITLE],
            'ARTIFACT or ASSERTED',
        ),
        (
            ['--id', 'doi:10.1/abc', '--licence', _LICENCE, '--licence-basis', 'ASSERTED', '--title', ''],
            'no title',
        ),
        (
            ['--id', 'doi:10.1/abc', '--licence', _LICENCE, '--licence-basis', 'ASSERTED'],
            'exactly one',
        ),
    ],
)
def test_main_refuses_invalid_cli_inputs(
    tmp_path: pathlib.Path, extra_args: list[str], expected_reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    pdf_path = tmp_path / 'paper.pdf'
    pdf_path.write_bytes(b'%PDF-stub')

    code = deposit.main([str(pdf_path), '--project', 'cpg-themis-test', *extra_args])

    assert code == 1
    assert expected_reason in capsys.readouterr().err


def test_main_refuses_both_title_and_metadata_file(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    pdf_path = tmp_path / 'paper.pdf'
    pdf_path.write_bytes(b'%PDF-stub')

    code = deposit.main(
        [
            str(pdf_path),
            '--project',
            'cpg-themis-test',
            '--id',
            'doi:10.1/abc',
            '--licence',
            _LICENCE,
            '--licence-basis',
            'ASSERTED',
            '--title',
            _TITLE,
            '--metadata-file',
            str(tmp_path / 'm.json'),
        ]
    )

    assert code == 1
    assert 'exactly one' in capsys.readouterr().err


def test_main_refuses_a_missing_pdf_path(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = deposit.main(
        [
            str(tmp_path / 'absent.pdf'),
            '--project',
            'cpg-themis-test',
            '--id',
            'doi:10.1/abc',
            '--licence',
            _LICENCE,
            '--licence-basis',
            'ASSERTED',
            '--title',
            _TITLE,
        ]
    )

    assert code == 1
    assert 'no such PDF' in capsys.readouterr().err


@pytest.mark.parametrize(
    ('filename', 'content', 'expected_reason'),
    [
        ('empty.json', '{}', 'no index record'),
        ('meta.txt', 'anything', '.json or .pb'),
    ],
)
def test_main_refuses_bad_metadata_files(
    tmp_path: pathlib.Path,
    filename: str,
    content: str,
    expected_reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf_path = tmp_path / 'paper.pdf'
    pdf_path.write_bytes(b'%PDF-stub')
    meta_path = tmp_path / filename
    meta_path.write_text(content, 'utf-8')

    code = deposit.main(
        [
            str(pdf_path),
            '--project',
            'cpg-themis-test',
            '--id',
            'doi:10.1/abc',
            '--licence',
            _LICENCE,
            '--licence-basis',
            'ASSERTED',
            '--metadata-file',
            str(meta_path),
        ]
    )

    assert code == 1
    assert expected_reason in capsys.readouterr().err


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


def test_main_defaults_bucket_to_project_corpus_and_builds_title_metadata(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gcs_client: gcs.Client,
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
) -> None:
    project = f'themis-test-{uuid.uuid4().hex[:8]}'
    bucket = gcs_client.bucket(f'{project}-fulltext')
    bucket.create()
    pdf_path = tmp_path / 'paper.pdf'
    pdf_path.write_bytes(_pdf_bytes())
    tasks, pool, sql_dials = _wire_main_backends(monkeypatch, gcs_client, crosswalk_connect)

    code = deposit.main(
        [
            str(pdf_path),
            '--project',
            project,
            '--id',
            'doi:10.1016/j.ajhg.2021.03.001',
            '--licence',
            _LICENCE,
            '--licence-basis',
            'ASSERTED',
            '--title',
            'The paper title',
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert pool.closed is True
    assert sql_dials == [(f'{project}:australia-southeast1:themis-sql', 'themis', f'themis-clu@{project}.iam')]
    doc_id = _crosswalk_rows(conn)['doi:10.1016/j.ajhg.2021.03.001']
    assert tasks.tasks[0].name.startswith(
        f'projects/{project}/locations/australia-southeast1/queues/themis-convert-bulk/tasks/{doc_id}--main_text--gen-'
    )
    assert f'gs://{project}-fulltext/papers/{doc_id}/manifest.pb' in out
    stored_meta = sourced_metadata.read_metadata(bucket.blob(writer.metadata_path(doc_id)).download_as_bytes())
    assert stored_meta.title == 'The paper title'


@pytest.mark.parametrize(
    ('basis_flag', 'expected_basis'),
    [
        ('ARTIFACT', litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT),
        ('ASSERTED', litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED),
    ],
)
def test_main_records_requested_licence_basis(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    gcs_client: gcs.Client,
    gcs_bucket: gcs.Bucket,
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    basis_flag: str,
    expected_basis: litcache_pb2.LicenceBasis,
) -> None:
    assert gcs_bucket.name is not None
    pdf_path = tmp_path / 'paper.pdf'
    pdf_path.write_bytes(_pdf_bytes())
    _wire_main_backends(monkeypatch, gcs_client, crosswalk_connect)

    code = deposit.main(
        [
            str(pdf_path),
            '--project',
            'cpg-themis-test',
            '--bucket',
            gcs_bucket.name,
            '--id',
            'doi:10.1/basis',
            '--licence',
            _LICENCE,
            '--licence-basis',
            basis_flag,
            '--title',
            _TITLE,
        ]
    )

    assert code == 0
    doc_id = _crosswalk_rows(conn)['doi:10.1/basis']
    manifest = litcache_pb2.Manifest.FromString(gcs_bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())
    assert manifest.sources[0].licence_basis == expected_basis


_CROSSREF_SOURCE = litcache_pb2.MetadataSource(
    crossref_json=json.dumps(
        {
            'title': ['The <i>BRCA1</i> gene'],
            'author': [{'given': 'Jane', 'family': 'Doe'}],
            'issued': {'date-parts': [[2020]]},
        }
    )
)


@pytest.mark.parametrize(
    ('filename', 'meta_bytes'),
    [
        ('meta.json', json_format.MessageToJson(_CROSSREF_SOURCE).encode('utf-8')),
        ('meta.pb', _CROSSREF_SOURCE.SerializeToString()),
    ],
)
def test_main_fills_metadata_from_metadata_file(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    gcs_client: gcs.Client,
    gcs_bucket: gcs.Bucket,
    conn: pg8000.dbapi.Connection,
    crosswalk_connect: Callable[[], pg8000.dbapi.Connection],
    filename: str,
    meta_bytes: bytes,
) -> None:
    assert gcs_bucket.name is not None
    meta_path = tmp_path / filename
    meta_path.write_bytes(meta_bytes)
    pdf_path = tmp_path / 'paper.pdf'
    pdf_path.write_bytes(_pdf_bytes())
    _wire_main_backends(monkeypatch, gcs_client, crosswalk_connect)

    code = deposit.main(
        [
            str(pdf_path),
            '--project',
            'cpg-themis-test',
            '--bucket',
            gcs_bucket.name,
            '--id',
            'doi:10.1/brca1',
            '--licence',
            _LICENCE,
            '--licence-basis',
            'ASSERTED',
            '--metadata-file',
            str(meta_path),
        ]
    )

    assert code == 0
    doc_id = _crosswalk_rows(conn)['doi:10.1/brca1']
    stored = sourced_metadata.read_metadata(gcs_bucket.blob(writer.metadata_path(doc_id)).download_as_bytes())
    assert stored.title == 'The BRCA1 gene'
    assert [a.name for a in stored.authors] == ['Jane Doe']
    assert stored.year == '2020'


@pytest.mark.parametrize(
    ('ids', 'reason'),
    [
        ([], 'at least one'),
        (['10.1016/j.ajhg.2021.03.001'], 'scheme-qualified'),
        (['doi:not-a-doi'], 'not a recognised'),
        (['pmid:PMC7'], 'recognised as pmcid'),
        (['doi:10.1/abc.pdf'], 'altered by normalisation'),
    ],
)
def test_deposit_refuses_invalid_external_ids_before_minting(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection, ids: list[str], reason: str
) -> None:
    with pytest.raises(deposit.DepositError, match=reason):
        _deposit(gcs_bucket, conn, ids=ids, pdf=_pdf_bytes())
    assert _crosswalk_rows(conn) == {}


@pytest.mark.parametrize(
    ('pdf_data', 'reason'),
    [
        (b'this is not a pdf', 'not a PDF'),
        (b'%PDF-1.7\nnot loadable', 'not a loadable PDF'),
    ],
)
def test_deposit_refuses_unreadable_pdf_before_minting(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection, pdf_data: bytes, reason: str
) -> None:
    with pytest.raises(deposit.DepositError, match=reason):
        _deposit(gcs_bucket, conn, ids=['doi:10.1/abc'], pdf=pdf_data)
    assert _crosswalk_rows(conn) == {}


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


@pytest.mark.parametrize('ids', [['doi:10.1/a', 'doi:10.2/b'], ['doi:10.1/ABC', 'doi:10.1/abc']])
def test_deposit_refuses_two_ids_of_one_scheme_before_minting(
    gcs_bucket: gcs.Bucket, conn: pg8000.dbapi.Connection, ids: list[str]
) -> None:
    with pytest.raises(deposit.DepositError, match='two doi ids'):
        _deposit(gcs_bucket, conn, ids=ids, pdf=_pdf_bytes())
    assert _crosswalk_rows(conn) == {}


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
