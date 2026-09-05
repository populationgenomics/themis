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
re-asserts them (an existing binding is idempotent). A role added to a live
environment in the same run as the first resource that needs it is the one
intra-run case: `grant_deploy_roles` returns the bindings so that resource can
`depends_on` its role rather than race IAM propagation. `projectIamAdmin` and
`storage.admin` deliberately stay in bootstrap — moving them would risk locking
the SA out of the very IAM/state it needs to recover.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# Roles the deploy SA needs to create/manage the program's resources.
_DEPLOY_ROLES: tuple[str, ...] = (
    'roles/artifactregistry.admin',
    # A CryptoKeyIAMMember reads and sets a key's policy; no predefined role carries just that. This one
    # also lets the SA disable or destroy any key version in the project and grant itself sign or decrypt —
    # nothing `projectIamAdmin` below did not already allow in one more step, but one step fewer.
    'roles/cloudkms.admin',
    'roles/cloudscheduler.admin',
    'roles/cloudsql.admin',
    'roles/cloudtasks.queueAdmin',
    'roles/compute.admin',
    'roles/iam.serviceAccountAdmin',
    'roles/iam.serviceAccountUser',
    'roles/iap.admin',
    'roles/logging.configWriter',  # retention on the _Default log bucket (baseline.py) is a buckets.create/update
    'roles/monitoring.editor',  # the workspace-spend monitor's Monitoring resources (cost.py)
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
) -> dict[str, gcp.projects.IAMMember]:
    """Grant the CI deploy SA its project roles (see module docstring).

    Args:
        project: The GCP project; also fixes the deploy SA's deterministic email.
        opts: Resource options (dependency wiring).

    Returns:
        The bindings by role, so a resource whose creation needs one of them can `depends_on` it: a
        role added in the same run as the first resource needing it is otherwise raced by IAM
        propagation.
    """
    member = f'serviceAccount:{deploy_sa_email(project)}'
    bindings = {}
    for role in _DEPLOY_ROLES:
        slug = role.removeprefix('roles/').replace('.', '-')
        bindings[role] = gcp.projects.IAMMember(
            f'themis-deploy-{slug}',
            project=project,
            role=role,
            member=member,
            opts=opts,
        )
    return bindings
