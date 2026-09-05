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

import importlib.resources
import inspect
import pathlib
import re

import pulumi

from themis_infra import capture, grants

# Provider types with `iam` in the name that are not grants.
_NOT_A_BINDING = re.compile(r'(CustomRole|AuditConfig|MemberRemove)$')
_TYPE_TOKEN = re.compile(r'@pulumi\.type_token\("(gcp:[^"]+)"\)')


def _grant_classes() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(grants, inspect.isclass)
        if issubclass(member, pulumi.ComponentResource) and not name.startswith('_')
    }


def test_every_binding_is_a_capability(program: capture.Capture) -> None:
    assert program.bindings, 'the program registered no IAM binding; the mocks are not running it'
    grant_classes = _grant_classes()
    loose = [b.urn for b in program.bindings if b.capability not in grant_classes]
    assert not loose, 'IAM bindings outside a grants.* capability:\n' + '\n'.join(loose)


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
