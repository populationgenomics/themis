"""Ingress from the public internet goes through IAP, or is a named exception that holds its own controls.

The rule and the case for each exception: docs/design/security.md, "Ingress from the public internet". The
exceptions: `themis_infra.public_ingress.EXCEPTIONS`. Each test names the property that has to hold of every
resource of its kind, whichever module declares it, so a new service, bucket or backend is held to it unasked.
IAP counts wherever it is enabled: on the load balancer's backend service (the operator backends), or on the
Cloud Run service itself (the sign-in services, `public_ingress.SIGN_IN_SERVICES`). Who a sign-in service's IAP admits
is that service's decision and is not checked here; who an operator backend's IAP admits is.
"""

from __future__ import annotations

import pytest

from themis_infra import capture, public_ingress, sandbox, web

_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'
_SQL_INSTANCE_TYPE = 'gcp:sql/databaseInstance:DatabaseInstance'
# Load-balancer backends the checks below do not read; the program may declare none until they do.
_UNREAD_BACKEND_TYPES = frozenset(
    {'gcp:compute/regionBackendService:RegionBackendService', 'gcp:compute/backendBucket:BackendBucket'}
)
_SERVICE_IAM_TYPE = 'gcp:cloudrunv2/serviceIamMember:ServiceIamMember'
_BUCKET_IAM_TYPE = 'gcp:storage/bucketIAMMember:BucketIAMMember'
_IAP_SERVICE_IAM_TYPE = 'gcp:iap/webCloudRunServiceIamMember:WebCloudRunServiceIamMember'
_IAP_BACKEND_IAM_TYPE = 'gcp:iap/webBackendServiceIamMember:WebBackendServiceIamMember'
_BUCKET_TYPE = 'gcp:storage/bucket:Bucket'
_PUBLIC_MEMBERS = frozenset({'allUsers', 'allAuthenticatedUsers'})
_IAP_ACCESSOR = 'roles/iap.httpsResourceAccessor'
_INVOKER = 'roles/run.invoker'
# Exactly `storage.objects.get`: an object is read by a name the reader already knows, and the bucket is not listed.
_PUBLIC_OBJECT_READ = 'roles/storage.legacyObjectReader'
_PUBLIC_SERVICES = public_ingress.names(public_ingress.Kind.CLOUD_RUN_SERVICE)
_PUBLIC_BUCKETS = public_ingress.names(public_ingress.Kind.BUCKET)
_PUBLIC_SQL = public_ingress.names(public_ingress.Kind.SQL_INSTANCE)


def _services(program: capture.Capture) -> dict[str, capture.Registration]:
    services = {str(service.state['name']): service for service in capture.of_type(program, _SERVICE_TYPE)}
    assert services, 'the program declared no Cloud Run service; the mocks are not running it'
    return services


def _iap_on_backend(backend: capture.Registration) -> bool:
    return backend.state.get('iap', {}).get('enabled') is True


def _operator_account(program: capture.Capture) -> str:
    return f'serviceAccount:themis-clu@{program.project}.iam.gserviceaccount.com'


def test_no_cloud_run_service_takes_traffic_from_the_internet_directly(program: capture.Capture) -> None:
    # A run.app URL open to the internet bypasses whatever gate the load balancer holds.
    open_ = sorted(
        name for name, service in _services(program).items() if service.state['ingress'] == 'INGRESS_TRAFFIC_ALL'
    )
    assert not open_, f'Cloud Run services that take traffic from the internet: {open_}'


def test_every_backend_is_of_a_kind_these_checks_read(program: capture.Capture) -> None:
    unread = sorted(type_ for type_ in _UNREAD_BACKEND_TYPES if capture.of_type(program, type_))
    assert not unread, f'backends of a kind the public-ingress checks do not read: {unread}'


def test_only_a_sign_in_service_carries_iap_on_itself(program: capture.Capture) -> None:
    # IAP on a Cloud Run service gates every path to it with accessors no check here constrains, so a data-plane
    # service with it would be reachable by whoever those accessors are.
    on_service = {name for name, service in _services(program).items() if service.state.get('iapEnabled') is True}
    assert on_service, 'no Cloud Run service carries IAP; the check is vacuous'
    assert on_service <= public_ingress.SIGN_IN_SERVICES, f'IAP on services that are not sign-in services: {on_service}'
    granted = {b.target for b in program.bindings if b.type_ == _IAP_SERVICE_IAM_TYPE}
    assert granted <= public_ingress.SIGN_IN_SERVICES, f'IAP access granted on services that are not sign-in: {granted}'


def test_every_load_balancer_backend_is_behind_iap_or_fronts_an_exception(program: capture.Capture) -> None:
    services = _services(program)
    backends = capture.of_type(program, capture.BACKEND_TYPE)
    assert backends, 'the program declared no load-balancer backend'
    ungated = []
    for backend in backends:
        fronted = capture.fronted_services(program, backend)
        assert fronted, f'backend {backend.state["name"]} fronts no Cloud Run service'
        behind_iap = _iap_on_backend(backend) or all(services[name].state.get('iapEnabled') is True for name in fronted)
        if not behind_iap and not fronted <= _PUBLIC_SERVICES:
            ungated.append((str(backend.state['name']), sorted(fronted)))
    assert not ungated, f'backends with no IAP in front of a service that is not an exception: {ungated}'


@pytest.mark.parametrize('service', sorted(_PUBLIC_SERVICES))
def test_an_exception_service_is_reached_only_through_a_deny_by_default_policy(
    program: capture.Capture, service: str
) -> None:
    assert _services(program)[service].state['ingress'] == 'INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER', (
        f'{service}: reachable other than through the load balancer, past its Cloud Armor policy'
    )
    backends = capture.backends_fronting(program, service)
    assert backends, f'{service}: no load-balancer backend'
    for backend in backends:
        policy = capture.security_policy(program, backend)
        assert policy is not None, f'{service}: backend {backend.state["name"]} has no Cloud Armor policy'
        rules = policy.state['rules']
        catch_alls = [
            rule['priority']
            for rule in rules
            if str(rule['action']).startswith('deny') and rule['match']['config']['srcIpRanges'] == ['*']
        ]
        assert catch_alls, f'{service}: no deny matching every source'
        previewed = [
            rule['priority'] for rule in rules if rule['priority'] in catch_alls and rule.get('preview') is True
        ]
        assert not previewed, f'{service}: the deny matching every source only previews: {previewed}'
        (default,) = [rule for rule in rules if rule['priority'] == sandbox.DEFAULT_RULE_PRIORITY]
        assert str(default['action']).startswith('deny'), f'{service}: the default rule admits every source'
        narrow = [rule['priority'] for rule in rules if rule['action'] == 'allow']
        assert all(priority < min(catch_alls) for priority in narrow), f'{service}: an allow below the deny'


def test_operator_backends_admit_only_the_operator_account(program: capture.Capture) -> None:
    iap_backends = {str(b.state['name']) for b in capture.of_type(program, capture.BACKEND_TYPE) if _iap_on_backend(b)}
    assert iap_backends, 'no backend with IAP on it; the check is vacuous'
    accessors: dict[str, set[str]] = {name: set() for name in iap_backends}
    for binding in program.bindings:
        if binding.type_ == _IAP_BACKEND_IAM_TYPE and binding.role == _IAP_ACCESSOR:
            accessors.setdefault(binding.target, set()).add(binding.member)
    wrong = {name: sorted(members) for name, members in accessors.items() if members != {_operator_account(program)}}
    assert not wrong, f'operator backends whose IAP admits other than the operator account: {wrong}'


def test_iap_access_is_granted_on_one_resource_at_a_time(program: capture.Capture) -> None:
    # A project-wide or resource-type-wide accessor would pass IAP on every operator backend at once.
    broad = sorted(
        (b.type_, b.member)
        for b in program.bindings
        if b.role == _IAP_ACCESSOR and b.type_ not in {_IAP_SERVICE_IAM_TYPE, _IAP_BACKEND_IAM_TYPE}
    )
    assert not broad, f'IAP access granted beyond one resource: {broad}'


def _public_grant_allowed(binding: capture.Binding, project: str) -> bool:
    if binding.type_ == _SERVICE_IAM_TYPE and binding.role == _INVOKER:
        return binding.target in _PUBLIC_SERVICES
    if binding.type_ == _BUCKET_IAM_TYPE and binding.role == _PUBLIC_OBJECT_READ:
        return binding.target.removeprefix(f'{project}-') in _PUBLIC_BUCKETS
    # A sign-in service's IAP is the gate, and it may admit any signed-in Google account; never an anonymous one.
    return (
        binding.type_ == _IAP_SERVICE_IAM_TYPE
        and binding.member == 'allAuthenticatedUsers'
        and binding.target in public_ingress.SIGN_IN_SERVICES
    )


def test_nothing_is_granted_to_the_public_but_the_exceptions(program: capture.Capture) -> None:
    public = [b for b in program.bindings if b.member in _PUBLIC_MEMBERS]
    assert public, 'no grant to a public member, though the exceptions hold some; the check is vacuous'
    stray = sorted(
        (b.type_, b.role, b.target, b.member) for b in public if not _public_grant_allowed(b, program.project)
    )
    assert not stray, f'grants to the public outside the exceptions: {stray}'


def test_every_exception_names_a_resource_the_program_declares(program: capture.Capture) -> None:
    # An exception nothing uses is a standing permission for whatever takes its name next.
    declared = {
        public_ingress.Kind.CLOUD_RUN_SERVICE: set(_services(program)),
        public_ingress.Kind.BUCKET: {
            str(b.state['name']).removeprefix(f'{program.project}-') for b in capture.of_type(program, _BUCKET_TYPE)
        },
        public_ingress.Kind.SQL_INSTANCE: {str(i.state['name']) for i in capture.of_type(program, _SQL_INSTANCE_TYPE)},
    }
    unused = sorted(e.name for e in public_ingress.EXCEPTIONS if e.name not in declared[e.kind])
    assert not unused, f'exceptions naming nothing the program declares: {unused}'


def test_cloud_sql_takes_connections_from_the_connectors_alone(program: capture.Capture) -> None:
    instances = capture.of_type(program, _SQL_INSTANCE_TYPE)
    assert instances, 'the program declared no Cloud SQL instance'
    for instance in instances:
        name = str(instance.state['name'])
        settings = instance.state['settings']
        ip = settings.get('ipConfiguration', {})
        assert not ip.get('authorizedNetworks'), f'{name}: an authorized network reaches the instance directly'
        assert settings.get('connectorEnforcement') == 'REQUIRED', f'{name}: takes connections from outside a connector'
        if ip.get('ipv4Enabled') is not False:
            assert name in _PUBLIC_SQL, f'{name}: a public IP, and not an exception'


@pytest.mark.parametrize(
    'service', ['themis.rpc.sheaf.Sheaf', 'themis.rpc.gene_disease.GeneDisease', 'grpc.health.v1.Health']
)
def test_a_grpc_route_takes_a_fully_qualified_service(service: str) -> None:
    assert web.GrpcRoute(service=service, backend_service='backend').url_map_path == f'/{service}/*'


@pytest.mark.parametrize(
    'service',
    [
        'Sheaf',
        'themis.rpc.sheaf',
        'themis.rpc.sheaf.Sheaf/',
        '/themis.rpc.sheaf.Sheaf',
        'themis.rpc.*',
        'themis.rpc.sheaf.Sh*',
    ],
)
def test_a_grpc_route_refuses_anything_but_a_service_name(service: str) -> None:
    with pytest.raises(ValueError, match='fully-qualified gRPC service name'):
        web.GrpcRoute(service=service, backend_service='backend')


def test_an_exact_route_may_not_take_a_method_from_a_grpc_route() -> None:
    # The URL map's longest match would send that one method past the gRPC route's backend and its IAP.
    routes = [
        web.GrpcRoute(service='themis.rpc.sheaf.Sheaf', backend_service='operator'),
        web.Route(path='/themis.rpc.sheaf.Sheaf/Publish', backend_service='public'),
    ]
    with pytest.raises(ValueError, match='under a gRPC route prefix'):
        web.check_routes(routes)


def test_two_routes_may_not_share_a_path() -> None:
    routes = [
        web.GrpcRoute(service='themis.rpc.sheaf.Sheaf', backend_service='a'),
        web.GrpcRoute(service='themis.rpc.sheaf.Sheaf', backend_service='b'),
    ]
    with pytest.raises(ValueError, match='duplicate'):
        web.check_routes(routes)
