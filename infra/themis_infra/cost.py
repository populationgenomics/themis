"""The workspace-spend monitor (`docs/design/cost-monitoring.md`): the exporter of Anthropic session cost.

The exporter reaches the Anthropic API by Workload Identity Federation, so its GCP identity — no stored key —
is what the Anthropic side authorizes. The federation rule that authorizes it can only be registered against
an identity that already exists (it pins the account's numeric unique id), and registering it is an
organization-admin action outside this program: the identity therefore stands on its own, minted by a deploy,
and the exporter runs as it.

- `CostExporter` — the exporter and its runtime SA (the GCP half of that federation).
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp


class CostExporter(pulumi.ComponentResource):
    """The workspace-spend exporter and the runtime identity it federates into Anthropic as.

    Attributes:
        service_account_email: The runtime SA's email — the `email` claim the exporter's Anthropic
            federation rule matches (`../../docs/runbooks/claude-api-wif.md` Path B).
        service_account_unique_id: The runtime SA's numeric unique id — the stable `sub` claim that rule
            pins; never reissued, so a recreated account with the same email does not match the rule.
    """

    def __init__(
        self,
        *,
        project: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:CostExporter', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        # The Anthropic federation rule pins this SA's email/unique_id; the unique_id is never reissued,
        # so protect + retain_on_delete refuse a delete/replace that would strand the pinned rule.
        # account_id/project (the replace triggers) don't change on a normal `up`.
        service_account = gcp.serviceaccount.Account(
            'themis-cost-exporter-runtime',
            project=project,
            account_id='themis-cost-exporter',
            display_name='Themis cost exporter runtime (workspace-spend monitor)',
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(protect=True, retain_on_delete=True)),
        )
        self.service_account_email = service_account.email
        self.service_account_unique_id = service_account.unique_id
        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
            }
        )
