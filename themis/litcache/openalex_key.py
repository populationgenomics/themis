"""OpenAlex's API key as the stack provisions it: a project's `openalex-api-key` Secret Manager secret.

Read here by the processes no deploy hands an environment: the Dataflow ingest workers, each in its own `setup`, and
the laptop tools, under the caller's ADC. The evidence service gets the same secret from Cloud Run, which mounts it as
an environment variable (`themis.services.evidence.literature.config`).
"""

from __future__ import annotations

from google.cloud import secretmanager

from themis.litcache import openalex

# The secret's name in its project, as `infra/themis_infra/secrets.py` provisions it.
SECRET_ID = 'openalex-api-key'  # noqa: S105 — the secret's name, not its value


def from_secret_manager(project: str) -> openalex.ApiKey:
    """Read the latest version of `project`'s OpenAlex API key.

    Args:
        project: The GCP project holding the secret.

    Returns:
        The key.

    Raises:
        google.api_core.exceptions.GoogleAPICallError: If the secret cannot be read: there is none, or the caller
            holds no `secretAccessor` on it.
        ValueError: If the stored value is not a key (`openalex.ApiKey`).
    """
    with secretmanager.SecretManagerServiceClient() as client:
        response = client.access_secret_version(name=client.secret_version_path(project, SECRET_ID, 'latest'))
    return openalex.ApiKey(response.payload.data.decode('utf-8'))
