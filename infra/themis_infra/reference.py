"""The reference-build Job: the mirror of `resources/reference.toml`, and the stores and genomes built from it.

One execution mirrors every pinned upstream into the resources bucket's `reference/` dataset, fetches the
Entrez status table of each historical set, and builds the hgvs-weaver-data stores and genomes under
`reference-stores/` and `reference-genomes/` (`tools/resources/job.py`). Every stage skips what already
exists, so an execution with nothing new to do reads provenance and exits; nothing schedules it. The
reference workflow (`.github/workflows/reference.yml`) starts it after a deploy of a commit that changed the
manifest, and by hand.

The runtime account may add objects to the resources bucket but never replace or delete one, so neither a
bug in the job nor a run racing another can change what a reader is pinned to, in its own datasets or the
gene-disease dumps beside them. The cost is that the job cannot repair a mirrored object that went wrong;
a person deletes it and the next run mirrors it again.

- `ReferenceBuild` — the Job, its runtime account and grant, and the account the workflow starts it as.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

from themis_infra import grants

JOB_NAME = 'themis-reference-build'
CONTAINER_NAME = 'build'
# The workflow whose runs on `main` may start the Job; its OIDC token's `workflow_ref` is this, `@<ref>`.
_WORKFLOW = 'populationgenomics/themis-internal/.github/workflows/reference.yml'
_WORKFLOW_REF = 'refs/heads/main'
# The workload identity pool `bootstrap.sh` creates for GitHub Actions.
_POOL = 'github'
# A first run downloads about 8 GB over HTTPS and copies 6 GB within GCS, fetches 190,000 versions' status
# from Entrez at three requests a second, and cuts five artefacts, two of them single-threaded genome builds of
# about twelve minutes each: a few hours, with room for a slow upstream.
_TIMEOUT_SECONDS = 6 * 3600
# Scratch is memory on Cloud Run: an assembly's inputs (up to 2.5 GB with its historical set), the genome kept
# for the Ensembl build (0.9 GB) and the builder's own working set, which reads a GenBank file whole.
_CPU = '8'
_MEMORY = '32Gi'


def _workflow_principal_set(project_number: str) -> str:
    """The IAM member naming runs of the reference workflow read from `main`, through the pool's provider."""
    return (
        f'principalSet://iam.googleapis.com/projects/{project_number}/locations/global/'
        f'workloadIdentityPools/{_POOL}/attribute.workflow_ref/{_WORKFLOW}@{_WORKFLOW_REF}'
    )


class ReferenceBuild(pulumi.ComponentResource):
    """The reference-build Job, the account it runs as, and the account the reference workflow starts it as.

    Attributes:
        job_name: The Cloud Run Job's name.
        dispatcher_email: The account the reference workflow federates into to start the Job.
    """

    def __init__(
        self,
        *,
        project: str,
        project_number: str,
        region: str,
        image: pulumi.Input[str],
        resources_bucket: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the Job, its runtime account and grant, and the workflow's starting account.

        Args:
            project: The GCP project id.
            project_number: The project's numeric id, for the pool path in the federation member.
            region: Where the Job runs.
            image: The reference-build image.
            resources_bucket: The resources bucket's name: what the Job reads and adds to.
            opts: Resource options (dependency wiring).
        """
        super().__init__('themis:infra:ReferenceBuild', 'themis', None, opts)
        child = pulumi.ResourceOptions(parent=self)

        runtime = gcp.serviceaccount.Account(
            'themis-reference-build-runtime',
            project=project,
            account_id='themis-reference-build',
            display_name='Themis reference-build runtime (mirror, Entrez status, stores and genomes)',
            opts=child,
        )
        grants.BucketObjectCreator(
            'themis-reference-build',
            member=runtime.member,
            bucket=resources_bucket,
            target='resources',
            opts=child,
        )
        job = gcp.cloudrunv2.Job(
            JOB_NAME,
            project=project,
            location=region,
            name=JOB_NAME,
            deletion_protection=False,
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=runtime.email,
                    timeout=f'{_TIMEOUT_SECONDS}s',
                    # A retry resumes rather than repeats: every stage skips what the failed attempt finished.
                    max_retries=1,
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            name=CONTAINER_NAME,
                            image=image,
                            # Appended to the image's entrypoint, `python -m tools.resources.job`.
                            args=['--project', project],
                            envs=[
                                # Past this an unfinished attempt is dead, so another execution may start.
                                gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(
                                    name='THEMIS_REFERENCE_BUILD_TIMEOUT_SECONDS', value=str(_TIMEOUT_SECONDS)
                                ),
                            ],
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(
                                limits={'cpu': _CPU, 'memory': _MEMORY}
                            ),
                        ),
                    ],
                ),
            ),
            opts=child,
        )
        self.job_name = job.name

        dispatcher = gcp.serviceaccount.Account(
            'themis-reference-dispatch',
            project=project,
            account_id='themis-reference-dispatch',
            display_name='Themis reference workflow (starts the reference-build Job)',
            opts=child,
        )
        self.dispatcher_email = dispatcher.email
        grants.FederatedImpersonator(
            'github-reference-workflow',
            member=_workflow_principal_set(project_number),
            account=dispatcher.name,
            target='themis-reference-dispatch',
            opts=child,
        )
        grants.JobRunner(
            'themis-reference-dispatch',
            member=dispatcher.member,
            job=job.name,
            project=project,
            location=region,
            target='reference-build',
            opts=child,
        )
        self.register_outputs({'job_name': self.job_name, 'dispatcher_email': self.dispatcher_email})
