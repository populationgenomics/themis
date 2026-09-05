"""The servicer over the real GCS backend, against fake-gcs-server.

`test_servicer.py` covers the protocol over the local backend; this is one publish, read and fetch
through the real backend, checking that a GCS generation — an opaque microsecond timestamp — is the
version token the wire carries. Skipped, through the repo-root `gcs_bucket` fixture, when no Docker
daemon is reachable.
"""

from __future__ import annotations

from google.cloud import storage
from google.protobuf import empty_pb2

from themis import sheaf
from themis.clients.auth.tests import fixture_session
from themis.rpc import sheaf_pb2, sheaf_pb2_grpc
from themis.services.sheaf.tests import conftest
from themis.sheaf import refdoc
from themis.sheaf.backends import gcs

_MIN_GCS_GENERATION = 1_000_000


def test_publish_read_and_fetch_over_gcs(gcs_bucket: storage.Bucket) -> None:
    backend = gcs.GcsBackend(gcs_bucket)
    intent = conftest.intent(0, {conftest.REF: (None, conftest.SHA_A)}, packs=[conftest.PACK_1])

    async def scenario(
        stub: sheaf_pb2_grpc.SheafAsyncStub,
    ) -> tuple[sheaf_pb2.PublishResponse, sheaf_pb2.RefDocSnapshot, bytes]:
        response = await conftest.publish(stub, conftest.stream(intent, [conftest.PACK_1]))
        snapshot = await stub.ReadRefDoc(empty_pb2.Empty(), metadata=fixture_session.GOOD_METADATA)
        return response, snapshot, await conftest.fetch(stub, sheaf.pack_id(conftest.PACK_1))

    response, snapshot, fetched = conftest.run(scenario, backend)

    assert response.generation > _MIN_GCS_GENERATION, 'a GCS generation is not a small dense integer'
    assert snapshot.generation == response.generation
    assert snapshot.document.refs[conftest.REF].oid == conftest.SHA_A
    assert refdoc.REFLOG_REF in snapshot.document.refs
    assert list(snapshot.document.packs) == [sheaf.pack_id(conftest.PACK_1)]
    assert fetched == conftest.PACK_1
    assert sheaf.Store(backend, fixture_session.ANALYSIS_ID).read().generation == response.generation
