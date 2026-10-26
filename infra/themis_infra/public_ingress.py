"""The named exceptions to the rule that ingress from the public internet goes through IAP.

Anything reachable from the internet sits behind IAP, unless it is one of the entries here, each of which
carries its own compensating controls (docs/design/security.md, "Ingress from the public internet").
`infra/tests/test_public_ingress.py` holds the program to the rule and to each entry's controls; an entry
is added here, beside its section of security.md, never by loosening the test.
"""

from __future__ import annotations

import enum
from typing import NamedTuple

_SECURITY_DOC = 'docs/design/security.md'


class Kind(enum.Enum):
    """What kind of resource an exception names, and so which controls the test holds it to."""

    CLOUD_RUN_SERVICE = 'Cloud Run service'
    """Public invoker, reached only through a load-balancer backend whose Cloud Armor policy denies by default."""
    BUCKET = 'bucket'
    """Public object reads by name, with no listing."""
    SQL_INSTANCE = 'Cloud SQL instance'
    """A public IP with no authorized networks, taking connections from the Cloud SQL connectors alone."""


class PublicIngressException(NamedTuple):
    """One resource the internet reaches without IAP."""

    kind: Kind
    name: str
    """The resource's name as the program sets it; a bucket's without the `<project>-` prefix every bucket carries."""
    rationale: str
    """Where security.md argues the exception and its controls."""


EXCEPTIONS: tuple[PublicIngressException, ...] = (
    PublicIngressException(
        Kind.CLOUD_RUN_SERVICE,
        'themis-dispatcher',
        f'{_SECURITY_DOC}#the-sandbox-dispatchers-webhook',
    ),
    PublicIngressException(Kind.BUCKET, 'pr-screenshots', f'{_SECURITY_DOC}#the-pr-screenshots-bucket'),
    PublicIngressException(Kind.SQL_INSTANCE, 'themis-sql', f'{_SECURITY_DOC}#cloud-sqls-public-ip'),
)


# The Cloud Run services that carry IAP on themselves, for the people who sign in to them in a browser; who that IAP
# admits is the service's own decision. Every other IAP gate is an operator backend, admitting the operator account
# alone.
SIGN_IN_SERVICES = frozenset({'themis-web'})


def names(kind: Kind) -> frozenset[str]:
    """The names of the exceptions of one kind."""
    return frozenset(exception.name for exception in EXCEPTIONS if exception.kind is kind)
