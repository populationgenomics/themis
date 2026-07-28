"""Where the CLI gets the OAuth client it consents to.

Offline throughout: the `pulumi` subprocess is the only seam that reaches outside, and it is
replaced. What has to hold: the environment overrides stack config half by half, an allowlist
naming more than one client is never guessed at, and every way the read can fail names the one
thing to go and do about it — the causes need different fixes, and the developer only sees the
message.
"""

from __future__ import annotations

import pathlib

import pytest

from themis.clients.iap import __main__ as iap_main
from themis.clients.iap.tests import fixture_pulumi, fixture_token


@pytest.fixture(autouse=True)
def _without_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own exports must not decide what these tests resolve."""
    for name in ('THEMIS_IAP_CLIENT_ID', 'THEMIS_IAP_CLIENT_SECRET', 'PULUMI_STACK'):
        monkeypatch.delenv(name, raising=False)


def test_the_environment_overrides_stack_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # The override exists for a machine that cannot reach Pulumi at all, so a fully-named client
    # must not need it.
    pulumi = fixture_pulumi.answering(monkeypatch)
    monkeypatch.setenv('THEMIS_IAP_CLIENT_ID', 'named.apps.googleusercontent.com')
    monkeypatch.setenv('THEMIS_IAP_CLIENT_SECRET', 'named-secret')

    source = iap_main._client_source('dev')

    assert source.client.client_id == 'named.apps.googleusercontent.com'
    assert source.client.client_secret == 'named-secret'
    assert 'THEMIS_IAP_CLIENT_ID' in source.id_origin
    assert 'THEMIS_IAP_CLIENT_SECRET' in source.secret_origin
    assert not pulumi.calls


@pytest.mark.parametrize(
    ('overridden', 'client_id', 'client_secret', 'read', 'id_origin', 'secret_origin'),
    [
        (
            'THEMIS_IAP_CLIENT_ID',
            'named',
            fixture_token.CLIENT_SECRET,
            [iap_main._SECRET_KEY],
            'THEMIS_IAP_CLIENT_ID',
            'dev',
        ),
        (
            'THEMIS_IAP_CLIENT_SECRET',
            fixture_token.CLIENT_ID,
            'named',
            [iap_main._CLIENTS_KEY],
            'dev',
            'THEMIS_IAP_CLIENT_SECRET',
        ),
    ],
    ids=['id', 'secret'],
)
def test_a_half_the_environment_leaves_unset_comes_from_the_stack(
    monkeypatch: pytest.MonkeyPatch,
    overridden: str,
    client_id: str,
    client_secret: str,
    read: list[str],
    id_origin: str,
    secret_origin: str,
) -> None:
    # Each half is reported as its own: an id left over in a shell outranks the stack that was
    # named, and a run reporting only that stack would hide which client it consented to.
    pulumi = fixture_pulumi.answering(monkeypatch)
    monkeypatch.setenv(overridden, 'named')

    source = iap_main._client_source('dev')

    assert source.client.client_id == client_id
    assert source.client.client_secret == client_secret
    assert pulumi.keys_read() == read
    assert id_origin in source.id_origin
    assert secret_origin in source.secret_origin


def test_an_environment_naming_neither_half_reads_both_from_the_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_pulumi.answering(monkeypatch)

    source = iap_main._client_source('dev')

    assert source.client.client_id == fixture_token.CLIENT_ID
    assert source.client.client_secret == fixture_token.CLIENT_SECRET
    assert 'dev' in source.id_origin
    assert 'dev' in source.secret_origin


def test_the_pulumi_project_is_the_checkouts_not_the_working_directorys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # `login` is run from wherever the developer happens to be; the project it reads config from
    # is this checkout's either way.
    pulumi = fixture_pulumi.answering(monkeypatch)
    monkeypatch.chdir(tmp_path)

    iap_main._client_source('dev')

    assert pulumi.calls
    for argv in pulumi.calls:
        project = pathlib.Path(argv[argv.index('--cwd') + 1])
        assert project.is_absolute()
        assert (project / 'Pulumi.yaml').is_file()


def test_no_read_can_stop_and_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    # A prompt in a non-interactive caller's process waits for input nobody is there to give.
    pulumi = fixture_pulumi.answering(monkeypatch)

    iap_main._client_source('dev')

    assert pulumi.calls
    for argv in pulumi.calls:
        assert '--non-interactive' in argv


@pytest.mark.parametrize(
    ('flag', 'from_env', 'named'),
    [(None, None, 'dev'), (None, 'prod', 'prod'), ('staging', 'prod', 'staging')],
    ids=['default', 'environment', 'flag'],
)
def test_the_stack_named_most_specifically_wins(
    monkeypatch: pytest.MonkeyPatch, flag: str | None, from_env: str | None, named: str
) -> None:
    if from_env is not None:
        monkeypatch.setenv('PULUMI_STACK', from_env)

    assert iap_main._stack(flag) == named


def test_every_read_is_against_the_stack_that_was_named(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two reads against two stacks would pair a client id with another environment's secret.
    pulumi = fixture_pulumi.answering(monkeypatch)

    iap_main._client_source('prod')

    assert pulumi.calls
    for argv in pulumi.calls:
        assert argv[argv.index('--stack') + 1] == 'prod'


def test_an_allowlist_of_several_clients_is_never_guessed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    listed = ['first.apps.googleusercontent.com', 'second.apps.googleusercontent.com']
    fixture_pulumi.answering(monkeypatch, fixture_pulumi.configured(listed))

    with pytest.raises(ValueError, match=iap_main._CLIENTS_KEY) as failure:
        iap_main._client_source('dev')

    message = str(failure.value)
    assert all(client_id in message for client_id in listed)
    assert 'THEMIS_IAP_CLIENT_ID' in message


@pytest.mark.parametrize(
    'raw',
    ['[]', '"one-client"', '[1]', '[""]', 'not-json'],
    ids=['empty', 'not-a-list', 'not-strings', 'blank-entry', 'not-json'],
)
def test_an_unusable_allowlist_fails_loud(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    # Consenting to a client id salvaged from a malformed allowlist yields a token IAP refuses.
    values = fixture_pulumi.configured() | {iap_main._CLIENTS_KEY: raw}
    fixture_pulumi.answering(monkeypatch, values)

    with pytest.raises(ValueError, match=iap_main._CLIENTS_KEY):
        iap_main._client_source('dev')


@pytest.mark.parametrize(
    ('refused', 'stderr', 'remedy'),
    [
        (iap_main._CLIENTS_KEY, fixture_pulumi.NOT_LOGGED_IN, 'not logged into'),
        (iap_main._CLIENTS_KEY, fixture_pulumi.LOGIN_WARNING, 'not logged into'),
        (iap_main._CLIENTS_KEY, fixture_pulumi.NO_SUCH_STACK, 'stack ls'),
        (iap_main._SECRET_KEY, fixture_pulumi.KMS_DENIED, 'PermissionDenied'),
        (iap_main._SECRET_KEY, fixture_pulumi.KMS_ABSENT, 'NotFound'),
    ],
    ids=['not-logged-in', 'logged-out-agent', 'unknown-stack', 'kms-denied', 'kms-absent'],
)
def test_a_refused_read_names_its_remedy(
    monkeypatch: pytest.MonkeyPatch, refused: str, stderr: str, remedy: str
) -> None:
    # Each cause takes a different fix, and one pulumi names no known cause for is handed over
    # verbatim rather than filed under whichever branch its wording happens to brush against —
    # an unreachable KMS key says "not found" while having nothing to do with an unset key.
    fixture_pulumi.answering(monkeypatch, fixture_pulumi.configured() | {refused: fixture_pulumi.Refusal(stderr)})

    with pytest.raises(ValueError, match=remedy) as failure:
        iap_main._client_source('dev')

    assert 'config set' not in str(failure.value)


@pytest.mark.parametrize(
    ('error', 'remedy'),
    [
        (fixture_pulumi.NO_SUCH_STACK, 'stack ls'),
        ("error: configuration key 'x' not found for stack 'dev'", 'config set'),
    ],
    ids=['unknown-stack', 'unset-key'],
)
def test_an_error_outranks_a_login_warning_beside_it(monkeypatch: pytest.MonkeyPatch, error: str, remedy: str) -> None:
    # pulumi prints the login complaint as a warning, so it can sit above an unrelated error. The
    # error is the cause; classifying on the warning would send the developer to `pulumi login`
    # for a stack that simply does not exist.
    stderr = f'{fixture_pulumi.LOGIN_WARNING}\n{error}'
    fixture_pulumi.answering(
        monkeypatch, fixture_pulumi.configured() | {iap_main._CLIENTS_KEY: fixture_pulumi.Refusal(stderr)}
    )

    with pytest.raises(ValueError, match=remedy):
        iap_main._client_source('dev')


@pytest.mark.parametrize(
    ('missing', 'required', 'refused'),
    [
        (iap_main._CLIENTS_KEY, ['--path', f"'{iap_main._CLIENTS_KEY}[0]'"], ['--secret']),
        (iap_main._SECRET_KEY, ['--secret', iap_main._SECRET_KEY], ['--path']),
    ],
    ids=['clients', 'secret'],
)
def test_a_key_the_stack_does_not_carry_names_how_to_set_it(
    monkeypatch: pytest.MonkeyPatch, missing: str, required: list[str], refused: list[str]
) -> None:
    # The remedy has to write the key's own shape. A plain `config set` on the list-valued
    # allowlist stores a bare string, which `config.require_object` then rejects for the whole
    # stack — so following the message would break `pulumi up` rather than fix the read.
    values = {key: value for key, value in fixture_pulumi.configured().items() if key != missing}
    fixture_pulumi.answering(monkeypatch, values)

    with pytest.raises(ValueError, match='config set') as failure:
        iap_main._client_source('dev')

    message = str(failure.value)
    assert all(fragment in message for fragment in required)
    assert not any(fragment in message for fragment in refused)


def test_a_key_the_stack_carries_empty_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty secret would reach Google as an unauthenticated consent, refused for a reason
    # naming the client rather than the config. Setting it back is an encrypted write: the file
    # the remedy edits is tracked and public.
    fixture_pulumi.answering(monkeypatch, fixture_pulumi.configured() | {iap_main._SECRET_KEY: ''})

    with pytest.raises(ValueError, match='empty') as failure:
        iap_main._client_source('dev')

    assert f'config set --secret {iap_main._SECRET_KEY}' in str(failure.value)


@pytest.mark.parametrize(
    'raises',
    [FileNotFoundError('No such file or directory'), PermissionError('Permission denied')],
    ids=['absent', 'not-executable'],
)
def test_a_pulumi_that_cannot_be_run_names_the_install_and_the_override(
    monkeypatch: pytest.MonkeyPatch, raises: OSError
) -> None:
    # Any way the binary fails to start is the developer's to fix, and reaches them as a message
    # rather than the traceback an uncaught OSError would print.
    fixture_pulumi.answering(monkeypatch, raises=raises)

    with pytest.raises(ValueError, match='could not be run') as failure:
        iap_main._client_source('dev')

    assert 'THEMIS_IAP_CLIENT_ID' in str(failure.value)


def test_a_tree_without_the_infra_project_names_the_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Nothing to read config from, so the remedy is the environment, not a pulumi command.
    pulumi = fixture_pulumi.answering(monkeypatch)
    monkeypatch.setattr(iap_main, '_INFRA_DIR', tmp_path / 'infra')

    with pytest.raises(ValueError, match='THEMIS_IAP_CLIENT_ID'):
        iap_main._client_source('dev')

    assert not pulumi.calls
