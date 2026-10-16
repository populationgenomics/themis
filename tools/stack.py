"""The config of the Pulumi stack deployed in a GCP project, read from its committed stack file.

Each environment is one `infra/Pulumi.<stack>.yaml` whose `gcp:project` names the project it deploys
to. A laptop tool that needs a value the stack states (the app's domain, the Anthropic workspace)
reads it here rather than taking it as a second, hand-typed copy.
"""

from __future__ import annotations

import pathlib

import yaml

_INFRA_DIR = pathlib.Path(__file__).resolve().parents[1] / 'infra'


def config(project: str, *, infra_dir: pathlib.Path = _INFRA_DIR) -> dict[str, object]:
    """The `config` mapping of the one stack file whose `gcp:project` is `project`.

    Raises:
        ValueError: If no stack file, or more than one, names `project`.
    """
    found: list[tuple[pathlib.Path, dict[str, object]]] = []
    for path in sorted(infra_dir.glob('Pulumi.*.yaml')):
        document = yaml.safe_load(path.read_text('utf-8'))
        stack_config = document.get('config') if isinstance(document, dict) else None
        if isinstance(stack_config, dict) and stack_config.get('gcp:project') == project:
            found.append((path, stack_config))
    if len(found) != 1:
        named = [str(path) for path, _ in found]
        raise ValueError(f'expected one Pulumi stack file in {infra_dir} with gcp:project {project!r}, found {named}')
    return found[0][1]


def value(project: str, key: str, *, infra_dir: pathlib.Path = _INFRA_DIR) -> str:
    """The non-empty string `key` the stack deployed in `project` sets.

    Raises:
        ValueError: If no single stack names `project`, or it sets no such string.
    """
    stated = config(project, infra_dir=infra_dir).get(key)
    if not isinstance(stated, str) or not stated.strip():
        raise ValueError(f'the stack deployed in {project} sets no {key}')
    return stated
