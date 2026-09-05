"""Draw the program's grants as three graphs, generated from the program.

Run from `infra/`: `uv run --frozen --group infra python -m themis_infra.grants_diagram`. It captures the
program (`capture.py`), writes one mermaid source per view under `docs/design/grants/`, and renders each
to an SVG beside it through mermaid-cli (bun, from `apps/web`) — a maintainer-run step, not CI's. Each SVG
ends with a comment naming the SHA-256 of the source it was rendered from; `infra/tests/test_grants_diagram.py`
fails when a source lags the program or an SVG lags its source.

Each graph has one edge per (holder, capability, target) — a capability's constituent roles collapse
into it — and a principal is one node whether it holds grants or is what another principal impersonates.
Every Cloud Run workload drawn carries a dotted `runs as` edge to its service account, and an IAP
backend a dotted `fronts` edge to the service behind it, so reach can be followed across hops.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import shutil
import subprocess
from typing import NamedTuple

from themis_infra import capture

_REPO = pathlib.Path(__file__).resolve().parents[2]
OUTPUT_DIR = _REPO / 'docs' / 'design' / 'grants'
# bunx from apps/web so its bunfig's release-age gate applies to what it fetches.
_MERMAID_CLI_CWD = _REPO / 'apps' / 'web'
_MERMAID_CLI = '@mermaid-js/mermaid-cli@11.17.0'
COMMAND = 'cd infra && uv run --frozen --group infra python -m themis_infra.grants_diagram'
# SVG text labels rather than HTML ones: an <img> viewer need not render foreignObject.
_INIT = (
    '%%{init: {"layout": "elk", "elk": {"mergeEdges": true, "nodePlacementStrategy": "NETWORK_SIMPLEX"}, '
    '"htmlLabels": false, "flowchart": {"htmlLabels": false, "wrappingWidth": 320}}}%%'
)
_SOURCE_HASH = re.compile(rb'<!-- source-sha256: ([0-9a-f]{64}) -->\n$')


class View(NamedTuple):
    """One graph: the capabilities it draws, and the direction it flows."""

    title: str
    capabilities: frozenset[str]
    direction: str
    """`TB` or `LR`: a graph wider than a doc column is read scrolled down, not across."""

    @property
    def stem(self) -> str:
        """The file stem under `OUTPUT_DIR`: `<stem>.mmd` and `<stem>.svg`."""
        return self.title.lower()


VIEWS = (
    View(
        'Calls',
        frozenset(
            {
                'ServiceInvoker',
                'PublicService',
                'JobRunner',
                'SandboxSpawner',
                'TaskEnqueuer',
                'AccountImpersonator',
                'AccountUser',
                'SelfSigner',
                'IapAccessor',
            }
        ),
        direction='TB',
    ),
    View(
        'Data',
        frozenset(
            {
                'BucketObjectReader',
                'BucketObjectReadWriter',
                'PublicObjectReader',
                'DatabaseConnector',
                'SecretReader',
                'SessionBearerDeriver',
            }
        ),
        direction='LR',
    ),
    View(
        'Platform',
        frozenset({'DeployAccountBuilder', 'DataflowWorker', 'SubnetUser'}),
        direction='LR',
    ),
)

# Google service agents, by the product segment of their `gcp-sa-<product>` domain.
_SERVICE_AGENTS = {'iap': 'IAP', 'cloudtasks': 'Cloud Tasks'}
_DATAFLOW_AGENT_DOMAIN = 'dataflow-service-producer-prod.iam.gserviceaccount.com'
_SERVICE_ACCOUNT_RESOURCE = re.compile(r'^projects/[^/]+/serviceAccounts/(.+)$')

_PRINCIPAL = 'principal'
_WORKLOAD = 'workload'
_RESOURCE = 'resource'
_SHAPES = {_PRINCIPAL: ('["', '"]'), _WORKLOAD: ('[["', '"]]'), _RESOURCE: ('[("', '")]')}


class _Node(NamedTuple):
    kind: str
    label: str


class _Edge(NamedTuple):
    source: _Node
    label: str
    target: _Node
    dotted: bool = False


def _principal(member: str) -> _Node:
    """The node for an IAM member string."""
    if member == 'allUsers':
        return _Node(_PRINCIPAL, 'allUsers')
    kind, _, identity = member.partition(':')
    local, _, domain = identity.partition('@')
    if kind == 'group':
        return _Node(_PRINCIPAL, f'group {local}')
    if kind != 'serviceAccount':
        raise ValueError(f'unrecognised IAM member: {member}')
    if domain == _DATAFLOW_AGENT_DOMAIN:
        return _Node(_PRINCIPAL, 'Dataflow service agent')
    if domain.startswith('gcp-sa-'):
        product = domain.removeprefix('gcp-sa-').split('.')[0]
        if product not in _SERVICE_AGENTS:
            raise ValueError(f'{member}: a Google service agent with no display name in _SERVICE_AGENTS')
        return _Node(_PRINCIPAL, f'{_SERVICE_AGENTS[product]} service agent')
    return _Node(_PRINCIPAL, local)


def _account_target(resource_name: str) -> _Node:
    """The principal node for a service account named as a binding target (`projects/…/serviceAccounts/…`)."""
    match = _SERVICE_ACCOUNT_RESOURCE.match(resource_name)
    if match is None:
        raise ValueError(f'not a service account resource name: {resource_name}')
    return _principal(f'serviceAccount:{match.group(1)}')


def _target(binding: capture.Binding, project: str) -> _Node:
    """The node a binding is over, labelled for the view it appears in."""
    target = binding.target
    match binding.capability:
        case 'ServiceInvoker' | 'PublicService' | 'JobRunner' | 'SandboxSpawner':
            return _Node(_WORKLOAD, target)
        case 'AccountImpersonator' | 'AccountUser' | 'SelfSigner':
            return _account_target(target)
        case 'TaskEnqueuer':
            return _Node(_RESOURCE, f'queue {target}')
        case 'IapAccessor':
            return _Node(_RESOURCE, f'IAP {target}')
        case 'BucketObjectReader' | 'BucketObjectReadWriter' | 'PublicObjectReader':
            return _Node(_RESOURCE, f'bucket {target.removeprefix(f"{project}-")}')
        case 'SecretReader':
            return _Node(_RESOURCE, f'secret {target}')
        case 'SessionBearerDeriver':
            return _Node(_RESOURCE, 'KMS session-token key')
        case 'DatabaseConnector':
            return _Node(_RESOURCE, 'Cloud SQL, project-wide')
        case 'DeployAccountBuilder' | 'DataflowWorker':
            return _Node(_RESOURCE, 'project')
        case 'SubnetUser':
            return _Node(_RESOURCE, f'subnet {target}')
    raise ValueError(f'{binding.capability}: no target rendering for this capability ({binding.urn})')


def _view_edges(program: capture.Capture, capabilities: frozenset[str]) -> set[_Edge]:
    edges = {
        _Edge(_principal(b.member), b.capability, _target(b, program.project))
        for b in program.bindings
        if b.capability in capabilities
    }
    drawn = {node for edge in edges for node in (edge.source, edge.target)}
    for fronting in program.frontings:
        backend = _Node(_RESOURCE, f'IAP {fronting.backend}')
        if backend in drawn:
            edges.add(_Edge(backend, 'fronts', _Node(_WORKLOAD, fronting.service), dotted=True))
    drawn = {node for edge in edges for node in (edge.source, edge.target)}
    for workload in program.workloads:
        node = _Node(_WORKLOAD, workload.name)
        if node in drawn:
            account = _principal(f'serviceAccount:{workload.service_account}')
            edges.add(_Edge(node, 'runs as', account, dotted=True))
    return edges


def _flowchart(edges: set[_Edge], direction: str) -> str:
    nodes = sorted({node for edge in edges for node in (edge.source, edge.target)})
    ids = {node: f'n{i}' for i, node in enumerate(nodes)}
    lines = [f'flowchart {direction}']
    for node in nodes:
        opening, closing = _SHAPES[node.kind]
        lines.append(f'    {ids[node]}{opening}{node.label}{closing}')
    for edge in sorted(edges):
        if edge.dotted:
            lines.append(f'    {ids[edge.source]} -. {edge.label} .-> {ids[edge.target]}')
        else:
            lines.append(f'    {ids[edge.source]} -->|{edge.label}| {ids[edge.target]}')
    return '\n'.join(lines)


def render_view(program: capture.Capture, view: View) -> bytes:
    """The mermaid source for one view, as the bytes written to its `.mmd`."""
    chart = _flowchart(_view_edges(program, view.capabilities), view.direction)
    return f'{_INIT}\n{chart}\n'.encode()


def check_drawable(program: capture.Capture) -> None:
    """Raise unless every capability the program grants has a view that draws it."""
    drawn = frozenset(c for view in VIEWS for c in view.capabilities)
    undrawn = sorted({b.capability or 'none' for b in program.bindings} - drawn)
    if undrawn:
        raise ValueError(f'capabilities no view draws: {undrawn}')


def source_hash(source: bytes) -> str:
    """The SHA-256 hex digest of a `.mmd` file's bytes, as its SVG records it."""
    return hashlib.sha256(source).hexdigest()


def rendered_from(svg: bytes) -> str:
    """The SHA-256 of the source an SVG was rendered from, read from its trailing comment.

    Raises:
        ValueError: If the SVG carries no such comment.
    """
    match = _SOURCE_HASH.search(svg)
    if match is None:
        raise ValueError('the SVG carries no source-sha256 comment; it was not written by this module')
    return match.group(1).decode('ascii')


def _render_svg(source: pathlib.Path, svg: pathlib.Path) -> None:
    bunx = shutil.which('bunx')
    if bunx is None:
        raise SystemExit(
            f'bunx is not on PATH; rendering runs {_MERMAID_CLI} through it (bun.sh) from {_MERMAID_CLI_CWD}'
        )
    command = [bunx, '--bun', _MERMAID_CLI, '-i', str(source), '-o', str(svg), '-b', 'white']
    subprocess.run(command, cwd=_MERMAID_CLI_CWD, check=True)  # noqa: S603 — fixed argv, our own paths
    svg.write_bytes(svg.read_bytes() + f'<!-- source-sha256: {source_hash(source.read_bytes())} -->\n'.encode())


def main() -> None:
    program = capture.capture_program()
    check_drawable(program)
    OUTPUT_DIR.mkdir(exist_ok=True)
    for view in VIEWS:
        source = OUTPUT_DIR / f'{view.stem}.mmd'
        source.write_bytes(render_view(program, view))
        _render_svg(source, OUTPUT_DIR / f'{view.stem}.svg')


if __name__ == '__main__':
    main()
