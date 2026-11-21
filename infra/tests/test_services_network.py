"""The services network resolves each dialed internal service's run.app host to addresses Cloud Run counts as internal.

An internal-ingress service admits a request only if it arrives over a same-project VPC. One of the ways Google
documents for a caller on a VPC to reach it there is DNS that answers the service's `run.app` host with a
private-access VIP (`private.googleapis.com` or `restricted.googleapis.com`), on a subnet with Private Google
Access. A workload with Direct VPC egress sends its DNS queries to the VPC's DNS, where a private zone gives that
answer. A zone is authoritative for its DNS name, so a name under it that has no record does not resolve at all.
Each zone therefore covers the host of one service and holds its record at the apex.

The workloads route only private ranges into the network, so it carries no call to a public host and has no NAT.
A workload that routed all traffic into it would need one.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import NamedTuple

import pytest

from themis_infra import capture, services_network

_NETWORK_TYPE = 'gcp:compute/network:Network'
_SUBNETWORK_TYPE = 'gcp:compute/subnetwork:Subnetwork'
_SERVICES_NETWORK_TYPE = 'themis:infra:ServicesNetwork'
_ZONE_TYPE = 'gcp:dns/managedZone:ManagedZone'
_RECORD_TYPE = 'gcp:dns/recordSet:RecordSet'
_DNS_POLICY_TYPE = 'gcp:dns/policy:Policy'
_ROUTER_TYPE = 'gcp:compute/router:Router'
_NAT_TYPE = 'gcp:compute/routerNat:RouterNat'
_API_TYPE = 'gcp:projects/service:Service'
_SERVICE_TYPE = 'gcp:cloudrunv2/service:Service'
_JOB_TYPE = 'gcp:cloudrunv2/job:Job'
_PROJECT_BINDING_TYPE = 'gcp:projects/iAMMember:IAMMember'
_INTERNAL = frozenset({'INGRESS_TRAFFIC_INTERNAL_ONLY', 'INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER'})
_INVOKER = 'roles/run.invoker'
_DNS_API = 'dns.googleapis.com'
_DNS_ROLE = 'roles/dns.admin'
_RUN_APP = '.run.app.'
# Google's two private-access VIPs. Each serves `*.run.app`; a record that mixes them is unsupported.
_PRIVATE_ACCESS_VIPS = (ipaddress.ip_network('199.36.153.8/30'), ipaddress.ip_network('199.36.153.4/30'))


def _services_network(program: capture.Capture, type_: str) -> capture.Registration:
    (registration,) = [
        registration
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn) == [_SERVICES_NETWORK_TYPE, type_]
    ]
    return registration


def _network_urls(program: capture.Capture) -> set[str]:
    """The two forms a resource names the services network by."""
    network = _services_network(program, _NETWORK_TYPE)
    return {network.id, str(network.state['self_link'])}


def _visible_to(zone: capture.Registration) -> list[str]:
    """The networks a zone answers on."""
    return [str(network['networkUrl']) for network in zone.state.get('privateVisibilityConfig', {}).get('networks', [])]


def _run_app_zones(program: capture.Capture) -> list[capture.Registration]:
    """The zones that answer names under `run.app` on the services network."""
    networks = _network_urls(program)
    return [
        zone
        for zone in capture.of_type(program, _ZONE_TYPE)
        if networks.intersection(_visible_to(zone)) and f'.{zone.state["dnsName"]}'.endswith(_RUN_APP)
    ]


def _run_app_records(program: capture.Capture) -> list[capture.Registration]:
    """Every record of the zones that answer names under `run.app` on the services network."""
    zones = {zone.state['name'] for zone in _run_app_zones(program)}
    return [record for record in capture.of_type(program, _RECORD_TYPE) if record.state['managedZone'] in zones]


def _workloads(program: capture.Capture) -> list[capture.Registration]:
    return capture.of_type(program, _SERVICE_TYPE) + capture.of_type(program, _JOB_TYPE)


def _vpc_access(workload: capture.Registration) -> dict[str, object]:
    template = workload.state['template']
    template = template.get('template', template)  # a job nests its task template one level down
    return template.get('vpcAccess', {})


def _networks(workload: capture.Registration) -> set[str]:
    interfaces = _vpc_access(workload).get('networkInterfaces', [])
    assert isinstance(interfaces, list)
    return {str(interface['network']) for interface in interfaces}


def _egress(workload: capture.Registration) -> object:
    return _vpc_access(workload).get('egress')


def _attached(program: capture.Capture) -> list[capture.Registration]:
    """The workloads with Direct VPC egress on the services network."""
    network = _services_network(program, _NETWORK_TYPE).id
    return [workload for workload in _workloads(program) if network in _networks(workload)]


def _member(workload: capture.Registration) -> str:
    """The IAM member a workload runs as."""
    template = workload.state['template']
    template = template.get('template', template)  # a job nests its task template one level down
    return f'serviceAccount:{template["serviceAccount"]}'


def test_every_run_app_zone_is_private_to_the_services_network_alone(program: capture.Capture) -> None:
    # Visible on another network, a zone would change what that network's workloads resolve.
    zones = _run_app_zones(program)
    assert zones, 'no zone answers run.app on the services network; the check is vacuous'
    for zone in zones:
        assert zone.state['visibility'] == 'private', zone.state['name']
        elsewhere = sorted(set(_visible_to(zone)) - _network_urls(program))
        assert not elsewhere, f'{zone.state["name"]} also answers on {elsewhere}'
        assert not zone.state['privateVisibilityConfig'].get('gkeClusters')


def test_every_run_app_zone_holds_one_record_at_its_apex(program: capture.Capture) -> None:
    # A zone is authoritative for its host: while the zone has no record there, the host does not resolve on the
    # network at all.
    zones = _run_app_zones(program)
    assert zones, 'no zone answers run.app on the services network; the check is vacuous'
    records = capture.of_type(program, _RECORD_TYPE)
    for zone in zones:
        names = [str(record.state['name']) for record in records if record.state['managedZone'] == zone.state['name']]
        assert names == [zone.state['dnsName']], f'{zone.state["name"]} holds records at {names}'


def test_every_run_app_record_answers_with_one_private_access_vip(program: capture.Capture) -> None:
    records = _run_app_records(program)
    assert records, 'no run.app zone has a record; the check is vacuous'
    for record in records:
        name = record.state['name']
        assert record.state['type'] == 'A', f'{name} is a record of type {record.state["type"]}'
        addresses = [ipaddress.ip_address(address) for address in record.state['rrdatas']]
        assert addresses, f'{name} has no address'
        vips = {vip for vip in _PRIVATE_ACCESS_VIPS for address in addresses if address in vip}
        outside = [str(address) for address in addresses if not any(address in vip for vip in _PRIVATE_ACCESS_VIPS)]
        assert not outside, f'{name} answers with addresses outside the private-access VIPs: {outside}'
        assert len(vips) == 1, f'{name} mixes the private-access VIPs: {sorted(map(str, addresses))}'


def _cloud_run_urls(workload: capture.Registration) -> set[str]:
    """Every value in the workload's environment that is a URL at a host under `run.app`."""
    values = [value for value in capture.env_values(workload).values() if isinstance(value, str)]
    return {
        value
        for value in values
        if value.startswith('https://') and f'{urllib.parse.urlsplit(value).hostname}.'.endswith(_RUN_APP)
    }


class _Dial(NamedTuple):
    caller: str
    """The URN of a workload on the services network; a service and a job may share a name."""
    service: str
    """An internal service its account may invoke."""
    named: bool
    """Whether a binding on the service itself names the caller; a project-level invoker may call every service."""
    urls: tuple[str, ...]
    """The URLs Cloud Run serves that service at that the caller's environment holds."""


def _dials(program: capture.Capture) -> list[_Dial]:
    """Each workload on the services network with each internal service its account may invoke.

    A workload handed the URL of a service it may not invoke names it without dialing it: the evidence service
    puts the convert worker's URL into a Cloud Tasks task.
    """
    internal = {
        str(service.state['name']): frozenset(service.state['urls'])
        for service in capture.of_type(program, _SERVICE_TYPE)
        if service.state['ingress'] in _INTERNAL
    }
    invokers = {(b.target, b.member) for b in program.bindings if b.role == _INVOKER}
    everywhere = {b.member for b in program.bindings if b.role == _INVOKER and b.type_ == _PROJECT_BINDING_TYPE}
    dials = []
    for caller in _attached(program):
        member = _member(caller)
        handed = _cloud_run_urls(caller)
        for service, urls in sorted(internal.items()):
            named = (service, member) in invokers
            if named or member in everywhere:
                dials.append(_Dial(caller.urn, service, named, tuple(sorted(handed & urls))))
    return dials


def test_every_cloud_run_url_a_caller_on_the_network_is_handed_is_one_a_service_reports(
    program: capture.Capture,
) -> None:
    # A caller dials the URL as it is handed and mints its ID token for it, and Cloud Run admits a token only for
    # a URL it serves the service at. The tests below find a service's URLs in an environment by that identity.
    reported = {url for service in capture.of_type(program, _SERVICE_TYPE) for url in service.state['urls']}
    handed = {(str(caller.state['name']), url) for caller in _attached(program) for url in _cloud_run_urls(caller)}
    assert handed, 'no workload on the services network is handed a Cloud Run URL; the check is vacuous'
    unreported = sorted((caller, url) for caller, url in handed if url not in reported)
    assert not unreported, f'callers handed a Cloud Run URL that no service of the program is served at: {unreported}'


def test_every_caller_on_the_network_is_handed_a_url_of_each_service_a_binding_names_it_on(
    program: capture.Capture,
) -> None:
    dials = [dial for dial in _dials(program) if dial.named]
    assert dials, 'no workload on the services network may invoke an internal service; the check is vacuous'
    without = sorted((dial.caller, dial.service) for dial in dials if not dial.urls)
    assert not without, f'callers that may invoke an internal service and are handed none of its URLs: {without}'


def _dns_name(url: str) -> str:
    """The URL's host as a zone or a record names it."""
    return f'{urllib.parse.urlsplit(url).hostname}.'


def _dialed(program: capture.Capture) -> list[tuple[str, str, str]]:
    """Each caller on the services network with each internal service it may invoke and each URL of it that it holds."""
    dialed = [(dial.caller, dial.service, url) for dial in _dials(program) for url in dial.urls]
    assert dialed, 'no workload on the services network is handed the URL of an internal service; the check is vacuous'
    return dialed


def test_every_run_app_zone_covers_one_host_that_a_caller_on_the_network_dials(program: capture.Capture) -> None:
    # A zone for a suffix, a region's or `run.app.`, would also answer the hosts of other projects' Cloud Run
    # services with the private addresses and route the calls to them into the VPC.
    hosts = {_dns_name(url) for _, _, url in _dialed(program)}
    zones = _run_app_zones(program)
    assert zones, 'no zone answers run.app on the services network; the check is vacuous'
    wider = sorted(str(zone.state['dnsName']) for zone in zones if zone.state['dnsName'] not in hosts)
    assert not wider, f'zones for a name that no caller dials an internal service at: {wider}'


def test_every_caller_on_the_network_dials_an_internal_service_at_a_host_a_record_answers(
    program: capture.Capture,
) -> None:
    # A host with no zone keeps its public answer, and so does the hashed URL under `a.run.app` that Cloud Run
    # also serves a service at. A caller that routes only private ranges into the VPC would reach either over the
    # internet and be refused.
    answered = {str(record.state['name']) for record in _run_app_records(program)}
    elsewhere = sorted(dial for dial in _dialed(program) if _dns_name(dial[2]) not in answered)
    assert not elsewhere, f'callers handed a URL of an internal service that no record answers: {elsewhere}'


def test_every_caller_on_the_network_waits_for_the_record_of_each_internal_service_it_dials(
    program: capture.Capture,
) -> None:
    # A revision that started before a record can keep the host's public answer for hours. A workload's
    # registered dependencies are those its own inputs and `depends_on` carry, so a URL handed as a plain string
    # leaves its caller free to start first.
    records = {str(record.state['name']): record.urn for record in _run_app_records(program)}
    early = sorted(
        (caller, service, url)
        for caller, service, url in _dialed(program)
        if records.get(_dns_name(url)) not in program.dependencies[caller]
    )
    assert not early, f'callers that do not depend on the record answering an internal service URL they hold: {early}'


@pytest.mark.parametrize('service', ['a' * 56, 'Themis-auth', 'themis_auth', '1themis'])
def test_a_zone_name_cloud_dns_would_reject_is_refused(service: str) -> None:
    with pytest.raises(ValueError, match='not a Cloud DNS zone name'):
        services_network.zone_name(service)


def _networks_with_a_nat(program: capture.Capture) -> set[str]:
    natted_routers = {str(nat.state['router']) for nat in capture.of_type(program, _NAT_TYPE)}
    return {
        str(router.state['network'])
        for router in capture.of_type(program, _ROUTER_TYPE)
        if router.state['name'] in natted_routers
    }


def test_no_workload_routes_all_traffic_into_a_network_without_a_nat(program: capture.Capture) -> None:
    # Its instances have no external address, so a call to a public host would have no route out of the VPC.
    with_a_nat = _networks_with_a_nat(program)
    stranded = sorted(
        str(workload.state['name'])
        for workload in _workloads(program)
        if _egress(workload) == 'ALL_TRAFFIC' and _networks(workload) - with_a_nat
    )
    assert not stranded, f'workloads that route all traffic into a network with no NAT: {stranded}'


def test_the_services_network_has_no_nat(program: capture.Capture) -> None:
    # No workload routes a public call into it, and Google bills a NAT on a Cloud Run subnet that carries none.
    assert not _networks_with_a_nat(program) & _network_urls(program)


def test_the_subnet_delivers_the_vip_over_the_vpc(program: capture.Capture) -> None:
    # Without Private Google Access a packet to the VIP has no path from an instance with no external address.
    assert _services_network(program, _SUBNETWORK_TYPE).state['privateIpGoogleAccess'] is True


def test_the_services_network_logs_its_dns_queries_and_forwards_none(program: capture.Capture) -> None:
    # The query log is the record of what an attached workload asked and was answered. A server policy can also
    # send a network's queries to other name servers, which would take them past the zone.
    networks = _network_urls(program)
    policies = [
        policy
        for policy in capture.of_type(program, _DNS_POLICY_TYPE)
        if networks.intersection(str(network['networkUrl']) for network in policy.state.get('networks', []))
    ]
    assert len(policies) == 1, f'expected one DNS server policy on the services network, found {len(policies)}'
    (policy,) = policies
    assert policy.state['enableLogging'] is True
    assert not policy.state.get('alternativeNameServerConfig')


def test_the_network_waits_for_the_api_and_the_role_its_dns_needs(program: capture.Capture) -> None:
    # A stack that gains the zones enables the API and grants the deploy account the role in the same update.
    (network,) = [urn for urn in program.resources if capture.urn_type_chain(urn)[-1] == _SERVICES_NETWORK_TYPE]
    needed = {
        urn
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _API_TYPE and registration.state['service'] == _DNS_API
    } | {binding.urn for binding in program.bindings if binding.role == _DNS_ROLE}
    assert len(needed) == 2, f'expected the DNS API and one {_DNS_ROLE} binding, found {sorted(needed)}'
    assert needed <= program.dependencies[network]
