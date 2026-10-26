r"""Deposit a supplied paper (a held PDF) into the litcache full-text corpus.

The deposit step of the supplied-literature runbook: a PDF handed to us, not fetched from a
source, is placed in the corpus and transcribed. It mints (or reuses) the
paper's `doc_id` in the crosswalk, wraps the PDF and its bibliographic record into a
`writer.PaperInput`, and commits it with `writer.write_paper` — the same placement the Beam
ingestion path uses. A paper already committed under the same ids gains the PDF as a source
revision. Then it asks the full-text worker for the paper, naming the main-text target, on the
bulk queue (`docs/design/evidence-fulltext.md` § Every trigger asks the same question): getting
text the open-access ladder could not reach is why a person deposits a PDF, so the deposit is the
decision to transcribe it. A paper that already has text is not transcribed.

Idempotent end to end, so a partially-completed deposit is safe to re-run: the crosswalk
claim is looked up before it is minted (a re-run adopts the existing `doc_id` rather than
double-claiming), the manifest write adds nothing it already holds (a content-addressed blob
re-put is a no-op), and the request is named for the generation it committed, so asking again
for the same commit is dropped. A crash between the mint and the write leaves an orphaned
crosswalk row, which the re-run reuses; a crash between the write and the request leaves the
paper to the sweep.

The request goes on the queue as `themis-clu`, impersonated from the caller's login as the
crosswalk connection is; infra grants it enqueue on the bulk queue and use of the conversion
invoker.

Every input is explicit and fails loud: the licence is the depositor's assertion (no
default), the metadata must name the paper (`sourced_metadata.check`, and a title), and a file that is not
a PDF, an id `identity` does not recognise, or a licence basis that is neither value each
raises `DepositError` rather than a stack trace from inside a library.

Run::

    uv run --group litcache python -m tools.litcache.deposit paper.pdf \\
        --id doi:10.1016/j.ajhg.2021.03.001 --id pmid:33811808 \\
        --licence 'https://creativecommons.org/licenses/by/4.0/' --licence-basis ASSERTED \\
        --title 'The title of the paper'
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import hashlib
import json
import pathlib
import sys
from collections.abc import Sequence

import pypdfium2
from google.api_core import exceptions as api_exceptions
from google.cloud import storage as gcs
from google.cloud import tasks_v2
from google.cloud.sql import connector
from google.protobuf import json_format, message

from themis.common import sql
from themis.litcache import (
    crosswalk,
    enqueue,
    identity,
    metadata_fields,
    pdf,
    sourced_metadata,
    upstream_record,
    work,
    writer,
)
from themis.litcache.models import litcache_pb2, work_pb2
from tools import clu

# The one source lineage a deposit writes: the held PDF, uploaded rather than fetched.
_PDF_HANDLE = 'pdf'

# Operator ids carry no seed key or docling origin; identity classifies them from the value alone.
_NO_ORIGIN = identity.DoclingOrigin(filename=None, binary_hash=None)

_LICENCE_BASES = {
    'ARTIFACT': litcache_pb2.LicenceBasis.LICENCE_BASIS_ARTIFACT,
    'ASSERTED': litcache_pb2.LicenceBasis.LICENCE_BASIS_ASSERTED,
}


class DepositError(Exception):
    """An operator input the deposit refuses, in words the operator can act on.

    Raised for every fail-loud path — a non-PDF file, an unrecognised or mis-scheme'd
    id, a bad licence basis, metadata that names no paper, a crosswalk claim that would
    bridge two works — so `main` turns it into a clean exit instead of a library
    traceback.
    """


@dataclasses.dataclass(frozen=True)
class DepositResult:
    """The outcome of a deposit.

    Attributes:
        doc_id: The `doc_id` the paper claims (minted fresh or adopted).
        minted: True if a fresh uuid4 was minted; False if an existing claim was reused.
        written: True if this call committed the paper; False if the paper was already
            committed, in which case the PDF joined it as a source revision.
        manifest: The committed manifest.
        asked: True if this call created the request for the paper's main text; False if a
            request for the same commit was already made.
    """

    doc_id: str
    minted: bool
    written: bool
    manifest: litcache_pb2.Manifest
    asked: bool


def _resolve_ids(raw_ids: Sequence[str]) -> identity.Identity:
    """Classify the operator's scheme-qualified ids into an `identity.Identity`.

    Each `{scheme}:{value}` is validated against `identity`'s own recognition — the value
    must classify, to the scheme the operator named, and survive classification unaltered —
    then the whole set is resolved into the identity (deduped, claim-key chosen by
    precedence) the crosswalk and manifest consume.

    Raises:
        DepositError: If `raw_ids` is empty, an id is not scheme-qualified, its value is not
            a recognised id, it classifies to a scheme other than the one named, or
            classification would alter the value (a suffix stripped, a `%xx` decoded).
    """
    if not raw_ids:
        raise DepositError('at least one --id is required (e.g. --id doi:10.1016/…)')
    values: list[str] = []
    for raw in raw_ids:
        scheme, sep, value = raw.partition(':')
        if not sep or not value:
            raise DepositError(f'id {raw!r} is not scheme-qualified — expected e.g. doi:10.1016/… or pmid:33811808')
        _classify_one(raw, scheme, value)  # validates; the value is re-resolved together below
        values.append(value)
    return identity.determine_identity(values[0], _NO_ORIGIN, extra_candidates=values[1:])


def _classify_one(raw: str, scheme: str, value: str) -> identity.ExternalId:
    """Classify one operator id, refusing an unrecognised, mis-scheme'd, or altered value.

    `identity.determine_identity` normalises a seed *key* — it strips a `.json`/`.pdf` suffix
    and URL-decodes — which would silently mint a truncated or decoded id under a different
    value than the operator supplied. So the classified value must equal the input (case
    aside, which the crosswalk folds); anything else is refused rather than mutated.

    Raises:
        DepositError: If the value is not a recognised id, classifies to a scheme other than
            `scheme`, or would be altered by classification.
    """
    try:
        ident = identity.determine_identity(value, _NO_ORIGIN)
    except ValueError as e:
        raise DepositError(f'id {raw!r} is not a recognised external id') from e
    (classified,) = ident.external_ids  # a lone recognised value yields exactly one id
    if classified.scheme != scheme:
        raise DepositError(f'id {raw!r} is recognised as {classified.scheme}, not {scheme}')
    if classified.value.lower() != value.lower():
        raise DepositError(
            f'id {raw!r} would be altered by normalisation ({value!r} → {classified.value!r}); supply the canonical id'
        )
    return classified


def _licence_basis(name: str) -> litcache_pb2.LicenceBasis:
    """Map the `--licence-basis` string to its enum, or fail loud.

    Raises:
        DepositError: If `name` is neither `ARTIFACT` nor `ASSERTED`.
    """
    basis = _LICENCE_BASES.get(name)
    if basis is None:
        raise DepositError(f'--licence-basis must be ARTIFACT or ASSERTED, not {name!r}')
    return basis


def _pdf_has_text_layer(pdf_bytes: bytes) -> bool:
    """Confirm the bytes are a loadable PDF and report whether it carries a text layer.

    Raises:
        DepositError: If the bytes are not a PDF, or not one pypdfium2 can load.
    """
    if not pdf_bytes.startswith(b'%PDF-'):
        raise DepositError('the supplied file is not a PDF (no %PDF- header)')
    try:
        return pdf.probe_has_text_layer(pdf_bytes)
    except pypdfium2.PdfiumError as e:
        raise DepositError('the supplied file is not a loadable PDF') from e


def _build_metadata(*, title: str | None, metadata_file: pathlib.Path | None) -> bytes:
    """Build the `metadata.pb` bytes from `--title` or `--metadata-file` (exactly one).

    A `--title` wraps into a source record holding a minimal Crossref record naming the paper; a
    `--metadata-file` carries a fuller `MetadataSource` (protobuf JSON `.json`, or serialized `.pb`).
    Either way the `Metadata` is filled from that source record (`metadata_fields.fill`), as every
    other writer fills it, and must state a title — metadata naming no paper is refused.

    Raises:
        DepositError: If neither or both inputs are given, the file cannot be parsed, or the
            source record holds no index record, cannot be filled, or fills no title.
    """
    if title is not None and metadata_file is None:
        source = litcache_pb2.MetadataSource(crossref_json=json.dumps({'title': [title]}))
    elif title is None and metadata_file is not None:
        source = _load_metadata_file(metadata_file)
    else:
        raise DepositError('supply exactly one of --title or --metadata-file')
    try:
        sourced_metadata.check_source(source)
        sourced = litcache_pb2.SourcedMetadata(metadata=metadata_fields.fill(source), source=source)
        data = sourced_metadata.to_canonical_bytes(sourced)
    except (ValueError, upstream_record.SchemaDriftError) as e:
        raise DepositError(f'metadata does not name the paper: {e}') from e
    if not sourced.metadata.title:
        raise DepositError('metadata does not name the paper: its record states no title')
    return data


def _load_metadata_file(path: pathlib.Path) -> litcache_pb2.MetadataSource:
    """Parse a `--metadata-file` into a `MetadataSource`; `.json` is protobuf JSON, `.pb` is serialized.

    Raises:
        DepositError: If the suffix is neither, or the contents do not parse as a
            `MetadataSource`.
    """
    data = path.read_bytes()
    suffix = path.suffix.lower()
    source = litcache_pb2.MetadataSource()
    if suffix == '.json':
        try:
            json_format.Parse(data.decode('utf-8'), source)
        except (json_format.ParseError, UnicodeDecodeError) as e:
            raise DepositError(f'--metadata-file {path} is not valid MetadataSource JSON') from e
        return source
    if suffix == '.pb':
        try:
            source.ParseFromString(data)
        except message.DecodeError as e:
            raise DepositError(f'--metadata-file {path} is not a valid MetadataSource') from e
        return source
    raise DepositError(f'--metadata-file must be .json or .pb, not {suffix!r}')


def _manifest_external_ids(ident: identity.Identity) -> litcache_pb2.ExternalIds:
    """Map the paper's ids to the manifest `ExternalIds`, failing loud on a scheme it can't record.

    `ExternalIds` has one field per modelled scheme; a claimed id whose scheme has no field
    would be minted in the crosswalk yet dropped from the manifest — the disagreement that
    makes the crosswalk un-rebuildable from manifests. Refuse it rather than drop it.

    Raises:
        DepositError: If an id's scheme is not a field of `ExternalIds`, or two ids share a scheme (two
            spellings of one id included): the field records one, and the crosswalk would claim both.
    """
    fields = set(litcache_pb2.ExternalIds.DESCRIPTOR.fields_by_name)
    present: dict[str, str] = {}
    for eid in ident.external_ids:
        if eid.scheme not in fields:
            raise DepositError(f'id scheme {eid.scheme!r} is claimable but the manifest cannot record it')
        if eid.scheme in present:
            raise DepositError(f'two {eid.scheme} ids, {present[eid.scheme]!r} and {eid.value!r}; give one')
        present[eid.scheme] = eid.value
    return litcache_pb2.ExternalIds(**present)


def _claim_doc_id(bucket: gcs.Bucket, conn: crosswalk.Connection, ident: identity.Identity) -> tuple[str, bool]:
    """Look up the paper's ids, reusing an existing `doc_id` or minting a fresh one.

    Lookup before mint keeps a re-run idempotent: when every id already maps to one `doc_id`,
    that claim is reused untouched. When some ids are new but the incumbent's manifest is not
    yet committed, the deposit is completing an interrupted run, so the missing ids are minted
    onto it. When some ids are new and the incumbent is already committed, minting them would
    leave the crosswalk holding ids the manifest never records — refused, not silently minted.

    Returns:
        The claimed `doc_id` and whether it was minted fresh (False when an existing claim was
        reused or adopted).

    Raises:
        DepositError: If the ids resolve to more than one `doc_id` (they bridge separate works,
            the deferred equivalence path), or add ids to an already-committed paper.
    """
    known = crosswalk.lookup(conn, ident.mint_keys)
    incumbents = set(known.values())
    if len(incumbents) > 1:
        raise DepositError(
            f'ids {list(ident.mint_keys)} map to multiple doc_ids {sorted(incumbents)}: '
            'they bridge separate works (the deferred cross-paper equivalence path), not one deposit'
        )
    if incumbents:
        (doc_id,) = incumbents
        if set(known) == set(ident.mint_keys):
            return doc_id, False
        if bucket.blob(writer.manifest_path(doc_id)).exists():
            new = sorted(set(ident.mint_keys) - set(known))
            raise DepositError(
                f'{doc_id} is already committed under ids {sorted(known)}; the deposit adds {new}. '
                'Extending the id set of a committed paper is the deferred cross-paper equivalence path, not a deposit'
            )
    result = crosswalk.mint(conn, ident.mint_keys)
    if result.linked_doc_ids:
        raise DepositError(
            f'ids {list(ident.mint_keys)} bridge separate works {list(result.linked_doc_ids)}: '
            'the deferred cross-paper equivalence path, not one deposit'
        )
    return result.doc_id, result.minted


def _require_ids_as_committed(bucket: gcs.Bucket, doc_id: str, manifest_ids: litcache_pb2.ExternalIds) -> None:
    """Refuse ids a committed paper records under another spelling.

    The crosswalk folds a DOI's case and a PMID's leading zeros, so the claim can match a committed paper whose
    manifest spells an id otherwise; the writer joins a committed paper only on its own spelling.

    Raises:
        DepositError: If `doc_id` is committed and its manifest records one of `manifest_ids` differently.
    """
    try:
        held = litcache_pb2.Manifest.FromString(bucket.blob(writer.manifest_path(doc_id)).download_as_bytes())
    except api_exceptions.NotFound:
        return
    differ = [
        f'{field.name}:{value} (committed as {getattr(held.external_ids, field.name)!r})'
        for field, value in manifest_ids.ListFields()
        if getattr(held.external_ids, field.name) != value
    ]
    if differ:
        raise DepositError(f'{doc_id} is committed under other spellings of its ids: {", ".join(differ)}; use those')


def deposit(
    bucket: gcs.Bucket,
    conn: crosswalk.Connection,
    *,
    pdf_bytes: bytes,
    external_ids: Sequence[str],
    licence: str,
    licence_basis: litcache_pb2.LicenceBasis,
    metadata: bytes,
    now: datetime.datetime,
    enqueuer: enqueue.Enqueuer,
) -> DepositResult:
    """Deposit one held PDF: claim a `doc_id`, commit it, and ask the worker for the paper's main text.

    Args:
        bucket: The corpus bucket to write the paper directory into.
        conn: A crosswalk (Postgres) connection; the caller owns its lifecycle.
        pdf_bytes: The held PDF's raw bytes.
        external_ids: The paper's scheme-qualified external ids (`doi:…`, `pmid:…`, …).
        licence: The depositor's asserted raw licence string.
        licence_basis: `LICENCE_BASIS_ARTIFACT` or `LICENCE_BASIS_ASSERTED`.
        metadata: The `metadata.pb` bytes (a `SourcedMetadata` naming the paper).
        now: Timezone-aware capture timestamp.
        enqueuer: The enqueuer on the bulk queue the request goes on.

    Returns:
        The `DepositResult` — the `doc_id`, whether it was minted, whether the paper was
        written, the committed manifest, and whether the request was made.

    Raises:
        DepositError: On any refused input (see the module helpers); a crosswalk claim
            bridging two works; or an already-committed paper whose id set the deposit
            would change.
            Also if the request cannot be created: the paper is then committed, and a re-run
            asks again, as does the sweep.
    """
    ident = _resolve_ids(external_ids)
    manifest_ids = _manifest_external_ids(ident)  # before the mint: nothing minted if a scheme is unrecordable
    has_text_layer = _pdf_has_text_layer(pdf_bytes)
    doc_id, minted = _claim_doc_id(bucket, conn, ident)
    _require_ids_as_committed(bucket, doc_id, manifest_ids)

    source = writer.SourceInput(
        handle=_PDF_HANDLE,
        media_type=litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF,
        kind=litcache_pb2.SourceKind.SOURCE_KIND_UPLOAD,
        data=pdf_bytes,
        licence=licence,
        licence_basis=licence_basis,
        access=litcache_pb2.Access(institution_captured=litcache_pb2.InstitutionCaptured()),
        captured_at=now,
        has_text_layer=has_text_layer,
    )
    paper = writer.PaperInput(
        doc_id=doc_id,
        external_ids=manifest_ids,
        claim_key=ident.claim_key,
        equivalence=litcache_pb2.Equivalence(edges=[], canonical_doc_id=doc_id),
        retraction=litcache_pb2.Retraction(),
        sources=[source],
        renderings=[],  # the worker transcribes the PDF once asked
        metadata=metadata,
    )
    result = writer.write_paper(bucket, paper)
    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
    if not any(rev.hash == pdf_hash for s in result.manifest.sources for rev in s.revisions):
        raise DepositError(f'{doc_id} is committed, and its manifest did not take the PDF')
    request = work_pb2.WorkRequest(doc_id=doc_id, targets=[work.MAIN_TEXT_TARGET])
    try:
        asked = enqueuer.enqueue(request, generation=result.generation)
    except api_exceptions.GoogleAPIError as e:
        raise DepositError(
            f'{doc_id} is deposited, but the worker could not be asked for it ({e}); '
            're-run the deposit to ask again, or the hourly sweep will'
        ) from e
    return DepositResult(doc_id=doc_id, minted=minted, written=result.written, manifest=result.manifest, asked=asked)


def _report(result: DepositResult, pdf_bytes: bytes, *, bucket_name: str) -> str:
    """The operator-facing summary: the `doc_id`, the paths written, and the next step."""
    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
    handle = next(s.handle for s in result.manifest.sources if any(r.hash == pdf_hash for r in s.revisions))
    paths = [
        writer.source_revision_path(result.doc_id, handle, pdf_hash, litcache_pb2.SourceFormat.SOURCE_FORMAT_PDF),
        writer.metadata_path(result.doc_id),
        writer.manifest_path(result.doc_id),
    ]
    claim = 'minted a fresh doc_id' if result.minted else 'reused an existing doc_id'
    wrote = 'wrote the paper directory' if result.written else 'paper already committed — the PDF joined it'
    asked = 'asked the worker for its main text' if result.asked else 'the worker was already asked for this commit'
    lines = [
        f'doc_id: {result.doc_id} ({claim})',
        wrote,
        'paths:',
        *(f'  gs://{bucket_name}/{path}' for path in paths),
        '',
        f'{asked}; the paper reads READY once its text is committed.',
    ]
    return '\n'.join(lines)


def _crosswalk_connection(project: str) -> tuple[connector.Connector, crosswalk.Connection]:
    """Open an IAM-authed crosswalk connection as `themis-clu`, impersonated from the caller's login.

    A personal identity has no database login, so the tool dials the Cloud SQL connector as
    the `themis-clu` service account (`tools.clu`), the account everything else reaches the
    instance as.
    """
    service_account = clu.service_account(project)
    pool = connector.Connector(credentials=clu.impersonated(service_account))
    try:
        conn = sql.iam_connect(
            pool,
            connection_name=f'{project}:{clu.REGION}:{clu.SQL_INSTANCE}',
            database=clu.SQL_DATABASE,
            db_user=clu.db_user(service_account),
        )
    except Exception:
        pool.close()  # the connector holds background refresh threads; don't leak them on a failed dial
        raise
    return pool, conn


def _bulk_enqueuer(project: str) -> enqueue.Enqueuer:
    """An enqueuer on `project`'s bulk queue into the full-text worker, as `themis-clu`.

    The names mirror the infra that creates them (`infra/themis_infra/convert.py`).
    """
    target = enqueue.ConversionTarget(
        queue_path=f'projects/{project}/locations/{clu.REGION}/queues/themis-convert-bulk',
        worker_url=clu.run_service_url('themis-convert-worker', project=project),
        invoker_service_account_email=f'themis-convert-invoker@{project}.iam.gserviceaccount.com',
    )
    client = tasks_v2.CloudTasksClient(credentials=clu.impersonated(clu.service_account(project)))
    return enqueue.Enqueuer(client, target)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('pdf', type=pathlib.Path, help='path to the held PDF to deposit')
    parser.add_argument(
        '--id',
        dest='ids',
        action='append',
        metavar='SCHEME:VALUE',
        help='a scheme-qualified external id (doi:…, pmid:…, pmcid:…); repeatable, at least one',
    )
    parser.add_argument('--licence', help="the paper's raw licence string (the depositor's assertion)")
    parser.add_argument(
        '--licence-basis', help='ARTIFACT (read from the bytes) or ASSERTED (supplied by the depositor)'
    )
    parser.add_argument('--title', help='the paper title, wrapped into a minimal Crossref metadata record')
    parser.add_argument(
        '--metadata-file',
        type=pathlib.Path,
        help=(
            'a fuller MetadataSource as protobuf JSON (.json) or serialized (.pb); use instead of --title. In JSON an '
            'index record is a string holding its JSON: {"crossrefJson": "{\\"title\\": [\\"T\\"], ...}"}'
        ),
    )
    parser.add_argument('--bucket', default=None, help='the corpus bucket (default: <project>-fulltext)')
    parser.add_argument(
        '--project',
        default=clu.DEFAULT_PROJECT,
        help='GCP project the crosswalk, corpus and worker live in (default: %(default)s)',
    )
    args = parser.parse_args(argv)
    if args.bucket is None:
        args.bucket = f'{args.project}-fulltext'
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Deposit the PDF named on the command line. Returns an exit code."""
    args = _parse_args(argv)
    try:
        if not args.ids:
            raise DepositError('at least one --id is required (e.g. --id doi:10.1016/…)')
        if args.licence is None or not args.licence.strip():
            raise DepositError('--licence is required and non-empty (a deposit carries the depositor-asserted licence)')
        if args.licence_basis is None:
            raise DepositError('--licence-basis is required (ARTIFACT or ASSERTED)')
        if not args.pdf.is_file():
            raise DepositError(f'no such PDF: {args.pdf}')
        licence_basis = _licence_basis(args.licence_basis)
        metadata = _build_metadata(title=args.title, metadata_file=args.metadata_file)
        pdf_bytes = args.pdf.read_bytes()

        now = datetime.datetime.now(tz=datetime.UTC)
        bucket = gcs.Client(project=args.project).bucket(args.bucket)
        enqueuer = _bulk_enqueuer(args.project)
        pool, conn = _crosswalk_connection(args.project)
        try:
            with contextlib.closing(conn):
                result = deposit(
                    bucket,
                    conn,
                    pdf_bytes=pdf_bytes,
                    external_ids=args.ids,
                    licence=args.licence,
                    licence_basis=licence_basis,
                    metadata=metadata,
                    now=now,
                    enqueuer=enqueuer,
                )
        finally:
            pool.close()
    except DepositError as e:
        print(f'deposit refused: {e}', file=sys.stderr)
        return 1

    print(_report(result, pdf_bytes, bucket_name=args.bucket))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
