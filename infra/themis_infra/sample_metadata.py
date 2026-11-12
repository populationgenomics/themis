"""The sample metadata service's runtime service account, which the site grants read on its sample registry.

The module declares the account and no workload that runs as it.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


def runtime_account(*, project: str, opts: pulumi.ResourceOptions | None = None) -> gcp.serviceaccount.Account:
    """The sample metadata service's runtime service account, `themis-sample-metadata`.

    Top-level, so a fresh environment's targeted `up` creates it before the site grants it read on its sample registry.
    """
    return gcp.serviceaccount.Account(
        'themis-sample-metadata-runtime',
        project=project,
        account_id='themis-sample-metadata',
        display_name='Themis sample metadata service runtime',
        opts=opts,
    )
