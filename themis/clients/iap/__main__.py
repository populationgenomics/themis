"""``python -m themis.clients.iap`` — consent once, then print a token per run.

``login`` reads the environment's Desktop OAuth client out of its Pulumi stack config, runs the
browser consent, and immediately spends the result once, so a grant that cannot be replayed is
caught now rather than on the next run. It reaches only Google — whether IAP admits the client is
settled by the first real request. ``token`` prints a fresh ID token for an ad-hoc call:

    curl -H "Authorization: Bearer $(python -m themis.clients.iap token)" https://HOST/api/projects
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Sequence

from google.auth.transport import requests as google_auth_requests

from themis.clients.iap import credentials

_INFRA_DIR = pathlib.Path(__file__).resolve().parents[3] / 'infra'

_CLIENTS_KEY = 'themis:iapProgrammaticClients'
_SECRET_KEY = 'themis:iapProgrammaticClientSecret'  # noqa: S105 — a config key name, not a secret

_CLIENT_ID_ENV = 'THEMIS_IAP_CLIENT_ID'
_CLIENT_SECRET_ENV = 'THEMIS_IAP_CLIENT_SECRET'  # noqa: S105 — an env var name, not a secret
_STACK_ENV = 'PULUMI_STACK'
_DEFAULT_STACK = 'dev'


@dataclasses.dataclass(frozen=True)
class _ClientSource:
    """The client to consent to, plus a phrase naming where each half was read from."""

    client: credentials.OAuthClient
    id_origin: str
    secret_origin: str


def _pulumi(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one ``pulumi`` subcommand against the repo's infra project."""
    return subprocess.run(  # noqa: S603 — no shell; every argument is a literal, a stack, or a key
        ['pulumi', '--non-interactive', '--cwd', str(_INFRA_DIR), *args],  # noqa: S607 — found on PATH
        capture_output=True,
        text=True,
        check=False,
    )


def _setter(stack: str, key: str) -> str:
    """The ``pulumi config set`` that puts a value on ``stack``.

    Shaped per key: the secret is a scalar, written encrypted; the allowlist is a list, so it
    takes an indexed ``--path`` write. A plain ``config set`` on the list key stores a bare
    string, which `config.require_object` then rejects for the whole stack, not just this tool.
    """
    if key == _SECRET_KEY:
        return f'pulumi -C {_INFRA_DIR} -s {stack} config set --secret {key} <value>'
    return f"pulumi -C {_INFRA_DIR} -s {stack} config set --path '{key}[0]' <client-id>"


def _refusal(stack: str, key: str, stderr: str) -> str:
    """Name the one thing to go and do about a refused ``pulumi config get``.

    Each cause is matched on the phrase pulumi prints for it alone, and the causes it reports as
    an error are matched before the one it can report as a mere warning alongside them.

    Args:
        stack: The stack the read was against.
        key: The config key that was read.
        stderr: What pulumi wrote to stderr.

    Returns:
        The message to raise: a remedy for a cause pulumi names, else its own text.
    """
    if f"no stack named '{stack}' found" in stderr:
        return (
            f'the backend you are logged into holds no stack named {stack!r} (`pulumi -C {_INFRA_DIR} stack ls` lists '
            f'the ones it does). Name another with --stack or ${_STACK_ENV}, or `pulumi login` to the backend that '
            'holds it.'
        )
    # pulumi drops the project namespace from the key it echoes, so only the wrapping text matches.
    if 'configuration key ' in stderr and 'not found for stack' in stderr:
        return (
            f'{key} is not set on stack {stack}, so that environment declares no programmatic OAuth client: '
            f'`{_setter(stack, key)}` (docs/runbooks/iap-access.md).'
        )
    if 'PULUMI_ACCESS_TOKEN must be set' in stderr or 'requires logging in' in stderr:
        return (
            f'not logged into the Pulumi backend that holds the {stack} stack, so {key} cannot be read: run '
            f'`pulumi login gs://cpg-themis-{stack}-pulumi-state`.'
        )
    return f'`pulumi config get {key}` failed for stack {stack}: {stderr}'


def _stack_config(stack: str, key: str) -> str:
    """Read one key out of the infra project's stack config.

    A secret key is decrypted through the stack's KMS key, so this succeeds only for someone the
    environment's IAM admits — the line the programmatic client is scoped to.

    Args:
        stack: The Pulumi stack (an environment) to read.
        key: The namespaced config key, e.g. ``themis:iapProgrammaticClients``.

    Returns:
        The value as ``pulumi config get`` prints it — a scalar verbatim, a structured value as
        JSON.

    Raises:
        ValueError: If the CLI cannot be run, the tree carries no ``infra/``, the caller is not
            logged into the backend, the stack is unknown, or the key is unset or empty. Each
            message names the fix for its own cause.
    """
    if not _INFRA_DIR.is_dir():
        raise ValueError(
            f'{_INFRA_DIR} does not exist, so {key} cannot be read from stack config. Run this from a checkout, '
            f'or name the client yourself with {_CLIENT_ID_ENV} and {_CLIENT_SECRET_ENV}.'
        )
    try:
        completed = _pulumi(['config', 'get', key, '--stack', stack])
    except OSError as e:
        raise ValueError(
            f'`pulumi` could not be run ({e}), so {key} cannot be read from the {stack} stack. Install it '
            f'(https://www.pulumi.com/docs/install/), or name the client yourself with {_CLIENT_ID_ENV} and '
            f'{_CLIENT_SECRET_ENV}.'
        ) from e
    if completed.returncode != 0:
        raise ValueError(_refusal(stack, key, completed.stderr.strip()))
    value = completed.stdout.strip()
    if not value:
        raise ValueError(f'{key} is empty on stack {stack}; set it with `{_setter(stack, key)}`.')
    return value


def _sole_client_id(stack: str) -> str:
    """The single OAuth client id ``stack`` allowlists for programmatic access.

    Args:
        stack: The Pulumi stack to read the allowlist from.

    Returns:
        The one client id on ``themis:iapProgrammaticClients``.

    Raises:
        ValueError: If the allowlist is malformed, empty, or names more than one client. Consent
            is to one client; choosing among several would silently consent to the wrong one.
    """
    raw = _stack_config(stack, _CLIENTS_KEY)
    try:
        listed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f'{_CLIENTS_KEY} on stack {stack} is not the JSON list of client ids it must be: {raw}') from e
    if not isinstance(listed, list) or not all(isinstance(entry, str) and entry for entry in listed):
        raise ValueError(f'{_CLIENTS_KEY} on stack {stack} is not a list of client ids: {raw}')
    if not listed:
        raise ValueError(
            f'{_CLIENTS_KEY} is empty on stack {stack}, so that environment allowlists no programmatic client and '
            'IAP would refuse every token minted against it. Declare one and `pulumi up` '
            '(docs/runbooks/iap-access.md).'
        )
    if len(listed) > 1:
        raise ValueError(
            f'{_CLIENTS_KEY} on stack {stack} allowlists {len(listed)} clients ({", ".join(listed)}), so which one '
            f'to consent to is not ours to guess: name it with {_CLIENT_ID_ENV}=<id>.'
        )
    return listed[0]


def _client_source(stack: str) -> _ClientSource:
    """The Desktop OAuth client to consent to: the environment first, then the stack's config.

    Each half resolves on its own, so a machine that cannot reach Pulumi can carry either or both
    in its environment; the stack is read only for a half the environment does not name.

    Args:
        stack: The Pulumi stack to read whatever the environment leaves unset.

    Returns:
        The client, and where each half came from — a consent against the wrong environment is
        otherwise indistinguishable until IAP refuses the token much later, and an id left over
        in the environment silently outranks the stack that was named.

    Raises:
        ValueError: If a half is neither in the environment nor readable from the stack. A
            missing half is never defaulted: the unauthenticated request it would produce comes
            back as an IAP refusal that blames the wrong thing.
    """
    client_id = os.environ.get(_CLIENT_ID_ENV)
    id_origin = f'${_CLIENT_ID_ENV}'
    if not client_id:
        client_id = _sole_client_id(stack)
        id_origin = f'the {stack} stack'
    client_secret = os.environ.get(_CLIENT_SECRET_ENV)
    secret_origin = f'${_CLIENT_SECRET_ENV}'
    if not client_secret:
        client_secret = _stack_config(stack, _SECRET_KEY)
        secret_origin = f'the {stack} stack'
    return _ClientSource(
        client=credentials.OAuthClient(client_id=client_id, client_secret=client_secret),
        id_origin=id_origin,
        secret_origin=secret_origin,
    )


def _stack(flag: str | None) -> str:
    """The stack to read config from: ``--stack``, else ``$PULUMI_STACK``, else ``dev``."""
    if flag:
        return flag
    from_env = os.environ.get(_STACK_ENV)
    if from_env:
        return from_env
    return _DEFAULT_STACK


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog='python -m themis.clients.iap', description=__doc__)
    parser.add_argument(
        '--cache',
        type=pathlib.Path,
        default=None,
        help='credential cache file (default: $THEMIS_IAP_CREDENTIALS, else $XDG_CONFIG_HOME/themis/iap.json)',
    )
    parser.add_argument(
        '--stack',
        default=None,
        help=f'Pulumi stack the OAuth client is read from (default: ${_STACK_ENV}, else {_DEFAULT_STACK})',
    )
    parser.add_argument('command', choices=('login', 'token'))
    args = parser.parse_args(argv)

    if args.stack is not None and not args.stack:
        parser.error('--stack needs the name of a stack')
    if args.command == 'token' and args.stack is not None:
        parser.error(
            '--stack selects the environment `login` consents to; `token` spends whatever consent --cache holds'
        )

    path = args.cache if args.cache is not None else credentials.default_cache_path()
    request = google_auth_requests.Request()

    # Every error below carries its remedy in the message, so a traceback would only bury it.
    try:
        if args.command == 'login':
            source = _client_source(_stack(args.stack))
            print(
                f'consenting to OAuth client {source.client.client_id} (id from {source.id_origin}, '
                f'secret from {source.secret_origin})',
                file=sys.stderr,
            )
            stored = credentials.authorize(source.client, path)
            token = credentials.mint(stored, request)
            print(f'consented as {token.email}; cached in {path}', file=sys.stderr)
            return
        print(credentials.mint(credentials.load(path), request).value)
    except (credentials.ConsentRequiredError, credentials.ConsentRejectedError, ValueError) as e:
        raise SystemExit(str(e)) from e
    except OSError as e:
        # On `login` this lands after the consent is already spent, so the remedy has to be
        # enough to get the cache written on the next run rather than send anyone hunting.
        raise SystemExit(
            f'the consent cache at {path} could not be read or written ({e}). Point --cache or '
            '$THEMIS_IAP_CREDENTIALS somewhere writable, or fix that path, and run this again.'
        ) from e


if __name__ == '__main__':
    main()
