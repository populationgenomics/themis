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
    backend = gcs.GcsBackend(gcs_bucket, signer=None)
    intent = conftest.intent(0, {conftest.REF: (None, conftest.SHA_A)}, packs=[conftest.PACK_1])

    def scenario(
        stub: sheaf_pb2_grpc.SheafStub,
    ) -> tuple[sheaf_pb2.PublishResponse, sheaf_pb2.RefDocSnapshot, bytes]:
        response = conftest.publish(stub, conftest.stream(intent, [conftest.PACK_1]))
        snapshot = stub.ReadRefDoc(empty_pb2.Empty(), metadata=conftest.WORKER)
        return response, snapshot, conftest.fetch(stub, sheaf.pack_id(conftest.PACK_1))

    response, snapshot, fetched = conftest.run(scenario, backend)

    assert response.generation > _MIN_GCS_GENERATION, 'a GCS generation is not a small dense integer'
    assert snapshot.generation == response.generation
    assert snapshot.document.refs[conftest.REF].oid == conftest.SHA_A
    assert refdoc.REFLOG_REF in snapshot.document.refs
    assert list(snapshot.document.packs) == [sheaf.pack_id(conftest.PACK_1)]
    assert fetched == conftest.PACK_1
    assert sheaf.Store(backend, fixture_session.ANALYSIS_ID).read().generation == response.generation


def test_sign_pack_urls_sizes_each_pack_from_the_bucket(gcs_bucket: storage.Bucket) -> None:
    """The size comes from a listing under the pack's exact key, which the emulator answers as GCS does."""
    backend = gcs.GcsBackend(gcs_bucket, signer=None)
    small, large = b'small pack', b'large pack ' * 50
    conftest.seed(backend, {conftest.REF: (None, conftest.SHA_A)}, packs=[small, large])
    request = sheaf_pb2.SignPackUrlsRequest(pack_ids=[sheaf.pack_id(large), sheaf.pack_id(small)])

    response = conftest.run(
        lambda stub: stub.SignPackUrls(request, metadata=conftest.WEB_WITH_SESSION), conftest.Signing(backend)
    )

    assert [(pack.pack_id, pack.size) for pack in response.packs] == [
        (sheaf.pack_id(large), len(large)),
        (sheaf.pack_id(small), len(small)),
    ]
