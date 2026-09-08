"""The workspace-spend monitor (`docs/design/cost-monitoring.md`): the exporter of Anthropic session cost.

The exporter is a Cloud Run Job on a five-minute schedule. Each execution lists every session in the Anthropic
workspace, totals cumulative usage by agent, and writes each agent's four running-total gauges (list cost, tokens
by type, active seconds, web searches) and one heartbeat — the series the freshness alert watches — through the
Telemetry API to Cloud Monitoring (`cost_metrics.py` names them). It reaches the Anthropic API by Workload Identity
Federation, so its GCP identity — no stored key — is what the Anthropic side authorizes.
The federation rule that authorizes it can only be registered against an identity that already exists (it pins
the account's numeric unique id), and registering it is an organization-admin action outside this program: the
identity therefore stands on its own, minted by a deploy, and the exporter runs as it.

- `CostExporter` — the exporter Job, its schedule and the identity that fires it, and its runtime SA (the GCP half
  of that federation, and the telemetry writer).
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants

# Every five minutes: spend appears with at most one tick of latency, and a full scan of the workspace is a
# handful of pages against the sessions API's per-minute ceiling.
_SCHEDULE = '*/5 * * * *'
# The scan's in-process deadline, well under the schedule so executions do not pile up; the Job's own timeout
# backstops it with room for a write that hangs past it.
_RUN_DEADLINE_SECONDS = 180
_JOB_TIMEOUT_MARGIN_SECONDS = 60


def _env(name: str, value: pulumi.Input[str]) -> gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs:
    return gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(name=name, value=value)


class CostExporter(pulumi.ComponentResource):
    """The workspace-spend exporter: its Job and schedule, and the identity it federates into Anthropic as.

    Attributes:
        service_account_email: The runtime SA's email — the `email` claim the exporter's Anthropic
            federation rule matches (`../../docs/runbooks/claude-api-wif.md` Path B).
        service_account_unique_id: The runtime SA's numeric unique id — the stable `sub` claim that rule
            pins; never reissued, so a recreated account with the same email does not match the rule.
        job_name: The Cloud Run Job's name, for running an export by hand.
    """

    def __init__(
        self,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        anthropic_federation_rule_id: str,
        anthropic_organization_id: str,
        anthropic_service_account_id: str,
        anthropic_workspace_id: str,
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

        # The exporter's GCP reach: writing its gauges' points. Reading the Anthropic side is the federation's.
        grants.TelemetryWriter('themis-cost-exporter', member=service_account.member, project=project, opts=child)

        job = gcp.cloudrunv2.Job(
            'themis-cost-exporter',
            project=project,
            location=region,
            name='themis-cost-exporter',
            deletion_protection=False,
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=service_account.email,
                    timeout=f'{_RUN_DEADLINE_SECONDS + _JOB_TIMEOUT_MARGIN_SECONDS}s',
                    # A failed run is not retried: the next tick carries the full total regardless, and a run that
                    # keeps failing is the freshness alert's to notice.
                    max_retries=0,
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            name='exporter',
                            image=image,
                            envs=[
                                # Keyless WIF (Path B), its own svac + rule (../../docs/runbooks/claude-api-wif.md).
                                _env('ANTHROPIC_FEDERATION_RULE_ID', anthropic_federation_rule_id),
                                _env('ANTHROPIC_ORGANIZATION_ID', anthropic_organization_id),
                                _env('ANTHROPIC_SERVICE_ACCOUNT_ID', anthropic_service_account_id),
                                _env('ANTHROPIC_WORKSPACE_ID', anthropic_workspace_id),
                                # Where the gauges are written, and the `location` label their series carry.
                                _env('THEMIS_COST_EXPORTER_PROJECT', project),
                                _env('THEMIS_COST_EXPORTER_LOCATION', region),
                                _env('THEMIS_COST_EXPORTER_DEADLINE_SECONDS', str(_RUN_DEADLINE_SECONDS)),
                            ],
                            # A few pages of session JSON and one export; nothing is held beyond the totals.
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                                limits={'cpu': '1', 'memory': '512Mi'}
                            ),
                        ),
                    ],
                ),
            ),
            opts=child,
        )
        self.job_name = job.name

        # Cloud Scheduler fires the Job against the Cloud Run Admin API as its own SA, invoker-only on this Job.
        scheduler_account = gcp.serviceaccount.Account(
            'themis-cost-exporter-scheduler',
            project=project,
            account_id='themis-cost-scheduler',
            display_name='Themis cost exporter scheduler',
            opts=child,
        )
        grants.JobRunner(
            'themis-cost-exporter-scheduler',
            member=scheduler_account.member,
            job=job.name,
            project=project,
            location=region,
            target='cost-exporter',
            opts=child,
        )
        gcp.cloudscheduler.Job(
            'themis-cost-exporter-schedule',
            project=project,
            region=region,
            name='themis-cost-exporter',
            schedule=_SCHEDULE,
            time_zone='Etc/UTC',
            http_target=gcp.cloudscheduler.JobHttpTargetArgs(
                # The v2 :run endpoint POST with a cloud-platform-scoped OAuth token (not OIDC — the target is a
                # Google API), the form Google documents (run/docs/execute/jobs-on-schedule).
                uri=pulumi.Output.format(
                    'https://run.googleapis.com/v2/projects/{0}/locations/{1}/jobs/{2}:run',
                    project,
                    region,
                    job.name,
                ),
                http_method='POST',
                oauth_token=gcp.cloudscheduler.JobHttpTargetOauthTokenArgs(
                    service_account_email=scheduler_account.email,
                    scope='https://www.googleapis.com/auth/cloud-platform',
                ),
            ),
            opts=child,
        )

        self.register_outputs(
            {
                'service_account_email': self.service_account_email,
                'service_account_unique_id': self.service_account_unique_id,
                'job_name': self.job_name,
            }
        )
