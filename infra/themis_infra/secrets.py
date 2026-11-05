"""Secret Manager secrets: credentials the stack writes.

A credential with no keyless alternative arrives as `config.require_secret`, so the plaintext never enters the
repo, and leaves as one secret whose latest version a Cloud Run container reads as env (`stack_secret`). Who may
read one is a `grants.SecretReader` where its consumer is wired. See `infra/README.md` (State and secrets).
"""

from __future__ import annotations

from typing import NamedTuple

import pulumi
import pulumi_gcp as gcp


class StackSecret(NamedTuple):
    """A secret and its one version.

    Cloud Run checks at revision creation that `latest` exists and that the runtime account may read it,
    so a workload reading the secret depends on `version` and on its `grants.SecretReader`, not on `secret`
    alone.
    """

    secret: gcp.secretmanager.Secret
    version: gcp.secretmanager.SecretVersion


def stack_secret(
    secret_id: str,
    *,
    project: str,
    region: str,
    value: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> StackSecret:
    """Provision one Secret Manager secret whose only version is `value`.

    The `secret_id` is unprefixed and the same in every environment: secret ids are project-scoped and each
    environment is its own project, so a reader resolves the secret from a fixed name in its own project.
    Replication is pinned to `region`, in-region with the rest of the stack. Rotating is updating the encrypted
    config value, which replaces the version; consumers read `latest`.

    Args:
        secret_id: The secret's name (`anthropic-environment-key`); the Pulumi resource is `themis-<secret_id>`.
        project: The GCP project to create the secret in.
        region: The single region the secret replicates to.
        value: The secret value, from `config.require_secret` (kept secret).
        opts: Resource options (parent/dependency wiring).

    Returns:
        The secret, and the version that carries `value`.
    """
    secret = gcp.secretmanager.Secret(
        f'themis-{secret_id}',
        project=project,
        secret_id=secret_id,
        replication=gcp.secretmanager.SecretReplicationArgs(
            user_managed=gcp.secretmanager.SecretReplicationUserManagedArgs(
                replicas=[gcp.secretmanager.SecretReplicationUserManagedReplicaArgs(location=region)],
            ),
        ),
        opts=opts,
    )
    version = gcp.secretmanager.SecretVersion(
        f'themis-{secret_id}-current',
        secret=secret.id,
        secret_data=value,
        opts=pulumi.ResourceOptions(parent=secret),
    )
    return StackSecret(secret, version)


def secret_env(name: str, secret_id: pulumi.Input[str]) -> gcp.cloudrunv2.ServiceTemplateContainerEnvArgs:
    """A Cloud Run container env var `name` carrying the latest version of the secret `secret_id`.

    The runtime account reads it at instance start, so it needs a `grants.SecretReader` on the secret.
    """
    return gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
        name=name,
        value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
            secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(
                secret=secret_id, version='latest'
            ),
        ),
    )
