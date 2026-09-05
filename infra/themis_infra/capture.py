"""Run the Pulumi program under mocks and record what it grants.

One run of `infra/__main__.py` against the dev stack's config with every opt-in switched on, no cloud
access, yielding every resource the program registers plus two derived views: each IAM binding as
(capability, member, role, target), and each Cloud Run workload with the service account it runs as.
The exhaustiveness test and the grants diagram both read from here, so the two cannot disagree about
what the program does.

The mocks synthesise the outputs the program reads off resources it creates. A service account's email
follows GCP's form, `<account_id>@<project>.iam.gserviceaccount.com`, so two accounts that share a
Pulumi resource name (the web and auth runtime SAs are both `themis-runtime`) stay distinct principals.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import runpy
from typing import NamedTuple, override

import pulumi
import yaml
from pulumi.runtime import mocks
from pulumi.runtime.proto import resource_pb2

_INFRA = pathlib.Path(__file__).resolve().parents[1]
_PROGRAM = _INFRA / '__main__.py'
_PROJECT = _INFRA / 'Pulumi.yaml'
_DEV_STACK = _INFRA / 'Pulumi.dev.yaml'

# Every IAM-binding resource type the GCP provider offers, by its type token's last segment.
BINDING_TYPE = re.compile(r'^gcp:[^:]+:\w*(IAMMember|IamMember|IAMBinding|IamBinding|IAMPolicy|IamPolicy)$')
GRANT_TYPE_PREFIX = 'themis:grants:'
# The stack booleans that switch a grant on. Forced on so the run is the maximal program; a boolean not
# named here fails the run until it is classified.
_OPT_INS = frozenset({'themis:cluDerivesSessionTokens', 'themis:enablePrScreenshotBucket'})

# The input that names what each binding type is over.
_TARGET_INPUT = {
    'gcp:cloudrunv2/serviceIamMember:ServiceIamMember': 'name',
    'gcp:cloudrunv2/jobIamMember:JobIamMember': 'name',
    'gcp:cloudtasks/queueIamMember:QueueIamMember': 'name',
    'gcp:storage/bucketIAMMember:BucketIAMMember': 'bucket',
    'gcp:kms/cryptoKeyIAMMember:CryptoKeyIAMMember': 'cryptoKeyId',
    'gcp:secretmanager/secretIamMember:SecretIamMember': 'secretId',
    'gcp:serviceaccount/iAMMember:IAMMember': 'serviceAccountId',
    'gcp:compute/subnetworkIAMMember:SubnetworkIAMMember': 'subnetwork',
    'gcp:iap/webBackendServiceIamMember:WebBackendServiceIamMember': 'webBackendService',
    'gcp:projects/iAMMember:IAMMember': 'project',
}
_SERVICE_ACCOUNT_TYPE = 'gcp:serviceaccount/account:Account'
_SERVICE_IDENTITY_TYPE = 'gcp:projects/serviceIdentity:ServiceIdentity'
_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'
_JOB_TYPE = 'gcp:cloudrunv2/job:Job'
_BACKEND_TYPE = 'gcp:compute/backendService:BackendService'
_NEG_TYPE = 'gcp:compute/regionNetworkEndpointGroup:RegionNetworkEndpointGroup'


class Binding(NamedTuple):
    """One IAM binding the program registers, with the capability it belongs to."""

    urn: str
    type_: str
    capability: str | None
    """The `grants` class it is a child of, or None for a binding outside any capability."""
    member: str
    role: str
    target: str


class Workload(NamedTuple):
    """A Cloud Run service or job and the service account it runs as."""

    kind: str
    """`service` or `job`."""
    name: str
    service_account: str
    """The runtime SA's email."""


class Fronting(NamedTuple):
    """A load-balancer backend and the Cloud Run service it fronts."""

    backend: str
    service: str


class Capture(NamedTuple):
    """What one mocked run of the program registered."""

    project: str
    """The stack's `gcp:project`, which the program's bucket names carry as a prefix."""
    resources: dict[str, mocks.MockMonitor.ResourceRegistration]
    bindings: list[Binding]
    workloads: list[Workload]
    frontings: list[Fronting]


class _Mocks(mocks.Mocks):
    """Enough of the provider for the program to run: synthesised outputs, and the data sources it reads."""

    def __init__(self, project: str) -> None:
        self._project = project

    @override
    def new_resource(self, args: mocks.MockResourceArgs) -> tuple[str, dict[str, object]]:
        name = args.inputs.get('name', args.name)
        email = f'{args.name}@{self._project}.iam.gserviceaccount.com'
        if args.typ == _SERVICE_ACCOUNT_TYPE:
            email = f'{args.inputs["accountId"]}@{self._project}.iam.gserviceaccount.com'
            name = f'projects/{self._project}/serviceAccounts/{email}'
        elif args.typ == _SERVICE_IDENTITY_TYPE:
            product = str(args.inputs['service']).removesuffix('.googleapis.com')
            email = f'service-123456789@gcp-sa-{product}.iam.gserviceaccount.com'
        # Outputs the program reads off resources that are not inputs; a key a type lacks is ignored.
        synthesised = {
            'name': name,
            'email': email,
            'member': f'serviceAccount:{email}',
            'unique_id': f'{args.name}-unique-id',
            'uri': f'https://{args.name}.run.app',
            'self_link': f'https://mock/{args.name}',
            'generated_id': 1,
            'connection_name': f'project:region:{args.name}',
            'address': '203.0.113.1',
        }
        return f'{args.name}-id', {**synthesised, **args.inputs}

    @override
    def call(self, args: mocks.MockCallArgs) -> tuple[dict[str, object], list[tuple[str, str]]]:
        image = {'image': 'mock-image'}
        match args.token:
            case 'gcp:organizations/getProject:getProject':
                return {'number': '123456789'}, []
            case 'gcp:kms/getKMSKeyRing:getKMSKeyRing':
                return {'id': f'projects/{self._project}/locations/mock/keyRings/themis'}, []
            case 'gcp:cloudrunv2/getService:getService':
                return {'templates': [{'containers': [image]}]}, []
            case 'gcp:cloudrunv2/getJob:getJob':
                containers = [{'name': container, **image} for container in ('refresh', 'worker')]
                return {'templates': [{'templates': [{'containers': containers}]}]}, []
        raise NotImplementedError(f'the program invoked {args.token}, which this mock does not answer')


class _Monitor(mocks.MockMonitor):
    """The mock monitor, refusing a URN registered twice — which the base overwrites.

    At least as strict as the engine: a mock URN carries only the immediate parent's type, so two
    same-named bindings under different grandparents collide here and not in a real update.
    """

    def __init__(self, program_mocks: mocks.Mocks) -> None:
        super().__init__(program_mocks)
        self.seen: set[str] = set()

    @override
    def RegisterResource(self, request: resource_pb2.RegisterResourceRequest) -> resource_pb2.RegisterResourceResponse:
        response = super().RegisterResource(request)
        if response.urn in self.seen:
            raise AssertionError(f'registered twice: {response.urn}')
        self.seen.add(response.urn)
        return response


def _dev_stack_config() -> dict[str, str]:
    """The dev stack's config, every secret a placeholder and every opt-in on."""
    stack = yaml.safe_load(_DEV_STACK.read_text('utf-8'))
    config = {}
    for key, value in stack['config'].items():
        if isinstance(value, dict):
            if set(value) != {'secure'}:
                raise ValueError(f'{key}: unexpected structured config')
            config[key] = 'mock-secret'
        elif isinstance(value, bool):
            if key not in _OPT_INS:
                raise ValueError(f'{key}: a stack boolean the capture does not know to force on')
            config[key] = 'true'
        else:
            config[key] = str(value)
    return config


def urn_type_chain(urn: str) -> list[str]:
    """The URN's type segment split on `$`: the immediate parent's type, then the resource's own."""
    return urn.split('::')[2].split('$')


def _bindings(resources: dict[str, mocks.MockMonitor.ResourceRegistration]) -> list[Binding]:
    bindings = []
    for urn, registration in resources.items():
        chain = urn_type_chain(urn)
        type_ = chain[-1]
        if not BINDING_TYPE.match(type_):
            continue
        parent = chain[-2] if len(chain) > 1 else ''
        capability = parent.removeprefix(GRANT_TYPE_PREFIX) if parent.startswith(GRANT_TYPE_PREFIX) else None
        state = registration.state
        if type_ not in _TARGET_INPUT:
            raise KeyError(f'{type_}: no target input known for this binding type ({urn})')
        bindings.append(
            Binding(
                urn=urn,
                type_=type_,
                capability=capability,
                member=str(state['member']),
                role=str(state['role']),
                target=str(state[_TARGET_INPUT[type_]]),
            )
        )
    return bindings


def _workloads(resources: dict[str, mocks.MockMonitor.ResourceRegistration]) -> list[Workload]:
    workloads = []
    for urn, registration in resources.items():
        type_ = urn_type_chain(urn)[-1]
        state = registration.state
        if type_ == _SERVICE_TYPE:
            workloads.append(Workload('service', str(state['name']), str(state['template']['serviceAccount'])))
        elif type_ == _JOB_TYPE:
            account = state['template']['template']['serviceAccount']
            workloads.append(Workload('job', str(state['name']), str(account)))
    return workloads


def _frontings(resources: dict[str, mocks.MockMonitor.ResourceRegistration]) -> list[Fronting]:
    """Each backend service joined to the Cloud Run service its serverless NEG names."""
    neg_services = {
        registration.id: str(registration.state['cloudRun']['service'])
        for urn, registration in resources.items()
        if urn_type_chain(urn)[-1] == _NEG_TYPE
    }
    frontings = []
    for urn, registration in resources.items():
        if urn_type_chain(urn)[-1] != _BACKEND_TYPE:
            continue
        for backend in registration.state['backends']:
            frontings.append(Fronting(str(registration.state['name']), neg_services[backend['group']]))
    return frontings


def capture_program() -> Capture:
    """Run the program once under mocks and return everything it registered.

    `set_mocks` configures Pulumi's process-global runtime, so one process runs one program.
    """
    config = _dev_stack_config()
    program_mocks = _Mocks(config['gcp:project'])
    monitor = _Monitor(program_mocks)
    # The project name is the namespace `pulumi.Config()` reads `themis:*` keys under.
    project = yaml.safe_load(_PROJECT.read_text('utf-8'))['name']

    @pulumi.runtime.test
    def run() -> None:
        runpy.run_path(str(_PROGRAM), run_name='themis_infra_program')

    # `set_mocks` registers the mock stack, and `pulumi.runtime.test` drives the program, on
    # `asyncio.get_event_loop()`, which raises once an earlier `asyncio.run` in the process has
    # cleared the thread's loop; give them one of their own.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pulumi.runtime.set_mocks(program_mocks, project=project, stack='dev', monitor=monitor)
        pulumi.runtime.set_all_config(config)
        run()
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    resources = monitor.resources
    return Capture(config['gcp:project'], resources, _bindings(resources), _workloads(resources), _frontings(resources))
