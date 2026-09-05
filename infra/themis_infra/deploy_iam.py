"""The CI deploy service account's identity.

`bootstrap.sh` creates the account; its project roles are `grants.DeployAccountBuilder`.
"""

from __future__ import annotations


def deploy_sa_email(project: str) -> str:
    """The CI deploy SA's deterministic email (created by `bootstrap.sh`).

    Args:
        project: The GCP project; fixes the SA's email.

    Returns:
        `themis-deploy@<project>.iam.gserviceaccount.com`.
    """
    return f'themis-deploy@{project}.iam.gserviceaccount.com'
