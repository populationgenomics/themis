"""The literature full-text store bucket.

The durable source of truth for the literature-evidence layer — one GCS
directory per paper. See `docs/design/literature-evidence-layer.md` §2.1 for the
storage model and `infra/README.md` (Storage) for the bucket policy and naming.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Days a noncurrent version is kept before the lifecycle rule below GCs it.
_NONCURRENT_RETENTION_DAYS = 30


def fulltext_bucket(
    *,
    project: str,
    region: str,
    cors_origins: list[str],
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.storage.Bucket:
    """Create the full-text store bucket, returned for export and IAM grants.

    `cors_origins` are the web origins allowed to read objects cross-origin: the BFF 302s
    paper-content reads to signed URLs on this bucket, so the browser fetches the bytes directly,
    and pdf.js / fetch read the body cross-origin.

    Raises:
        ValueError: If `cors_origins` is empty — the bucket's only reader is the browser, so a
            missing origin list would leave every paper-content read blocked by CORS.
    """
    if not cors_origins:
        raise ValueError('fulltext_bucket requires at least one CORS origin')
    return gcp.storage.Bucket(
        'themis-fulltext',
        project=project,
        name=f'{project}-fulltext',
        location=region,
        uniform_bucket_level_access=True,
        public_access_prevention='enforced',
        # GET/HEAD reads only, with the Range headers pdf.js needs to page a large PDF.
        cors=[
            gcp.storage.BucketCorArgs(
                origins=cors_origins,
                methods=['GET', 'HEAD'],
                response_headers=[
                    'Content-Type',
                    'Content-Range',
                    'Content-Length',
                    'Accept-Ranges',
                    'Range',
                ],
                max_age_seconds=3600,
            )
        ],
        # Recovery is object versioning, not soft delete: soft delete's window
        # can't be overridden, which would block a deliberate reclaim.
        versioning=gcp.storage.BucketVersioningArgs(enabled=True),
        lifecycle_rules=[
            gcp.storage.BucketLifecycleRuleArgs(
                action=gcp.storage.BucketLifecycleRuleActionArgs(type='Delete'),
                condition=gcp.storage.BucketLifecycleRuleConditionArgs(
                    days_since_noncurrent_time=_NONCURRENT_RETENTION_DAYS,
                ),
            )
        ],
        # Disable the GCS-default 7-day soft delete so it can't shadow versioning.
        soft_delete_policy=gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=0),
        # Delete-only lifecycle keeps Autoclass valid; storage-class transitions don't.
        autoclass=gcp.storage.BucketAutoclassArgs(enabled=True, terminal_storage_class='ARCHIVE'),
        opts=opts,
    )
