"""The `Caller` enum names principals the program can produce (rpc-authorization.md).

A member is an account plus what a call from it must claim to be acting for. The account id is what a
verifier completes into the email it admits, so a member naming an account the infrastructure does not
create admits nobody and a renamed account leaves a stale member behind; the program run under mocks is
the source of truth. Two members may share an account only if their claims differ — otherwise one of
them is unreachable — and the sandbox job's account, which forwards the guest's calls, is never admitted
as acting for itself alone: every one of its members carries a session.
"""

from __future__ import annotations

from themis.rpc import sandbox_options_pb2
from themis_infra import capture

_ACCOUNT_TYPE = 'gcp:serviceaccount/account:Account'
_SANDBOX_JOB = 'themis-sandbox'


def _declared_account_ids(program: capture.Capture) -> set[str]:
    return {
        str(registration.state['accountId'])
        for urn, registration in program.resources.items()
        if capture.urn_type_chain(urn)[-1] == _ACCOUNT_TYPE
    }


def _members() -> dict[str, tuple[str, int]]:
    """Each named `Caller` member (the zero value excluded) to (account id, calling_as)."""
    out = {}
    for value in sandbox_options_pb2.Caller.DESCRIPTOR.values:
        if value.number == 0:
            continue
        options = value.GetOptions()
        # grpcio-tools types the extension as a bare FieldDescriptor, not the handle Extensions[] expects.
        account = options.Extensions[sandbox_options_pb2.account_id]  # pyright: ignore[reportArgumentType]
        calling_as = options.Extensions[sandbox_options_pb2.calling_as]  # pyright: ignore[reportArgumentType]
        out[value.name] = (account, calling_as)
    return out


def test_every_caller_names_a_declared_account(program: capture.Capture) -> None:
    declared = _declared_account_ids(program)
    assert declared, 'the program declared no service account; the mocks are not running it'
    members = _members()
    assert members, 'the Caller enum has no named member'
    undeclared = {name: account for name, (account, _) in members.items() if account not in declared}
    assert not undeclared, f'Caller members name accounts the program does not declare: {undeclared}'


def test_every_caller_carries_an_account_and_a_claim() -> None:
    # A member without an account completes to `@<project>…` and admits nobody; one without a claim
    # cannot be told from a sibling sharing its account.
    incomplete = [
        name
        for name, (account, calling_as) in _members().items()
        if not account or calling_as == sandbox_options_pb2.CALLING_AS_UNSPECIFIED
    ]
    assert not incomplete, f'Caller members without an account_id or calling_as: {incomplete}'


def test_members_sharing_an_account_differ_in_what_they_act_for() -> None:
    seen: dict[tuple[str, int], str] = {}
    for name, key in _members().items():
        assert key not in seen, f'{name} and {seen[key]} name the same account acting for the same thing'
        seen[key] = name


def test_the_sandbox_job_account_never_calling_as_itself_alone(program: capture.Capture) -> None:
    # The worker forwards the guest's calls, so every principal on its account carries a session: the
    # agent's, or its own within one. An `CALLING_AS_SELF` member on it would admit the worker anywhere it
    # was named with no session to attribute the call to.
    jobs = [w for w in program.workloads if w.kind == 'job' and w.name == _SANDBOX_JOB]
    assert jobs, f'no {_SANDBOX_JOB!r} job in the program'
    job_account = jobs[0].service_account.split('@', 1)[0]
    on_job_account = {name: calling_as for name, (account, calling_as) in _members().items() if account == job_account}
    assert on_job_account, 'no Caller names the sandbox job account; the agent cannot be admitted'
    assert sandbox_options_pb2.CALLING_AS_SELF not in on_job_account.values(), on_job_account
