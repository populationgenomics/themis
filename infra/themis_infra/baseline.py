"""Per-environment baseline: enabled GCP services and the image registry.

What every Themis service in an environment needs before it can be created —
the GCP APIs the program calls, and the Artifact Registry repository its images
are pushed to and deployed from. Standing this up first lets later concern
modules (`web`, and database/storage/audit as they land) depend on a single
`Baseline` rather than each re-enabling services.
"""

from __future__ import annotations

import pulumi
import pulumi_gcp as gcp

# APIs the program's resources need; a new concern module adds its API here, so a
# fresh environment converges in one `pulumi up`. Bootstrap enables the substrate
# (state bucket, KMS, WIF) plus the meta-APIs the program can't turn on for itself:
# serviceusage (to enable these) and cloudresourcemanager (project IAM — e.g. the
# deploy and ingest role grants).
_REQUIRED_SERVICES = (
    'artifactregistry.googleapis.com',
    'run.googleapis.com',
    'compute.googleapis.com',
    'iap.googleapis.com',
    'storage.googleapis.com',
    'secretmanager.googleapis.com',
    'sqladmin.googleapis.com',
    'dataflow.googleapis.com',
)


class Baseline(pulumi.ComponentResource):
    """Enabled services and the shared Docker image registry for one environment.

    Attributes:
        image_registry: The Artifact Registry repository application images are
            pushed to and deployed from.
        image_prefix: Host/path prefix for images in `image_registry`, e.g.
            `australia-southeast1-docker.pkg.dev/<project>/themis`.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__('themis:infra:Baseline', name, None, opts)
        child = pulumi.ResourceOptions(parent=self)

        services = [
            gcp.projects.Service(
                f'{name}-{service.split(".", 1)[0]}',
                project=project,
                service=service,
                # Keep APIs enabled if the program is torn down — other tooling
                # and humans rely on them; disabling is disruptive and slow.
                disable_on_destroy=False,
                opts=child,
            )
            for service in _REQUIRED_SERVICES
        ]
        services_ready = pulumi.ResourceOptions(parent=self, depends_on=services)

        self.image_registry = gcp.artifactregistry.Repository(
            f'{name}-images',
            project=project,
            location=region,
            repository_id='themis',
            format='DOCKER',
            description='Themis application images.',
            opts=services_ready,
        )
        self.image_prefix = pulumi.Output.format(
            '{0}-docker.pkg.dev/{1}/{2}',
            region,
            project,
            self.image_registry.repository_id,
        )

        self.register_outputs({'image_prefix': self.image_prefix})
