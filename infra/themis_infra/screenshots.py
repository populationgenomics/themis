"""The PR review screenshot bucket (`docs/design/pr-screenshots.md`).

Public-read-by-URL, non-listable GCS storage for the before/after images a
rendered-surface PR ships with. GitHub renders an external markdown image through
its Camo proxy, which fetches the origin server-side and anonymously, so the
objects have to be readable with no credentials. `tools/screenshot/upload.py`
writes them under their own sha256; nothing at runtime reads this bucket.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Exactly `storage.objects.get`. `roles/storage.objectViewer` would also carry
# `storage.objects.list`, which makes a bucket publicly enumerable.
_PUBLIC_READ_ROLE = 'roles/storage.legacyObjectReader'


def pr_screenshot_bucket(
    *,
    project: str,
    region: str,
    team_group: str,
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.storage.Bucket:
    """Create the PR-screenshot bucket with its public-read and team grants.

    Objects are named `<sha256>.png` and served at
    `https://storage.googleapis.com/<bucket>/<name>`. The writer is a developer's own
    `gcloud` ADC — a member of `team_group` — so no service account is minted here.

    Args:
        project: The GCP project to create the bucket in.
        region: The bucket's location.
        team_group: The Google group that uploads, browses, and retracts.
        opts: Resource options (parent/dependency wiring).

    Returns:
        The `Bucket`, for export.
    """
    bucket = gcp.storage.Bucket(
        'themis-pr-screenshots',
        project=project,
        name=f'{project}-pr-screenshots',
        location=region,
        uniform_bucket_level_access=True,
        public_access_prevention='inherited',
        # Off, so a takedown is immediate: the GCS-default 7-day window can't be
        # overridden, and a deleted object stays retrievable for its duration.
        soft_delete_policy=gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=0),
        autoclass=gcp.storage.BucketAutoclassArgs(enabled=True, terminal_storage_class='ARCHIVE'),
        opts=opts,
    )
    gcp.storage.BucketIAMMember(
        'themis-pr-screenshots-public-read',
        bucket=bucket.name,
        role=_PUBLIC_READ_ROLE,
        member='allUsers',
        opts=opts,
    )
    # objectAdmin, not objectCreator: whoever can publish to the project's one public
    # bucket must be able to retract from it without escalating to a project owner.
    gcp.storage.BucketIAMMember(
        'themis-pr-screenshots-team-admin',
        bucket=bucket.name,
        role='roles/storage.objectAdmin',
        member=f'group:{team_group}',
        opts=opts,
    )
    return bucket
