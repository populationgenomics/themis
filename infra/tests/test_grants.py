"""Every IAM binding the program registers is a child of a `grants` capability.

The program runs once under Pulumi's mocks (`themis_infra.capture`), and every registered resource whose
type is an IAM binding must sit directly under a `themis:grants:*` component that `grants.py` defines. A
binding constructed anywhere else — a bare `gcp.*IamMember` in a service module — fails here.

"IAM binding" is every provider resource type with `iam` in its name other than a role definition, an
audit-log config, or a member removal; a provider bump that adds a shape the pattern misses fails the
pattern test. Object and bucket ACL resources are outside it: every bucket here enforces uniform
bucket-level access, under which ACLs are inert.
"""

from __future__ import annotations

import collections
import importlib.resources
import pathlib
import re

import pytest

from themis_infra import capture

# Provider types with `iam` in the name that are not grants.
_NOT_A_BINDING = re.compile(r'(CustomRole|AuditConfig|MemberRemove)$')
_TYPE_TOKEN = re.compile(r'@pulumi\.type_token\("(gcp:[^"]+)"\)')


def test_every_binding_is_a_capability(program: capture.Capture) -> None:
    capture.check_bindings_are_capabilities(program)


def test_a_binding_outside_a_capability_is_refused(program: capture.Capture) -> None:
    loose = capture.Binding(
        urn='urn:pulumi:dev::themis::gcp:projects/iAMMember:IAMMember::loose',
        type_='gcp:projects/iAMMember:IAMMember',
        capability=None,
        member='serviceAccount:loose@p.iam.gserviceaccount.com',
        role='roles/viewer',
        target='p',
    )
    with pytest.raises(ValueError, match='::loose'):
        capture.check_bindings_are_capabilities(program._replace(bindings=[*program.bindings, loose]))


def test_no_two_bindings_grant_one_role_to_one_member_on_one_target(program: capture.Capture) -> None:
    # A member binding is not authoritative: of two granting the same thing, deleting either revokes it while
    # the other still records it as held. Two capabilities sharing a role (`serviceUsageConsumer`) cannot both
    # go to one holder.
    grants_of = collections.Counter((b.type_, b.target, b.role, b.member) for b in program.bindings)
    duplicated = sorted(grant for grant, count in grants_of.items() if count > 1)
    assert not duplicated, 'one grant made by several bindings:\n' + '\n'.join(map(str, duplicated))


def _provider_type_tokens() -> set[str]:
    """Every resource type token the installed GCP provider declares."""
    tokens: set[str] = set()
    package = importlib.resources.files('pulumi_gcp')
    assert isinstance(package, pathlib.Path)  # an installed package, not a zip
    for source in package.rglob('*.py'):
        text = source.read_text('utf-8')
        if 'type_token(' in text:
            tokens.update(_TYPE_TOKEN.findall(text))
    return tokens


def test_binding_type_pattern_covers_every_iam_shape_the_provider_offers() -> None:
    # The pattern, not the program, decides whether a binding counts, so it is checked against the provider.
    iam_tokens = {token for token in _provider_type_tokens() if 'iam' in token.rsplit(':', 1)[-1].lower()}
    assert iam_tokens
    missed = sorted(t for t in iam_tokens if not capture.BINDING_TYPE.match(t) and not _NOT_A_BINDING.search(t))
    assert not missed, f'IAM resource types the binding pattern misses: {missed}'
    assert not any(capture.BINDING_TYPE.match(t) and _NOT_A_BINDING.search(t) for t in iam_tokens)
