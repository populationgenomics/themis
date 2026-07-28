"""An offline stand-in for the deployed environment.

Mounted as a ``requests`` transport adapter rather than replacing the session, so the request
under assertion is the one ``requests`` actually built from the client's arguments. Kept out of
the production module so it ships no test-only surface.
"""

from __future__ import annotations

import requests
from requests import adapters, models


class FixtureAdapter(adapters.BaseAdapter):
    """Answers every request with one canned status, recording what was sent."""

    def __init__(self, status: int, body: str = '') -> None:
        super().__init__()
        self.status = status
        self.body = body
        self.calls: list[models.PreparedRequest] = []
        self.timeouts: list[float | tuple[float | None, float | None] | None] = []

    def send(
        self,
        request: models.PreparedRequest,
        stream: bool = False,
        timeout: float | tuple[float | None, float | None] | None = None,
        verify: bool | str = True,
        cert: str | tuple[str, str] | None = None,
        proxies: dict[str, str] | None = None,
    ) -> requests.Response:
        self.calls.append(request)
        self.timeouts.append(timeout)
        response = requests.Response()
        response.status_code = self.status
        response._content = self.body.encode('utf-8')
        response.url = request.url or ''
        response.request = request
        if 300 <= self.status < 400:
            response.headers['Location'] = 'https://accounts.google.com/signin'
        return response

    def close(self) -> None:
        return


def session(status: int, body: str = '') -> tuple[requests.Session, FixtureAdapter]:
    """A session answering any https request with ``status``, plus the adapter that recorded it."""
    adapter = FixtureAdapter(status, body)
    configured = requests.Session()
    configured.mount('https://', adapter)
    return configured, adapter
