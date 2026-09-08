"""The identity the repository's GitHub Actions runs write their Claude Code telemetry as.

The Claude Code workflows — the review bot, the doc garden, the merge follow-ups — export each run's token
and cost counters to Cloud Monitoring through the Telemetry API. They take this account by Workload Identity
Federation from the pool `bootstrap.sh` creates, whose provider admits only this repository's tokens; the
binding here is on the repository attribute, so any run of the repository, whatever its ref or event, may
take it. What a run gains is therefore what the account holds: writing points to any metric in the project —
a point under a new name creates the metric — and, through the consumer role the API requires for quota,
listing the project's time series and using its service quota; it reads nothing else. Unlike the deploy and
preview accounts, nothing about it is needed before Pulumi can run, so the program owns it.

- `CiTelemetryAccount` — the account, its writer grant, and the federation binding.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants

ACCOUNT_ID = 'themis-ci-telemetry'
# The repository whose runs may take the account; the GitHub OIDC provider in the pool admits only its tokens.
REPOSITORY = 'populationgenomics/themis-internal'
# The workload identity pool `bootstrap.sh` creates for GitHub Actions.
_POOL = 'github'


def _repository_principal_set(project_number: str) -> str:
    """The IAM member naming every GitHub Actions run of the repository, through the pool's provider.

    Args:
        project_number: The numeric id of the project holding the pool; a pool path takes the number,
            not the id.
    """
    return (
        f'principalSet://iam.googleapis.com/projects/{project_number}/locations/global/'
        f'workloadIdentityPools/{_POOL}/attribute.repository/{REPOSITORY}'
    )


class CiTelemetryAccount(pulumi.ComponentResource):
    """The metrics-writer identity the repository's GitHub Actions runs federate into.

    Attributes:
        service_account_email: The account's email — what the workflows' auth step names.
    """

    def __init__(
        self,
        *,
        project: str,
        project_number: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Create the account, grant it the writer roles, and bind the repository's runs to it.

        Args:
            project: The GCP project id: where the account lives and where its points are written.
            project_number: The project's numeric id, for the pool path in the federation member.
            opts: Parent/provider options.
        """
        super().__init__('themis:infra:CiTelemetryAccount', ACCOUNT_ID, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        service_account = gcp.serviceaccount.Account(
            ACCOUNT_ID,
            project=project,
            account_id=ACCOUNT_ID,
            display_name='Themis CI telemetry (GitHub Actions runs write Claude Code metrics as it)',
            opts=child,
        )
        self.service_account_email = service_account.email

        grants.TelemetryWriter(ACCOUNT_ID, member=service_account.member, project=project, opts=child)
        grants.FederatedImpersonator(
            'github-actions',
            member=_repository_principal_set(project_number),
            account=service_account.name,
            target=ACCOUNT_ID,
            opts=child,
        )
        self.register_outputs({'service_account_email': self.service_account_email})
