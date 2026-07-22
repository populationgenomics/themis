"""Project-level IAM for the CI deploy service account, managed in-program.

`bootstrap.sh` grants the deploy SA only the IAM *root* it needs before Pulumi
can run: `projectIamAdmin` (so the program can set project IAM), `storage.admin`
(so the SA can read/write its own Pulumi state), and KMS (the secrets provider,
loaded on every op). Every other project role the SA needs to build the
program's resources is declared here — versioned, drift-checked IaC instead of
imperative bootstrap.

Safe despite the SA granting "itself" these roles: a fresh environment's first
`pulumi up` is operator-run (Owner, per the fresh-environment runbook), so the
operator creates these bindings; thereafter CI already holds them and merely
re-asserts them (an existing binding is idempotent), so there is no intra-run
chicken-and-egg. `projectIamAdmin` and `storage.admin` deliberately stay in
bootstrap — moving them would risk locking the SA out of the very IAM/state it
needs to recover.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Roles the deploy SA needs to create/manage the program's resources.
_DEPLOY_ROLES: tuple[str, ...] = (
    'roles/artifactregistry.admin',
    'roles/cloudsql.admin',
    'roles/compute.admin',
    'roles/dns.admin',
    'roles/iam.serviceAccountAdmin',
    'roles/iam.serviceAccountUser',
    'roles/iap.admin',
    'roles/run.admin',
    'roles/secretmanager.admin',
    'roles/serviceusage.serviceUsageAdmin',
)


def deploy_sa_email(project: str) -> str:
    """The CI deploy SA's deterministic email (created by `bootstrap.sh`).

    Args:
        project: The GCP project; fixes the SA's email.

    Returns:
        `themis-deploy@<project>.iam.gserviceaccount.com`.
    """
    return f'themis-deploy@{project}.iam.gserviceaccount.com'


def grant_deploy_roles(
    *,
    project: str,
    opts: pulumi.ResourceOptions | None = None,
) -> None:
    """Grant the CI deploy SA its project roles (see module docstring).

    Args:
        project: The GCP project; also fixes the deploy SA's deterministic email.
        opts: Resource options (dependency wiring).
    """
    member = f'serviceAccount:{deploy_sa_email(project)}'
    for role in _DEPLOY_ROLES:
        slug = role.removeprefix('roles/').replace('.', '-')
        gcp.projects.IAMMember(
            f'themis-deploy-{slug}',
            project=project,
            role=role,
            member=member,
            opts=opts,
        )
