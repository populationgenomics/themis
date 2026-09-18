"""Admission derived from an rpc's contract (rpc-authorization.md).

One method option in ``sandbox_options.proto`` says who may call an rpc, and nothing in code says it a
second time: each ``admits_caller`` names a ``Caller`` member, and a member is a **principal** — a
service account (verified from the ID token) and what a call from it claims to be calling as (trusted
because the account is). An rpc naming none admits nobody. :func:`rule_for` reads the option off a
method descriptor; :func:`rule_for_path` finds the descriptor for a call's ``/package.Service/Method``
path in the default descriptor pool, where every stub this process imported registered its contract;
a path no stub registered has no rule, and the interceptor refuses it outright.

A member names its account by id, the same in every environment; the project that completes it into an
email is the deployment's, so :meth:`Rule.admits` takes it.

One principal is admitted everywhere: ``CALLER_CLU``, the developer's identity for exercising a deployed
system by hand. No rpc names it, and no environment switches it off here — whether anyone may become it,
and where it may reach, is IAM's, permanent in dev and granted just-in-time in prod (rpc-authorization.md).
"""

from __future__ import annotations

import dataclasses
import functools
import typing
from typing import NamedTuple

from google.protobuf import descriptor, descriptor_pool

from themis.clients.auth import claim as claim_mod
from themis.clients.auth import context as context_mod
from themis.rpc import sandbox_options_pb2


class Principal(NamedTuple):
    """What one ``Caller`` member names: an account id, and what a call from it must claim to be calling as."""

    account_id: str
    calling_as: sandbox_options_pb2.CallingAs


@dataclasses.dataclass(frozen=True)
class Rule:
    """Who an rpc's contract admits: the principals named."""

    principals: frozenset[Principal]

    def admits(self, auth: context_mod.AuthContext, *, project: str) -> bool:
        """Whether the call ``auth`` describes matches a named principal, or is the universal one.

        The account must match, completed with ``project``; the claim must match; and a claim naming a
        session is satisfied only when the session resolved.
        """
        if _matches(auth, principals()[UNIVERSAL_CALLER], project=project):
            return True
        return any(_matches(auth, principal, project=project) for principal in self.principals)


# What an rpc naming no caller derives to. Not quite "nobody": the universal caller is admitted here too,
# since the rpc exists. A path with no contract at all has no rule (`rule_for_path` returns None) and is
# refused before any rule is consulted.
DENY = Rule(principals=frozenset())

UNIVERSAL_CALLER = sandbox_options_pb2.CALLER_CLU


def _matches(auth: context_mod.AuthContext, principal: Principal, *, project: str) -> bool:
    return (
        auth.caller == account_email(principal.account_id, project)
        and auth.calling_as == principal.calling_as
        and _session_satisfied(auth, principal)
    )


def _session_satisfied(auth: context_mod.AuthContext, principal: Principal) -> bool:
    """A principal naming a session is satisfied only by one that resolved."""
    return principal.calling_as not in claim_mod.SESSION_SCOPED or auth.session is not None


def account_email(account_id: str, project: str) -> str:
    """The service-account email ``account_id`` names in ``project``."""
    return f'{account_id}@{project}.iam.gserviceaccount.com'


@functools.cache
def principals() -> dict[int, Principal]:
    """Each ``Caller`` member's number to the principal its options name; the zero value excluded."""
    out = {}
    for value in sandbox_options_pb2.Caller.DESCRIPTOR.values:
        if value.number == 0:
            continue
        options = value.GetOptions()
        # grpcio-tools types each extension as a bare FieldDescriptor, not the handle Extensions[] expects.
        out[value.number] = Principal(
            options.Extensions[sandbox_options_pb2.account_id],  # pyright: ignore[reportArgumentType]
            typing.cast(
                'sandbox_options_pb2.CallingAs',
                options.Extensions[sandbox_options_pb2.calling_as],  # pyright: ignore[reportArgumentType]
            ),
        )
    return out


def calling_as_self(account_email_: str, *, project: str) -> bool:
    """Whether some member lets ``account_email_`` call as itself — if none does, a call from it must claim."""
    return any(
        account_email(principal.account_id, project) == account_email_
        and principal.calling_as == sandbox_options_pb2.CALLING_AS_SELF
        for principal in principals().values()
    )


def is_named(account_email_: str, *, project: str) -> bool:
    """Whether any member names ``account_email_`` at all."""
    return any(account_email(principal.account_id, project) == account_email_ for principal in principals().values())


def rule_for(method: descriptor.MethodDescriptor) -> Rule:
    """The rule ``method``'s ``admits_caller`` option declares.

    Raises:
        ValueError: An ``admits_caller`` names ``CALLER_UNSPECIFIED``, which names no principal and would
            read as an admission while admitting nobody.
    """
    numbers = list(method.GetOptions().Extensions[sandbox_options_pb2.admits_caller])  # pyright: ignore[reportArgumentType]
    named = principals()
    if any(number not in named for number in numbers):
        raise ValueError(f'{method.full_name}: admits_caller names CALLER_UNSPECIFIED, which admits nobody')
    return Rule(principals=frozenset(named[number] for number in numbers))


def rule_for_path(path: str) -> Rule | None:
    """The rule for a call's method path, or ``None`` when no imported contract declares it."""
    try:
        method = descriptor_pool.Default().FindMethodByName(path.strip('/').replace('/', '.'))
    except KeyError:
        return None
    return rule_for(method)
