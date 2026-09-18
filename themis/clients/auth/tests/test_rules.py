"""Admission derived from the contract, off compiled descriptors and nothing else."""

from __future__ import annotations

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool

from themis.clients.auth import context as auth_context
from themis.clients.auth import rules
from themis.rpc import auth_pb2, literature_pb2, sandbox_options_pb2, store_pb2

_PROJECT = 'x'
_WEB = rules.account_email('themis-web', _PROJECT)
_JOB = rules.account_email('themis-sandbox-job', _PROJECT)
_CLU = rules.account_email('themis-clu', _PROJECT)
_SESSION = auth_pb2.SessionContext(project_id='p', analysis_id='a')
_LITERATURE = literature_pb2.DESCRIPTOR.services_by_name['Literature']
_STORE = store_pb2.DESCRIPTOR.services_by_name['Store']

_SELF = sandbox_options_pb2.CALLING_AS_SELF
_AGENT_SESSION = sandbox_options_pb2.CALLING_AS_AGENT_SESSION
_WORKER_SESSION = sandbox_options_pb2.CALLING_AS_WORKER_SESSION


def _rule(service: object, method: str) -> rules.Rule:
    return rules.rule_for(service.methods_by_name[method])  # pyright: ignore[reportAttributeAccessIssue]


def _auth(
    caller: str, calling_as: sandbox_options_pb2.CallingAs, session: auth_pb2.SessionContext | None
) -> auth_context.AuthContext:
    return auth_context.AuthContext(caller=caller, calling_as=calling_as, session=session)


def test_a_named_principal_is_the_account_and_its_claim() -> None:
    rule = _rule(_LITERATURE, 'DescribePaper')
    assert rules.Principal('themis-web', _SELF) in rule.principals
    assert rule.admits(_auth(_WEB, _SELF, None), project=_PROJECT)


def test_the_same_account_id_in_another_project_is_not_the_principal() -> None:
    other = _auth(rules.account_email('themis-web', 'y'), _SELF, None)
    assert not _rule(_LITERATURE, 'DescribePaper').admits(other, project=_PROJECT)


def test_a_session_claimed_by_a_self_acting_caller_admits_nothing_extra() -> None:
    # web is named on DescribePaper calling as itself; a session on the call is attribution, not a way in.
    rule = _rule(_LITERATURE, 'GetMarkdown')
    assert not rule.admits(_auth(_WEB, _SELF, _SESSION), project=_PROJECT)


def test_the_agent_is_the_job_account_claiming_the_agent_s_session_that_resolves() -> None:
    rule = _rule(_LITERATURE, 'GetMarkdown')
    assert rules.Principal('themis-sandbox-job', _AGENT_SESSION) in rule.principals
    assert rule.admits(_auth(_JOB, _AGENT_SESSION, _SESSION), project=_PROJECT)
    assert not rule.admits(_auth(_JOB, _AGENT_SESSION, None), project=_PROJECT)  # the session did not resolve


def test_the_worker_s_claim_is_not_the_agent_s() -> None:
    # Same account; the claim decides which member the call is, and store names only the worker's.
    assert not _rule(_LITERATURE, 'GetMarkdown').admits(_auth(_JOB, _WORKER_SESSION, _SESSION), project=_PROJECT)
    assert _rule(_STORE, 'PutWorkingDocument').admits(_auth(_JOB, _WORKER_SESSION, _SESSION), project=_PROJECT)
    assert not _rule(_STORE, 'PutWorkingDocument').admits(_auth(_JOB, _AGENT_SESSION, _SESSION), project=_PROJECT)


def test_the_deny_rule_admits_nobody_but_the_developer() -> None:
    for auth in (_auth(_WEB, _SELF, _SESSION), _auth(_JOB, _AGENT_SESSION, _SESSION)):
        assert not rules.DENY.admits(auth, project=_PROJECT)
    assert rules.DENY.admits(_auth(_CLU, _SELF, None), project=_PROJECT)


def test_the_developer_identity_is_admitted_on_every_rpc_without_being_named() -> None:
    # CLU exists to exercise a deployed system by hand; the contract names it nowhere and the rule admits it
    # everywhere. Its claim rules still hold: a session it claims is resolved, and the wrong claim is not it.
    for service, method in (
        (_LITERATURE, 'GetMarkdown'),
        (_STORE, 'PutWorkingDocument'),
        (_LITERATURE, 'MaybeIngestPapers'),
    ):
        rule = _rule(service, method)
        assert rules.Principal('themis-clu', _SELF) not in rule.principals
        assert rule.admits(_auth(_CLU, _SELF, None), project=_PROJECT), method
        assert rule.admits(_auth(_CLU, _SELF, _SESSION), project=_PROJECT), method
        assert not rule.admits(_auth(_CLU, _AGENT_SESSION, _SESSION), project=_PROJECT), method


def test_a_path_no_contract_declares_has_no_rule() -> None:
    assert rules.rule_for_path('/themis.rpc.nowhere.Nothing/Call') is None


def test_a_declared_path_derives_the_same_rule_as_its_descriptor() -> None:
    assert rules.rule_for_path('/themis.rpc.literature.Literature/MaybeIngestPapers') == _rule(
        _LITERATURE, 'MaybeIngestPapers'
    )


def test_every_member_names_an_account_and_a_claim() -> None:
    named = rules.principals()
    assert named  # the enum has named members
    assert all(p.account_id and p.calling_as != sandbox_options_pb2.CALLING_AS_UNSPECIFIED for p in named.values())
    assert sandbox_options_pb2.CALLER_UNSPECIFIED not in named


def test_the_job_account_never_calling_as_itself_and_is_named() -> None:
    assert rules.is_named(_JOB, project=_PROJECT)
    assert not rules.calling_as_self(_JOB, project=_PROJECT)
    assert rules.calling_as_self(_WEB, project=_PROJECT)
    assert not rules.is_named(rules.account_email('themis-nobody', _PROJECT), project=_PROJECT)


def test_admitting_the_unspecified_member_is_refused() -> None:
    # Built by hand: no committed contract names CALLER_UNSPECIFIED, and this check is what keeps it so.
    pool = descriptor_pool.DescriptorPool()
    for dependency in (descriptor_pb2.DESCRIPTOR, sandbox_options_pb2.DESCRIPTOR):
        proto = descriptor_pb2.FileDescriptorProto()
        dependency.CopyToProto(proto)
        pool.Add(proto)
    file = descriptor_pb2.FileDescriptorProto(
        name='t/bad.proto', package='t.bad', syntax='proto3', dependency=[sandbox_options_pb2.DESCRIPTOR.name]
    )
    file.message_type.add(name='E')
    method = file.service.add(name='S').method.add(name='Call', input_type='.t.bad.E', output_type='.t.bad.E')
    method.options.Extensions[sandbox_options_pb2.admits_caller].append(  # pyright: ignore[reportArgumentType]
        sandbox_options_pb2.CALLER_UNSPECIFIED
    )
    pool.Add(file)
    with pytest.raises(ValueError, match='CALLER_UNSPECIFIED'):
        rules.rule_for(pool.FindMethodByName('t.bad.S.Call'))
