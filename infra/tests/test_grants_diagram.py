"""The committed grants diagrams are what the program renders, and the renderer refuses what it cannot draw honestly.

Two freshness checks, so CI needs no browser: each mermaid source under `docs/design/grants/` equals the
generator's rendering of the captured program, and each SVG beside it names the SHA-256 of the source it was
rendered from.
"""

from __future__ import annotations

import inspect

import pulumi
import pytest

from themis_infra import capture, grants, grants_diagram

_STALE = f'the grants diagrams are stale; run: {grants_diagram.COMMAND}'


@pytest.mark.parametrize('view', grants_diagram.VIEWS, ids=lambda view: view.stem)
def test_committed_source_is_the_rendered_program(program: capture.Capture, view: grants_diagram.View) -> None:
    source = grants_diagram.OUTPUT_DIR / f'{view.stem}.mmd'
    assert source.read_bytes() == grants_diagram.render_view(program, view), _STALE


@pytest.mark.parametrize('view', grants_diagram.VIEWS, ids=lambda view: view.stem)
def test_committed_svg_was_rendered_from_the_committed_source(view: grants_diagram.View) -> None:
    source = (grants_diagram.OUTPUT_DIR / f'{view.stem}.mmd').read_bytes()
    svg = (grants_diagram.OUTPUT_DIR / f'{view.stem}.svg').read_bytes()
    assert grants_diagram.rendered_from(svg) == grants_diagram.source_hash(source), _STALE


def test_every_capability_the_program_grants_is_drawn(program: capture.Capture) -> None:
    grants_diagram.check_drawable(program)


def test_views_partition_the_capabilities() -> None:
    # Every capability class is drawn in exactly one view; a renamed or misspelt class name in a view is
    # otherwise inert, and a class in two views is drawn twice.
    classes = {
        name
        for name, member in inspect.getmembers(grants, inspect.isclass)
        if issubclass(member, pulumi.ComponentResource) and not name.startswith('_')
    }
    drawn = [c for view in grants_diagram.VIEWS for c in view.capabilities]
    assert sorted(drawn) == sorted(classes)


def _capture_with(binding: capture.Binding) -> capture.Capture:
    return capture.Capture(project='p', resources={}, bindings=[binding], workloads=[], frontings=[])


def test_check_drawable_refuses_a_capability_no_view_draws() -> None:
    loose = capture.Binding('urn', 'gcp:x:IAMMember', capability=None, member='allUsers', role='r', target='t')
    with pytest.raises(ValueError, match='no view draws'):
        grants_diagram.check_drawable(_capture_with(loose))


@pytest.mark.parametrize(
    'member',
    [
        'user:someone@example.org',
        'serviceAccount:service-1@gcp-sa-pubsub.iam.gserviceaccount.com',
    ],
)
def test_render_refuses_a_principal_it_cannot_name(member: str) -> None:
    binding = capture.Binding(
        'urn', 'gcp:storage/bucketIAMMember:BucketIAMMember', 'BucketObjectReader', member, 'r', 'p-bucket'
    )
    view = next(v for v in grants_diagram.VIEWS if 'BucketObjectReader' in v.capabilities)
    with pytest.raises(ValueError, match=member.partition(':')[0]):
        grants_diagram.render_view(_capture_with(binding), view)


def test_rendered_from_refuses_an_svg_without_the_comment() -> None:
    with pytest.raises(ValueError, match='source-sha256'):
        grants_diagram.rendered_from(b'<svg></svg>\n')
