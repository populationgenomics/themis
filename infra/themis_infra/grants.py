"""IAM grants as named capabilities: what a holder can do, stated once, where it is granted.

Every IAM binding the program makes is a child of one of the components here, and each component is one
ability — "may call this service", "may derive the bearer of any live session" — not one role. The class
docstring states the ability and its blast radius; a call site names the holder and the resource and says
nothing more. `infra/tests/test_grants.py` binds the rule: a binding registered outside these components
fails the suite.

A binding's URN includes its parent chain, so moving one under a capability would make Pulumi delete and
recreate it — a live revocation window. `Prior` names where a binding lived before it joined its capability
(its resource name and parent), and the capability aliases the binding to that URN, so the move is a no-op
to the deployment.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

import pulumi
import pulumi_gcp as gcp

from themis_infra import deploy_iam

# Roles the deploy SA needs to create/manage the program's resources.
_DEPLOY_ROLES: tuple[str, ...] = (
    'roles/artifactregistry.admin',
    'roles/cloudkms.admin',  # a CryptoKeyIAMMember reads and sets a key's policy; no predefined role has just that
    'roles/cloudscheduler.admin',
    'roles/cloudsql.admin',
    'roles/cloudtasks.queueAdmin',
    'roles/compute.admin',
    'roles/iam.serviceAccountAdmin',
    'roles/iam.serviceAccountUser',
    'roles/iap.admin',
    'roles/logging.configWriter',  # retention on the _Default log bucket (baseline.py) is a buckets.create/update
    'roles/monitoring.editor',  # the workspace-spend monitor's Monitoring resources (cost.py)
    'roles/run.admin',
    'roles/secretmanager.admin',
    'roles/serviceusage.serviceUsageAdmin',
)

# `client` opens the connection (Admin-API ephemeral cert), `instanceUser` authenticates as the IAM DB user.
_CLOUD_SQL_CONNECT_ROLES: tuple[tuple[str, str], ...] = (
    ('cloudsql-client', 'roles/cloudsql.client'),
    ('cloudsql-instance-user', 'roles/cloudsql.instanceUser'),
)

# Exactly `storage.objects.get`. `roles/storage.objectViewer` would also carry `storage.objects.list`, which
# makes a bucket publicly enumerable.
_PUBLIC_OBJECT_READ_ROLE = 'roles/storage.legacyObjectReader'

ObjectReadWriteRole = Literal['roles/storage.objectAdmin', 'roles/storage.objectUser']


class Prior(NamedTuple):
    """Where a capability's bindings lived before they joined it.

    Attributes:
        name: The stem the bindings were named from. For a capability of one binding, its resource name;
            for one of several (`DatabaseConnector`, `DeployAccountBuilder`), the prefix each name extends.
        parent: The component they were children of; `None` for top-level bindings (the stack itself).
    """

    name: str
    parent: pulumi.Resource | None = None


def service_account(email: pulumi.Input[str]) -> pulumi.Output[str]:
    """The IAM member string for a service account's email."""
    return pulumi.Output.concat('serviceAccount:', email)


class _Capability(pulumi.ComponentResource):
    """One holder, one ability; the bindings that constitute it are the children.

    The class name is the component's type token and so part of every child's URN: renaming a class
    replaces every binding under it unless the component carries an alias to its old type.
    """

    def __init__(self, name: str, opts: pulumi.ResourceOptions | None) -> None:
        super().__init__(f'themis:grants:{type(self).__name__}', name, None, opts)

    def _binding(self, prior: Prior | None) -> pulumi.ResourceOptions:
        # `Alias(parent=None)` means "was top-level" — distinct from an omitted parent, which would mean
        # "same parent as now".
        aliases = [] if prior is None else [pulumi.Alias(name=prior.name, parent=prior.parent)]
        return pulumi.ResourceOptions(parent=self, aliases=aliases)


class ServiceInvoker(_Capability):
    """May call one Cloud Run service.

    Cloud Run admits the holder's ID token for the service's URL and refuses every caller without a
    binding. That IAM gate is the whole of what the holder gains: a session-scoped service still
    resolves a session token on every request, so there the holder acts only as the sessions whose
    bearer it presents. A service with no such check is fully open to the holder.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        service: pulumi.Input[str],
        project: str,
        location: str,
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant `member` invoke on the service named `service`.

        Args:
            holder: A slug for the holder, for resource names (`themis-web`).
            member: The holder's IAM member string.
            service: The Cloud Run service's name.
            project: The GCP project.
            location: The service's region.
            target: A slug for the service, for resource names (`evidence`).
            prior: Where the binding lived before it joined this capability, if it did.
            opts: Resource options (dependency wiring).
        """
        name = f'{holder}-invokes-{target}'
        super().__init__(name, opts)
        gcp.cloudrunv2.ServiceIamMember(
            name,
            project=project,
            location=location,
            name=service,
            role='roles/run.invoker',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class PublicService(_Capability):
    """Anyone on the internet may call one Cloud Run service, unauthenticated.

    Cloud Run's IAM gate is open (`allUsers`); whatever check the servicer applies to a request is the
    only authentication there is.
    """

    def __init__(
        self,
        *,
        service: pulumi.Input[str],
        project: str,
        location: str,
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'public-invokes-{target}'
        super().__init__(name, opts)
        gcp.cloudrunv2.ServiceIamMember(
            name,
            project=project,
            location=location,
            name=service,
            role='roles/run.invoker',
            member='allUsers',
            opts=self._binding(prior),
        )
        self.register_outputs({})


class JobRunner(_Capability):
    """May start an execution of one Cloud Run Job, as the Job is declared.

    The image, env and arguments are the Job's; the holder chooses only when it runs. `run.invoker` on a
    Job carries `run.jobs.run` and nothing that changes what the run does.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        job: pulumi.Input[str],
        project: str,
        location: str,
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-runs-{target}'
        super().__init__(name, opts)
        gcp.cloudrunv2.JobIamMember(
            name,
            project=project,
            location=location,
            name=job,
            role='roles/run.invoker',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class SandboxSpawner(_Capability):
    """May start an execution of the sandbox Job with per-execution container overrides.

    The holder sets each execution's env — which session it serves, and the session's bearer — so it
    decides what an execution does, not only when it runs. The capability carries its own project-level
    custom role with exactly `run.jobs.run` and `run.jobs.runWithOverrides`: the predefined
    `run.jobsExecutorWithOverrides` adds `run.executions.cancel`, which the holder has no reason to hold.
    The role is one per project, so this class expresses one holder.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        job: pulumi.Input[str],
        project: str,
        location: str,
        prior: Prior | None = None,
        role_prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant `member` run-with-overrides on the sandbox Job.

        Args:
            holder: A slug for the holder, for resource names.
            member: The holder's IAM member string.
            job: The sandbox Job's name.
            project: The GCP project; the custom role is defined here.
            location: The Job's region.
            prior: Where the Job binding lived before it joined this capability, if it did.
            role_prior: Where the custom role lived before, if it did.
            opts: Resource options (dependency wiring).
        """
        name = f'{holder}-spawns-sandbox'
        super().__init__(name, opts)
        role = gcp.projects.IAMCustomRole(
            f'{name}-role',
            project=project,
            role_id='themisSandboxJobRunner',
            title='Themis sandbox job runner',
            permissions=['run.jobs.run', 'run.jobs.runWithOverrides'],
            opts=self._binding(role_prior),
        )
        gcp.cloudrunv2.JobIamMember(
            name,
            project=project,
            location=location,
            name=job,
            role=role.name,
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class SessionBearerDeriver(_Capability):
    """May MAC-sign with the session-token key, and so derive the bearer of any live session.

    A session's bearer is `HMAC(key, session_id)`; with this and a session id the holder acts as that
    session against every session-scoped service — the store, hello, the sheaf service — for as long as
    the session lives. The key material never leaves KMS, so the holder cannot take the key itself, but
    every session, past and present, is derivable while the grant stands. The holders are the
    credential's blast radius (`docs/plans/self-hosted-sandbox.md` §7).
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        key: pulumi.Input[str],
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant `member` MAC sign/verify on the session-token key.

        Args:
            holder: A slug for the holder, for resource names.
            member: The holder's IAM member string.
            key: The MAC `CryptoKey`'s id.
            prior: Where the binding lived before it joined this capability, if it did.
            opts: Resource options (dependency wiring).
        """
        name = f'{holder}-derives-session-bearers'
        super().__init__(name, opts)
        gcp.kms.CryptoKeyIAMMember(
            name,
            crypto_key_id=key,
            role='roles/cloudkms.signerVerifier',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class BucketObjectReader(_Capability):
    """May read and list every object in one bucket.

    The whole bucket, not a prefix: GCS IAM has no prefix scope, so a holder granted for one dataset in a
    shared bucket reads them all.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        bucket: pulumi.Input[str],
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-reads-{target}'
        super().__init__(name, opts)
        gcp.storage.BucketIAMMember(
            name,
            bucket=bucket,
            role='roles/storage.objectViewer',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class BucketObjectReadWriter(_Capability):
    """May read, list, create, overwrite and delete every object in one bucket.

    Either role confers that. `objectAdmin` adds object-ACL and per-object-retention permissions (the
    difference `gcloud iam roles describe` lists), inert on a uniform-access bucket with no object
    retention — every bucket here — so the two are the same ability; the role is explicit because
    changing a live binding's role recreates it.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        bucket: pulumi.Input[str],
        role: ObjectReadWriteRole,
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-writes-{target}'
        super().__init__(name, opts)
        gcp.storage.BucketIAMMember(
            name,
            bucket=bucket,
            role=role,
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class PublicObjectReader(_Capability):
    """Anyone may read an object in one bucket by its URL, with no credentials — but cannot list the bucket.

    An object is reachable only by a name the reader already knows.
    """

    def __init__(
        self,
        *,
        bucket: pulumi.Input[str],
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'public-reads-{target}'
        super().__init__(name, opts)
        gcp.storage.BucketIAMMember(
            name,
            bucket=bucket,
            role=_PUBLIC_OBJECT_READ_ROLE,
            member='allUsers',
            opts=self._binding(prior),
        )
        self.register_outputs({})


class SecretReader(_Capability):
    """May read the value of one Secret Manager secret — every version of it, not the current one alone."""

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        secret: pulumi.Input[str],
        project: str,
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-reads-{target}'
        super().__init__(name, opts)
        gcp.secretmanager.SecretIamMember(
            name,
            project=project,
            secret_id=secret,
            role='roles/secretmanager.secretAccessor',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class TaskEnqueuer(_Capability):
    """May put tasks on one Cloud Tasks queue, choosing each task's target URL and body.

    It cannot list or read the tasks already there. A task calls its target as the identity the enqueuer
    attaches to it, which takes `AccountUser` on that identity; enqueue alone reaches only what is open
    to an unauthenticated request.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        queue: pulumi.Input[str],
        project: str,
        location: str,
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-enqueues-{target}'
        super().__init__(name, opts)
        gcp.cloudtasks.QueueIamMember(
            name,
            project=project,
            location=location,
            name=queue,
            role='roles/cloudtasks.enqueuer',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class AccountImpersonator(_Capability):
    """May obtain one service account's credentials, and so reach everything the account holds.

    Access and ID tokens, signed blobs and JWTs — all as the account, with the impersonation audit-logged
    against the holder. An account granted this on itself gains no identity; that is `SelfSigner`.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        account: pulumi.Input[str],
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant `member` token creation on the account.

        Args:
            holder: A slug for the holder, for resource names.
            member: The holder's IAM member string.
            account: The service account's fully-qualified name (`projects/…/serviceAccounts/…`).
            target: A slug for the account, for resource names.
            prior: Where the binding lived before it joined this capability, if it did.
            opts: Resource options (dependency wiring).
        """
        name = f'{holder}-impersonates-{target}'
        super().__init__(name, opts)
        gcp.serviceaccount.IAMMember(
            name,
            service_account_id=account,
            role='roles/iam.serviceAccountTokenCreator',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class SelfSigner(_Capability):
    """Signs blobs as itself through the IAM Credentials API, for keyless V4 signed URLs; confers no other identity.

    The same role as `AccountImpersonator`, granted to an account on its own identity: a Cloud Run runtime
    account has no private key, so signing a URL means asking IAM Credentials to sign as itself, and
    `signBlob` on oneself is what that takes. Minting its own tokens is what the account already does. The
    class takes the account and binds it to itself, so it cannot be an impersonation wearing this name.
    """

    def __init__(
        self,
        holder: str,
        *,
        account: gcp.serviceaccount.Account,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant `account` token creation on itself.

        Args:
            holder: A slug for the account, for resource names.
            account: The service account.
            prior: Where the binding lived before it joined this capability, if it did.
            opts: Resource options (dependency wiring).
        """
        name = f'{holder}-signs-as-itself'
        super().__init__(name, opts)
        gcp.serviceaccount.IAMMember(
            name,
            service_account_id=account.name,
            role='roles/iam.serviceAccountTokenCreator',
            member=account.member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class AccountUser(_Capability):
    """May run a workload as one service account.

    The holder attaches the account to a task, job or service it creates, and that workload then calls out
    as the account. It does not let the holder obtain the account's credentials itself — that is
    `AccountImpersonator`.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        account: pulumi.Input[str],
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant `member` actAs on the account.

        Args:
            holder: A slug for the holder, for resource names.
            member: The holder's IAM member string.
            account: The service account's fully-qualified name (`projects/…/serviceAccounts/…`).
            target: A slug for the account, for resource names.
            prior: Where the binding lived before it joined this capability, if it did.
            opts: Resource options (dependency wiring).
        """
        name = f'{holder}-acts-as-{target}'
        super().__init__(name, opts)
        gcp.serviceaccount.IAMMember(
            name,
            service_account_id=account,
            role='roles/iam.serviceAccountUser',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class DatabaseConnector(_Capability):
    """May connect to any Cloud SQL instance in the project and log in as its own IAM database user.

    Project-wide because neither role has an instance scope. What the login can do once connected is the
    table grants the migrations apply to it, not this.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        project: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant `member` the two connector roles on the project.

        Args:
            holder: A slug for the holder, for resource names.
            member: The holder's IAM member string.
            project: The GCP project holding the instances.
            prior: Where the bindings lived before they joined this capability, if they did: `name` is the
                prefix of both — `<name>-cloudsql-client`, `<name>-cloudsql-instance-user`.
            opts: Resource options (dependency wiring).
        """
        super().__init__(f'{holder}-connects-to-cloudsql', opts)
        for slug, role in _CLOUD_SQL_CONNECT_ROLES:
            gcp.projects.IAMMember(
                f'{holder}-{slug}',
                project=project,
                role=role,
                member=member,
                opts=self._binding(None if prior is None else Prior(f'{prior.name}-{slug}', prior.parent)),
            )
        self.register_outputs({})


class DataflowWorker(_Capability):
    """May run as a Dataflow worker in the project: claim work items and report status for any job there.

    Project-wide, since the role has no per-job scope — and the role carries more than Dataflow: it reads
    and creates objects in every bucket in the project (the store's session data included) and deletes
    any VM. A worker identity holding it has that reach whatever its bucket-scoped grants say.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        project: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-runs-dataflow-workers'
        super().__init__(name, opts)
        gcp.projects.IAMMember(
            name,
            project=project,
            role='roles/dataflow.worker',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class SubnetUser(_Capability):
    """May place network interfaces on one subnet: the VMs the holder launches get their addresses there.

    The role also lets those VMs take external addresses (`useExternalIp`), subject to org policy.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        subnetwork: pulumi.Input[str],
        region: pulumi.Input[str],
        project: str,
        target: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-uses-{target}'
        super().__init__(name, opts)
        gcp.compute.SubnetworkIAMMember(
            name,
            project=project,
            region=region,
            subnetwork=subnetwork,
            role='roles/compute.networkUser',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class IapAccessor(_Capability):
    """May pass IAP to reach the web app — in a browser, or with an ID token for the IAP audience.

    The coarse "may reach the app" gate. What the holder may do inside is the app's own roles, decided
    from its identity after IAP admits it.
    """

    def __init__(
        self,
        holder: str,
        *,
        member: pulumi.Input[str],
        backend_service: pulumi.Input[str],
        project: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        name = f'{holder}-passes-iap'
        super().__init__(name, opts)
        gcp.iap.WebBackendServiceIamMember(
            name,
            project=project,
            web_backend_service=backend_service,
            role='roles/iap.httpsResourceAccessor',
            member=member,
            opts=self._binding(prior),
        )
        self.register_outputs({})


class DeployAccountBuilder(_Capability):
    """The CI deploy account may create and manage every kind of resource the program declares.

    `bootstrap.sh` grants the deploy SA only the IAM root it needs before Pulumi can run: `projectIamAdmin`
    (so the program can set project IAM), `storage.admin` (its own state), and KMS (the secrets provider,
    loaded on every op). Every other project role the SA needs to build the program's resources is here —
    versioned, drift-checked IaC instead of imperative bootstrap. `cloudkms.admin` is the widest of them:
    it also lets the SA disable or destroy any key version in the project and grant itself sign or
    decrypt — nothing `projectIamAdmin` did not already allow in one more step, but one step fewer.

    Safe despite the SA granting "itself" these roles: a fresh environment's first `pulumi up` is
    operator-run (Owner, per the fresh-environment runbook), so the operator creates these bindings;
    thereafter CI already holds them and merely re-asserts them. A role added to a live environment in the
    same run as the first resource that needs it is the one intra-run case: `bindings` exposes each so that
    resource can `depends_on` its role rather than race IAM propagation. `projectIamAdmin` and
    `storage.admin` stay in bootstrap — moving them would risk locking the SA out of the very IAM/state it
    needs to recover.

    Attributes:
        bindings: The project bindings by role.
    """

    def __init__(
        self,
        *,
        project: str,
        prior: Prior | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Grant the deploy SA its project roles.

        Args:
            project: The GCP project; also fixes the deploy SA's deterministic email.
            prior: Where the bindings lived before they joined this capability, if they did: `name` is
                the prefix of each — `<name>-<role slug>`.
            opts: Resource options (dependency wiring).
        """
        super().__init__('themis-deploy-builds-the-project', opts)
        member = service_account(deploy_iam.deploy_sa_email(project))
        self.bindings: dict[str, gcp.projects.IAMMember] = {}
        for role in _DEPLOY_ROLES:
            slug = role.removeprefix('roles/').replace('.', '-')
            self.bindings[role] = gcp.projects.IAMMember(
                f'themis-deploy-{slug}',
                project=project,
                role=role,
                member=member,
                opts=self._binding(None if prior is None else Prior(f'{prior.name}-{slug}', prior.parent)),
            )
        self.register_outputs({})
