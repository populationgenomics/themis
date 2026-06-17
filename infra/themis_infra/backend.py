"""The orchestrator backend: the Managed-Agents control-plane client.

The backend (future `apps/api`) runs the Themis orchestrator — it mediates
access to private data and is the client of Anthropic's Managed Agents. It is
not built yet; this component forward-provisions only its **runtime service
account**, so that account's identity exists ahead of the service. That lets the
Anthropic-side WIF federation rule (see
[`docs/runbooks/claude-api-wif.md`](../../docs/runbooks/claude-api-wif.md), Path
B) pin the SA's `unique_id`/`email`, and CI/Console setup can proceed before the
service lands.

It grows into the full backend (Cloud Run service, Cloud SQL/GCS/Secret Manager
access, the Anthropic federation env wiring) by adding resources here and
attaching them to this same SA — symmetric with `web`. Kept a separate identity
from the web frontend by least privilege: only the backend holds Anthropic and
private-data access.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


class OrchestratorBackend(pulumi.ComponentResource):
    """The orchestrator backend's runtime identity (service to follow).

    Attributes:
        service_account_email: The runtime SA's email — the `email` claim the
            Anthropic federation rule matches.
        service_account_unique_id: The runtime SA's numeric unique ID — the
            stable `sub` claim the Anthropic federation rule pins (never reused,
            so it survives a delete/recreate of the same email).
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:OrchestratorBackend', name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        # No project roles yet — a forward-provisioned identity only. Data-access
        # and Secret Manager grants attach here as the backend service lands.
        service_account = gcp.serviceaccount.Account(
            f'{name}-runtime',
            project=project,
            account_id=f'{name}-backend',
            display_name='Themis orchestrator backend (Managed Agents client)',
            opts=child,
        )

        self.service_account_email = service_account.email
        self.service_account_unique_id = service_account.unique_id
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
            }
        )
