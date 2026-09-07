"""The workspace-spend monitor (`docs/design/cost-monitoring.md`): the exporter of Anthropic session cost.

The exporter is a Cloud Run Job on a five-minute schedule. Each execution lists every session in the Anthropic
workspace, totals cumulative list cost by agent, and writes one gauge point per agent to Cloud Monitoring, the
series everything downstream reads. It reaches the Anthropic API by Workload Identity Federation, so its
GCP identity — no stored key — is what the Anthropic side authorizes. The federation rule that authorizes it can
only be registered against an identity that already exists (it pins the account's numeric unique id), and
registering it is an organization-admin action outside this program: the identity therefore stands on its own,
minted by a deploy, and the exporter runs as it.

- `CostExporter` — the exporter Job, its schedule and the identity that fires it, its runtime SA (the GCP half of
  that federation), and the gauge's metric descriptor.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants

# The gauge the exporter writes. `themis/services/cost_exporter/gauge.py` names the same metric and label from the
# writing side; a test holds the two together.
_METRIC_TYPE = 'custom.googleapis.com/themis/anthropic/session_list_cost_cents'
_AGENT_LABEL = 'agent'

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
    """The workspace-spend exporter: its Job and schedule, the identity it federates into Anthropic as, its gauge.

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

        # The one GCP grant the exporter holds: writing points. Reading the Anthropic side is the federation's.
        grants.MetricWriter('themis-cost-exporter', member=service_account.member, project=project, opts=child)

        # Declared rather than minted by the first write: a write fixes only the type and its points' value type,
        # so the unit, the description and the label's meaning have nowhere else to live. Type, kind, value type and
        # labels are immutable — a change plans a replacement — and a deleted descriptor takes its history with it,
        # which Monitoring cannot backfill: protect, and leave the resource standing if the declaration goes.
        gcp.monitoring.MetricDescriptor(
            'themis-cost-exporter-list-cost',
            project=project,
            type=_METRIC_TYPE,
            metric_kind='GAUGE',
            value_type='INT64',
            unit='{cent}',
            display_name='Anthropic session list cost (cumulative)',
            description=(
                'Cumulative Anthropic list cost of every Managed Agents session in the workspace, in USD cents, by '
                'agent name; a running total observed each run, so deltas are a query over it.'
            ),
            labels=[
                gcp.monitoring.MetricDescriptorLabelArgs(
                    key=_AGENT_LABEL,
                    value_type='STRING',
                    description='The name of the agent the sessions ran.',
                )
            ],
            opts=pulumi.ResourceOptions.merge(child, pulumi.ResourceOptions(protect=True, retain_on_delete=True)),
        )

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
                                # Where the gauge is written, and the `generic_task` location its series carry.
                                _env('THEMIS_COST_EXPORTER_PROJECT', project),
                                _env('THEMIS_COST_EXPORTER_LOCATION', region),
                                _env('THEMIS_COST_EXPORTER_DEADLINE_SECONDS', str(_RUN_DEADLINE_SECONDS)),
                            ],
                            # A few pages of session JSON and one Monitoring write; nothing is held beyond the totals.
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
