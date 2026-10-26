"""Third-party API-key secrets in Secret Manager.

Third-party credentials that have no keyless/WIF alternative (unlike the Google-side auth the backend and CI use)
live as Secret Manager secrets, their values sourced from encrypted Pulumi config (`config.require_secret`) so the
plaintext never enters the repo. See `infra/README.md` (State and secrets).

No accessor grant is attached here: each runtime that reads a key is granted `secretAccessor` on it by its own
component (`grants.SecretReader`), beside the identity it runs as.
"""

from __future__ import annotations

import dataclasses

import pulumi
import pulumi_gcp as gcp


@dataclasses.dataclass(frozen=True)
class ApiKeySecret:
    """A provisioned key: the secret, and the version holding its value.

    Attributes:
        secret: The secret; a reader is granted on its `secret_id`.
        version: Its one config-sourced version. A runtime that resolves `latest` at deploy time (a Cloud Run env
            var) depends on it, so the deploy never finds the secret empty.
    """

    secret: gcp.secretmanager.Secret
    version: gcp.secretmanager.SecretVersion


def semantic_scholar_secret(
    *,
    project: str,
    region: str,
    api_key: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> ApiKeySecret:
    """Provision the Semantic Scholar API key as a Secret Manager secret.

    The litcache OA id resolver falls back to Semantic Scholar; the keyless endpoint is rate-limited, so a bulk
    (Dataflow) run needs the authenticated key.

    Args:
        project: The GCP project to create the secret in.
        region: The single region the secret replicates to.
        api_key: The key value, from `config.require_secret` (kept secret).
        opts: Resource options (parent/dependency wiring).

    Returns:
        The secret and the version carrying `api_key`.
    """
    return _api_key_secret(
        'themis-semantic-scholar-api-key',
        secret_id='semantic-scholar-api-key',  # noqa: S106 — the secret's name, not its value
        project=project,
        region=region,
        api_key=api_key,
        opts=opts,
    )


def openalex_secret(
    *,
    project: str,
    region: str,
    api_key: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None = None,
) -> ApiKeySecret:
    """Provision the OpenAlex API key as a Secret Manager secret.

    OpenAlex meters keyless callers, so every litcache lookup of a work sends the key: the evidence service's on-demand
    ingest and the Dataflow ingest workers. Its name is `themis.litcache.openalex_key.SECRET_ID`.

    Args:
        project: The GCP project to create the secret in.
        region: The single region the secret replicates to.
        api_key: The key value, from `config.require_secret` (kept secret).
        opts: Resource options (parent/dependency wiring).

    Returns:
        The secret and the version carrying `api_key`.
    """
    return _api_key_secret(
        'themis-openalex-api-key',
        secret_id='openalex-api-key',  # noqa: S106 — the secret's name, not its value
        project=project,
        region=region,
        api_key=api_key,
        opts=opts,
    )


def _api_key_secret(
    resource_name: str,
    *,
    secret_id: str,
    project: str,
    region: str,
    api_key: pulumi.Input[str],
    opts: pulumi.ResourceOptions | None,
) -> ApiKeySecret:
    """One API key's secret, replicated in `region` alone to keep it in-region with the rest of the stack."""
    secret = gcp.secretmanager.Secret(
        resource_name,
        project=project,
        # Deliberately unprefixed: secret_id is project-scoped, and each env is its
        # own project, so there is no cross-stack collision to disambiguate. Keeping
        # it stable and env-agnostic lets the runtime reader resolve it from a fixed
        # name + its own project — it need not know the Pulumi `themis-` prefix.
        secret_id=secret_id,
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
    version = gcp.secretmanager.SecretVersion(
        f'{resource_name}-current',
        secret=secret.id,
        secret_data=api_key,
        opts=pulumi.ResourceOptions(parent=secret),
    )
    return ApiKeySecret(secret=secret, version=version)
