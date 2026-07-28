"""An offline stand-in for the ``pulumi`` CLI.

The CLI reads the environment's OAuth client out of Pulumi stack config, so the subprocess call
is the one seam these tests replace: the fixture answers a ``config get`` per key, from a value
or from the stderr pulumi refuses with. The refusals are pulumi v3.250's own, captured from it,
since the classification they drive is the code under test.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from collections.abc import Mapping, Sequence

import pytest

from themis.clients.iap import __main__ as iap_main
from themis.clients.iap.tests import fixture_token

NOT_LOGGED_IN = 'error: PULUMI_ACCESS_TOKEN must be set for login during non-interactive CLI sessions'
# What a logged-out run prints instead when pulumi auto-provisions an ephemeral agent account:
# the same cause, reported as a warning, so it can also accompany an unrelated error.
LOGIN_WARNING = (
    'warning: failed to get user account details: this command requires logging in; try running `pulumi login` first'
)
NO_SUCH_STACK = "error: no stack named '{stack}' found"
KMS_DENIED = 'error: rpc error: code = PermissionDenied desc = cloudkms.cryptoKeyVersions.useToDecrypt denied'
# A KMS key the stack cannot reach says "not found" too, which is why the unset-key branch keys
# off the whole phrase pulumi wraps a config key in.
KMS_ABSENT = (
    'error: could not create secrets manager: secrets (code=NotFound): rpc error: code = NotFound desc = CryptoKey '
    'projects/cpg-themis-dev/locations/australia-southeast1/keyRings/themis/cryptoKeys/pulumi not found.'
)


@dataclasses.dataclass(frozen=True)
class Refusal:
    """A key pulumi refuses to answer for, and what it writes to stderr when asked."""

    stderr: str


def configured(client_ids: Sequence[str] = (fixture_token.CLIENT_ID,)) -> dict[str, str | Refusal]:
    """The stack config of an environment allowlisting ``client_ids``, as pulumi prints it."""
    return {
        iap_main._CLIENTS_KEY: json.dumps(list(client_ids)),
        iap_main._SECRET_KEY: fixture_token.CLIENT_SECRET,
    }


class FixturePulumi:
    """Answers ``pulumi config get`` from a mapping, recording every invocation whole.

    A key the mapping does not hold is refused the way pulumi refuses an unset one — including
    its habit of dropping the project namespace from the key it echoes — so a test spells out
    only the refusals that arise for other reasons.
    """

    def __init__(self, values: Mapping[str, str | Refusal], raises: Exception | None = None) -> None:
        self.values = values
        self.raises = raises
        self.calls: list[list[str]] = []

    def run(self, args: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        # A fixture blind to these would keep passing after the real call stopped decoding its
        # output, or started raising instead of returning a refusal to classify.
        assert kwargs['capture_output']
        assert kwargs['text']
        assert not kwargs['check']
        argv = list(args)
        self.calls.append(argv)
        if self.raises is not None:
            raise self.raises
        key = argv[argv.index('get') + 1]
        stack = argv[argv.index('--stack') + 1]
        answer = self.values.get(key)
        if isinstance(answer, Refusal):
            return subprocess.CompletedProcess(argv, 1, '', answer.stderr.format(stack=stack))
        if answer is None:
            echoed = key.split(':')[-1]
            return subprocess.CompletedProcess(
                argv, 1, '', f"error: configuration key '{echoed}' not found for stack '{stack}'"
            )
        return subprocess.CompletedProcess(argv, 0, f'{answer}\n', '')

    def keys_read(self) -> list[str]:
        """The config keys asked for, in the order they were asked for."""
        return [argv[argv.index('get') + 1] for argv in self.calls]


def answering(
    monkeypatch: pytest.MonkeyPatch,
    values: Mapping[str, str | Refusal] | None = None,
    raises: Exception | None = None,
) -> FixturePulumi:
    """Replace the CLI's subprocess call with a fixture pulumi, and hand it back."""
    fixture = FixturePulumi(configured() if values is None else values, raises=raises)
    monkeypatch.setattr(iap_main.subprocess, 'run', fixture.run)
    return fixture
