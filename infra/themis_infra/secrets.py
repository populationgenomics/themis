"""Ingestion-side API-key secrets in Secret Manager.

Third-party ingestion credentials that have no keyless/WIF alternative (unlike
the Google-side auth the backend and CI use) live as Secret Manager secrets,
their values sourced from encrypted Pulumi config (`config.require_secret`) so
the plaintext never enters the repo. See `infra/README.md` (State and secrets).

No accessor grant is attached here: the runtime consumer (the litcache
ingestion pipeline's id resolver) is not deployed yet. The `secretAccessor`
grant to its service account lands with that service.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


def semantic_scholar_secret(
    name: str,
    *,
    project: str,
    region: str,
    api_key: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> gcp.secretmanager.Secret:
    """Provision the Semantic Scholar API key as a Secret Manager secret.

    The litcache OA id resolver falls back to Semantic Scholar; the keyless
    endpoint is rate-limited, so a bulk (Dataflow) run needs the authenticated
    key. Replication is pinned to `region` (not automatic/multi-region) to keep
    the secret in-region with the rest of the stack.

    Args:
        name: Resource-name prefix (the environment's stack name).
        project: The GCP project to create the secret in.
        region: The single region the secret replicates to.
        api_key: The key value, from `config.require_secret` (kept secret).
        opts: Resource options (parent/dependency wiring).

    Returns:
        The `Secret`; its latest version carries `api_key`.
    """
    secret = gcp.secretmanager.Secret(
        f'{name}-semantic-scholar-api-key',
        project=project,
        # Deliberately unprefixed: secret_id is project-scoped, and each env is its
        # own project, so there is no cross-stack collision to disambiguate. Keeping
        # it stable and env-agnostic lets the runtime reader resolve it from a fixed
        # name + its own project — it need not know the Pulumi `{name}` prefix.
        secret_id='semantic-scholar-api-key',  # noqa: S106 — the secret's name, not its value
        replication=gcp.secretmanager.SecretReplicationArgs(
            user_managed=gcp.secretmanager.SecretReplicationUserManagedArgs(
                replicas=[gcp.secretmanager.SecretReplicationUserManagedReplicaArgs(location=region)],
            ),
        ),
        opts=opts,
    )
    # One config-sourced version; rotate by updating the encrypted config value,
    # which replaces this version (consumers read `latest`). Additive versioning
    # with an overlap window isn't needed for a read-latest API key.
    gcp.secretmanager.SecretVersion(
        f'{name}-semantic-scholar-api-key-current',
        secret=secret.id,
        secret_data=api_key,
        opts=pulumi.ResourceOptions(parent=secret),
    )
    return secret
