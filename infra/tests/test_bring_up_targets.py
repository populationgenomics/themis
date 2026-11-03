"""The bring-up's targeted `up`s name resources the program declares; a renamed one would target nothing."""

from __future__ import annotations

import pytest

from themis_infra import capture
from tools import bring_up

_DEV = bring_up.Environment(
    project='cpg-themis-dev', stack='dev', region='australia-southeast1', pulumi_project='themis'
)


def _real(urn: str) -> str:
    # Under mocks a top-level resource's type chain starts with the stack's own type; a real URN omits it.
    return urn.replace('::pulumi:pulumi:Stack$', '::')


def _matches(target: str, urn: str) -> bool:
    # A target ending in `**` names everything under its type chain, the way `pulumi up --target` reads it.
    return urn.startswith(target.removesuffix('**')) if target.endswith('**') else urn == target


@pytest.mark.parametrize('target', [*bring_up.first_targets(_DEV), *bring_up.refresh_targets(_DEV)])
def test_every_target_names_a_resource_the_program_declares(program: capture.Capture, target: str) -> None:
    assert any(_matches(target, _real(urn)) for urn in program.resources), target


def test_the_refresh_the_bring_up_runs_is_the_program_s_refresh_job(program: capture.Capture) -> None:
    jobs = capture.of_type(program, 'gcp:cloudrunv2/job:Job')
    assert jobs
    assert bring_up.REFRESH_JOB in {job.state['name'] for job in jobs}
