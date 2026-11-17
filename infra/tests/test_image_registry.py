"""Invariants on the Artifact Registry image repository's cleanup policy."""

from __future__ import annotations

import re

from themis_infra import baseline, capture
from tools import image_bake

_REPOSITORY_TYPE = 'gcp:artifactregistry/repository:Repository'
_NORMALIZED_SECONDS_RE = re.compile(r'^[1-9][0-9]*s$')


def test_build_cache_prefix_matches_image_bake() -> None:
    assert baseline.BUILD_CACHE_PACKAGE_PREFIX == image_bake.CACHE_PACKAGE_PREFIX


def test_image_registry_deletes_only_untagged_build_cache_versions(program: capture.Capture) -> None:
    # Deployed commit images live under `<name>:<sha>`, and `docker buildx` exports multi-manifest indexes whose
    # child manifests carry no tag of their own; scoping DELETE policies to `UNTAGGED` under `cache-*` alone keeps
    # application image packages untouched.
    (repo,) = capture.of_type(program, _REPOSITORY_TYPE)
    assert repo.state.get('cleanupPolicyDryRun') is False
    policies = repo.state.get('cleanupPolicies')
    assert isinstance(policies, list), policies
    assert policies, 'image registry declares no cleanup policies'

    delete_policies: list[dict[str, object]] = []
    for policy in policies:
        assert isinstance(policy, dict), policy
        assert policy.get('action') in {'DELETE', 'KEEP'}, policy
        if policy.get('action') == 'DELETE':
            delete_policies.append(policy)

    assert delete_policies, policies
    for policy in delete_policies:
        condition = policy.get('condition')
        assert isinstance(condition, dict), policy
        assert condition.get('tagState') == 'UNTAGGED', policy
        prefixes = condition.get('packageNamePrefixes')
        assert prefixes == [baseline.BUILD_CACHE_PACKAGE_PREFIX], policy
        assert _NORMALIZED_SECONDS_RE.fullmatch(str(condition.get('olderThan'))), policy
