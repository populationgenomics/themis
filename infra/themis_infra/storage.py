"""The literature full-text store bucket.

The durable source of truth for the literature-evidence layer — one GCS
directory per paper. See `docs/design/literature-evidence-layer.md` §2.1 for the
storage model and `infra/README.md` (Storage) for the bucket policy and naming.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

_NONCURRENT_RETENTION_DAYS = 30


def fulltext_bucket(
    name: str,
    *,
    project: str,
    region: str,
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.storage.Bucket:
    """Create the full-text store bucket, returned for export and IAM grants."""
    return gcp.storage.Bucket(
        f'{name}-fulltext',
        project=project,
        name=f'{project}-fulltext',
        location=region,
        uniform_bucket_level_access=True,
        public_access_prevention='enforced',
        versioning=gcp.storage.BucketVersioningArgs(enabled=True),
        lifecycle_rules=[
            gcp.storage.BucketLifecycleRuleArgs(
                action=gcp.storage.BucketLifecycleRuleActionArgs(type='Delete'),
                condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                    days_since_noncurrent_time=_NONCURRENT_RETENTION_DAYS,
                ),
            )
        ],
        # Delete-only lifecycle keeps Autoclass valid; storage-class transitions don't.
        autoclass=gcp.storage.BucketAutoclassArgs(enabled=True, terminal_storage_class='ARCHIVE'),
        opts=opts,
    )
