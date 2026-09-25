"""`python -m tools.agents`: create or update an agent from its declaration, diff it, or create a skill."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import anthropic
from anthropic.lib import credentials as anthropic_credentials

from tools.agents import config as config_mod
from tools.agents import control_plane


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m tools.agents', description=__doc__)
    subcommands = parser.add_subparsers(dest='command', required=True)
    for name, doc in (
        ('create', 'publish the skills the declaration holds, create the agent it describes, print its id'),
        ('apply', 'publish the skills whose content moved, then update the agent to the declaration, whole'),
        ('diff', 'print a diff from the deployed agent to the declaration (read-only)'),
    ):
        subcommand = subcommands.add_parser(name, help=doc)
        subcommand.add_argument('declaration', type=pathlib.Path, help='an agents/<name>.agent.yaml')
        if name in ('apply', 'diff'):
            subcommand.add_argument('--agent-id', required=True, help='the deployed agent (themis:anthropicAgentId)')
    create_skill = subcommands.add_parser(
        'create-skill', help='create a skill from a directory, its content the first version, and print its id'
    )
    create_skill.add_argument('directory', type=pathlib.Path, help='an agents/skills/<directory>')
    return parser


def _client() -> anthropic.Anthropic:
    """The SDK client on the credentials it resolves; none resolved is a failure here, not at the first request."""
    client = anthropic.Anthropic()
    if client.api_key is None and client.auth_token is None and client.credentials is None:
        raise SystemExit(
            'no Anthropic credentials resolved: `ant auth login` on a laptop, or the federation variables in CI'
        )
    return client


def _model_id() -> str:
    """The model id the declaration is applied with, read from the environment.

    Its source is the stack's encrypted config, read by hand or exported to the deploy.
    """
    model_id = os.environ.get(config_mod.MODEL_ID_ENV, '')
    if not model_id.strip():
        raise SystemExit(
            f'{config_mod.MODEL_ID_ENV} is unset: the model id is confidential stack config — '
            '`pulumi config get --stack dev themis:anthropicAgentModelId` by hand, the stack output in the deploy'
        )
    return model_id


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, 'agent_id', None) == '':
        raise SystemExit('--agent-id is empty; the stack output or config it came from is unset')
    try:
        client = _client()
        if args.command == 'create-skill':
            print(control_plane.create_skill(args.directory, client))
            return 0
        config = config_mod.load(args.declaration)
        model_id = _model_id()
        if args.command == 'create':
            print(control_plane.create(config, model_id, client).id)
        elif args.command == 'apply':
            applied = control_plane.apply(config, model_id, client, args.agent_id)
            for item in applied.published:
                print(f'published agents/skills/{item.directory} as version {item.version}')
            if applied.after == applied.before:
                print(f'{applied.agent_id}: unchanged at v{applied.before}')
            else:
                print(f'{applied.agent_id}: v{applied.before} -> v{applied.after}')
        else:
            report = control_plane.diff(config, model_id, client, args.agent_id)
            sys.stdout.write(report or 'no difference over the declared fields\n')
    except (config_mod.ConfigError, control_plane.ControlPlaneError) as exc:
        raise SystemExit(str(exc)) from exc
    except anthropic_credentials.WorkloadIdentityError as exc:
        rule = os.environ.get('ANTHROPIC_FEDERATION_RULE_ID')
        account = os.environ.get('ANTHROPIC_SERVICE_ACCOUNT_ID')
        raise SystemExit(
            f'Anthropic token exchange under rule {rule!r} for service account {account!r}: {exc}'
        ) from exc
    except anthropic.AnthropicError as exc:
        raise SystemExit(f'Anthropic API: {exc}') from exc
    return 0


if __name__ == '__main__':
    sys.exit(main())
