"""Dispatcher entrypoint: an HMAC-verified webhook that drains the work queue and spawns sandboxes.

On ``session.status_run_started``, verify the signature, then drain: poll (no ack), force-stop
non-session items, derive each session's token, and trigger one sandbox Job execution. All backends
are real (Anthropic API / KMS / Cloud Run Admin API), configured from the environment and fail-loud on
a missing value; the offline fixtures live beside the code for the orchestration tests. ``PORT`` is
the Cloud Run convention.
"""

from __future__ import annotations

import logging
import os

import aiohttp
import google.auth
from aiohttp import web

from themis.clients.auth import derive as derive_mod
from themis.clients.work_queue import client as work_queue_mod
from themis.services.dispatcher import handler as handler_mod
from themis.services.dispatcher import job_runner as job_runner_mod

_DEFAULT_BASE_URL = 'https://api.anthropic.com'
_CLOUD_PLATFORM_SCOPE = 'https://www.googleapis.com/auth/cloud-platform'

_DISPATCHER: web.AppKey[handler_mod.Dispatcher] = web.AppKey('dispatcher', handler_mod.Dispatcher)
_SESSION: web.AppKey[aiohttp.ClientSession] = web.AppKey('session', aiohttp.ClientSession)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'required environment variable {name} is unset or empty')
    return value


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text='ok')


async def _handle_webhook(request: web.Request) -> web.Response:
    status = await handler_mod.process_delivery(request.app[_DISPATCHER], request.headers, await request.read())
    return web.Response(status=status)


async def _on_startup(app: web.Application) -> None:
    session = aiohttp.ClientSession()
    app[_SESSION] = session
    environment_id = _require('ANTHROPIC_ENVIRONMENT_ID')
    environment_key = _require('ANTHROPIC_ENVIRONMENT_KEY')
    credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    app[_DISPATCHER] = handler_mod.Dispatcher(
        work_queue=work_queue_mod.AnthropicWorkQueue(
            session,
            base_url=os.environ.get('ANTHROPIC_BASE_URL', _DEFAULT_BASE_URL),
            environment_id=environment_id,
            environment_key=environment_key,
        ),
        deriver=derive_mod.kms_deriver(_require('THEMIS_SESSION_TOKEN_KEY_VERSION')),
        job_runner=job_runner_mod.CloudRunJobRunner(
            session,
            project=_require('THEMIS_SANDBOX_JOB_PROJECT'),
            region=_require('THEMIS_SANDBOX_JOB_REGION'),
            job=_require('THEMIS_SANDBOX_JOB_NAME'),
            credentials=credentials,
        ),
        signing_key=_require('ANTHROPIC_WEBHOOK_SIGNING_KEY'),
        environment_id=environment_id,
        environment_key=environment_key,
        reclaim_older_than_ms=int(_require('THEMIS_RECLAIM_OLDER_THAN_MS')),
    )


async def _on_cleanup(app: web.Application) -> None:
    await app[_SESSION].close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get('/healthz', _healthz)
    app.router.add_post('/webhooks/anthropic', _handle_webhook)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    web.run_app(build_app(), port=int(os.environ.get('PORT', '8080')))


if __name__ == '__main__':
    main()
