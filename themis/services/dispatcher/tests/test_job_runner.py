"""Tests for ``CloudRunJobRunner.spawn`` against a loopback Cloud Run Admin API server."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import typing

import aiohttp
import aiohttp.test_utils
import aiohttp.web
import google.auth.credentials
import google.auth.transport.requests
import pytest

from themis.services.dispatcher import job_runner

_BASELINE_REQUEST = job_runner.SpawnRequest(
    session_id='sid',
    work_id='wid',
    environment_id='eid',
    environment_key='ENV-KEY',
    session_token='SESSION-TOKEN',
)


@dataclasses.dataclass(frozen=True)
class _RecordedCall:
    url: str
    authorization: str | None
    body: dict[str, object]


class _Credentials(google.auth.credentials.Credentials):
    def __init__(
        self,
        *,
        initial: str | None = 'dispatcher-token',
        expired: bool = False,
        refreshed: str | None = 'minted-token',
    ) -> None:
        super().__init__()
        self.token = initial
        if expired:
            # google-auth stores naive UTC on `expiry` and compares against its naive `utcnow()`.
            self.expiry = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(hours=1)
        self._refreshed = refreshed
        self.refreshes = 0

    @typing.override
    def refresh(self, request: google.auth.transport.requests.Request) -> None:
        del request
        self.refreshes += 1
        self.token = self._refreshed
        self.expiry = None


def _spawn_against(
    credentials: _Credentials,
    *,
    request: job_runner.SpawnRequest | None = None,
    project: str = 'proj-1',
    region: str = 'australia-southeast1',
    job: str = 'themis-sandbox',
    status: int = 200,
    spawns: int = 1,
) -> list[_RecordedCall]:
    calls: list[_RecordedCall] = []
    outbound_urls: list[str] = []
    spawn_request = _BASELINE_REQUEST if request is None else request

    async def handle_run(http_request: aiohttp.web.Request) -> aiohttp.web.Response:
        calls.append(
            _RecordedCall(
                url=outbound_urls.pop(0),
                authorization=http_request.headers.get('Authorization'),
                body=await http_request.json(),
            )
        )
        return aiohttp.web.json_response({'name': 'operations/op-1'}, status=status)

    app = aiohttp.web.Application()
    app.router.add_post('/{tail:.*}', handle_run)

    async def drive() -> None:
        async with aiohttp.test_utils.TestServer(app) as server:

            async def reroute(req: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType) -> aiohttp.ClientResponse:
                outbound_urls.append(str(req.url))
                req.url = server.make_url(req.url.raw_path_qs)
                return await handler(req)

            async with aiohttp.ClientSession(middlewares=(reroute,)) as session:
                runner = job_runner.CloudRunJobRunner(
                    session,
                    project=project,
                    region=region,
                    job=job,
                    credentials=credentials,
                )
                for _ in range(spawns):
                    await runner.spawn(spawn_request)

    asyncio.run(drive())
    return calls


def test_spawn_posts_the_worker_container_overrides_to_the_job_run_endpoint() -> None:
    # The worker is a single trusted container (untrusted code runs only inside the postern sandbox),
    # so it receives both the session ids and the credentials in one explicit override (§4).
    credentials = _Credentials(initial='dispatcher-token')
    request = job_runner.SpawnRequest(
        session_id='sess-42',
        work_id='work-99',
        environment_id='env-7',
        environment_key='secret-env-key',
        session_token='derived-session-token',
    )

    [call] = _spawn_against(
        credentials,
        request=request,
        project='themis-dev',
        region='australia-southeast1',
        job='themis-sandbox-worker',
    )

    assert (
        call.url
        == 'https://run.googleapis.com/v2/projects/themis-dev/locations/australia-southeast1/jobs/themis-sandbox-worker:run'
    )
    assert call.authorization == 'Bearer dispatcher-token'
    assert credentials.refreshes == 0
    overrides = call.body['overrides']
    assert isinstance(overrides, dict)
    assert overrides['taskCount'] == 1
    container_overrides = overrides['containerOverrides']
    assert isinstance(container_overrides, list)
    [container] = container_overrides
    assert isinstance(container, dict)
    assert container['name'] == 'worker'
    env_entries = container['env']
    assert isinstance(env_entries, list)
    assert len(env_entries) == 5
    assert {entry['name']: entry['value'] for entry in env_entries if isinstance(entry, dict)} == {
        'ANTHROPIC_SESSION_ID': 'sess-42',
        'ANTHROPIC_WORK_ID': 'work-99',
        'ANTHROPIC_ENVIRONMENT_ID': 'env-7',
        'ANTHROPIC_ENVIRONMENT_KEY': 'secret-env-key',
        'THEMIS_SESSION_TOKEN': 'derived-session-token',
    }


def test_spawn_refreshes_an_expired_credential_once() -> None:
    credentials = _Credentials(initial='stale-token', expired=True, refreshed='fresh-token')

    calls = _spawn_against(credentials, spawns=2)

    assert [call.authorization for call in calls] == ['Bearer fresh-token', 'Bearer fresh-token']
    assert credentials.refreshes == 1


def test_spawn_raises_when_refreshing_credentials_mints_no_token() -> None:
    credentials = _Credentials(initial=None, refreshed=None)
    with pytest.raises(RuntimeError, match='access token'):
        _spawn_against(credentials)


def test_spawn_raises_on_a_non_2xx_response() -> None:
    with pytest.raises(aiohttp.ClientResponseError) as raised:
        _spawn_against(_Credentials(), status=503)
    assert raised.value.status == 503
