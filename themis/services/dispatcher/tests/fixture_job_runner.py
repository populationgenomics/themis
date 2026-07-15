"""Job-runner test double: records the spawns requested, for the dispatcher's orchestration tests.

Kept out of ``job_runner`` so the production module ships no test-only code.
"""

from __future__ import annotations

from themis.services.dispatcher import job_runner


class FixtureJobRunner(job_runner.JobRunner):
    """Records the spawns requested, for the dispatcher's orchestration tests."""

    def __init__(self) -> None:
        self.spawns: list[job_runner.SpawnRequest] = []

    async def spawn(self, request: job_runner.SpawnRequest) -> None:
        self.spawns.append(request)
