"""Properties every GCS bucket the program declares holds, whichever module declares it."""

from __future__ import annotations

from themis_infra import capture

_BUCKET_TYPE = 'gcp:storage/bucket:Bucket'


def _buckets(program: capture.Capture) -> dict[str, dict[str, object]]:
    """Each declared bucket's registered state, by URN."""
    return {
        urn: dict(registration.state)
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _BUCKET_TYPE
    }


def test_every_bucket_enforces_uniform_access(program: capture.Capture) -> None:
    # Grants model bucket access by IAM alone; uniform access makes object ACLs inert, so nothing bypasses them.
    buckets = _buckets(program)
    assert buckets, 'the program declared no bucket; the mocks are not running it'
    acl_governed = sorted(urn for urn, state in buckets.items() if state.get('uniformBucketLevelAccess') is not True)
    assert not acl_governed, 'buckets without uniform bucket-level access:\n' + '\n'.join(acl_governed)


def test_every_bucket_name_carries_the_project(program: capture.Capture) -> None:
    # Bucket names are global: one without the project prefix collides with another environment's.
    buckets = _buckets(program)
    assert buckets, 'the program declared no bucket; the mocks are not running it'
    unprefixed = sorted(
        f'{urn}: {state["name"]}'
        for urn, state in buckets.items()
        if not str(state['name']).startswith(f'{program.project}-')
    )
    assert not unprefixed, 'bucket names without the project prefix:\n' + '\n'.join(unprefixed)
