"""Logs stay in the stack's region, the Data Access audit logs the design names are on, and pgAudit keeps values."""

from __future__ import annotations

import re

from themis_infra import capture

_BUCKET_TYPE = 'gcp:logging/projectBucketConfig:ProjectBucketConfig'
_SINK_TYPE = 'gcp:logging/projectSink:ProjectSink'
_AUDIT_CONFIG_TYPE = 'gcp:projects/iAMAuditConfig:IAMAuditConfig'
_INSTANCE_TYPE = 'gcp:sql/databaseInstance:DatabaseInstance'
_EXCLUDED_LOG = re.compile(r'NOT LOG_ID\("(?P<log_id>[^"]+)"\)')
# What the platform routes to `_Required` (https://cloud.google.com/logging/docs/routing/overview#required-bucket).
_REQUIRED_LOG_IDS = {
    f'{origin}.googleapis.com/{log}'
    for origin in ('cloudaudit', 'externalaudit')
    for log in ('activity', 'system_event', 'access_transparency')
}
_BUCKET_DESTINATION = re.compile(
    r'logging\.googleapis\.com/projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)/buckets/(?P<bucket>[^/]+)'
)


def _default_sink(program: capture.Capture) -> capture.Registration:
    (sink,) = [s for s in capture.of_type(program, _SINK_TYPE) if s.state['name'] == '_Default']
    return sink


def test_default_sink_routes_into_a_bucket_in_the_stack_region(program: capture.Capture) -> None:
    config = capture.stack_config('dev')
    destination = _BUCKET_DESTINATION.fullmatch(str(_default_sink(program).state['destination']))
    assert destination is not None
    assert destination['project'] == config['gcp:project']
    assert destination['location'] == config['gcp:region']
    buckets = {b.state['bucketId']: b.state for b in capture.of_type(program, _BUCKET_TYPE)}
    assert buckets[destination['bucket']]['location'] == config['gcp:region']


def test_default_sink_takes_everything_required_does_not(program: capture.Capture) -> None:
    # A `_Default` that took a `_Required` log too would store it twice; one that excluded more would drop entries.
    clauses = str(_default_sink(program).state['filter']).split(' AND ')
    excluded = {_EXCLUDED_LOG.fullmatch(clause) for clause in clauses}
    assert None not in excluded, clauses
    assert {match['log_id'] for match in excluded if match} == _REQUIRED_LOG_IDS


def test_reads_and_writes_are_logged_for_secrets_keys_the_database_and_objects(program: capture.Capture) -> None:
    logged = {
        str(config.state['service']): {entry['logType'] for entry in config.state['auditLogConfigs']}
        for config in capture.of_type(program, _AUDIT_CONFIG_TYPE)
    }
    for service in (
        'secretmanager.googleapis.com',
        'cloudkms.googleapis.com',
        'cloudsql.googleapis.com',
        'storage.googleapis.com',
    ):
        assert {'ADMIN_READ', 'DATA_READ', 'DATA_WRITE'} <= logged.get(service, set()), service


def test_audit_logs_exempt_nobody(program: capture.Capture) -> None:
    for config in capture.of_type(program, _AUDIT_CONFIG_TYPE):
        assert not any(entry.get('exemptedMembers') for entry in config.state['auditLogConfigs']), config.state


def test_pgaudit_records_every_statement_with_its_values(program: capture.Capture) -> None:
    (instance,) = capture.of_type(program, _INSTANCE_TYPE)
    flags = {flag['name']: flag['value'] for flag in instance.state['settings']['databaseFlags']}
    assert flags['cloudsql.enable_pgaudit'] == 'on'
    assert flags['pgaudit.log'] == 'all'
    assert flags['pgaudit.log_parameter'] == 'on'
    assert flags.get('cloudsql.pgaudit_mask_literals', 'off') == 'off'
