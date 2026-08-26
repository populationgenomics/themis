"""Weekly gene-disease reference refresh (``python -m themis.services.evidence.gene_disease.refresh``).

Reads ``THEMIS_RESOURCES_BUCKET`` (fail-loud if unset), then fetches the GenCC / ClinGen / PanelApp
upstreams and writes the four reference dumps to that bucket's gene-disease dataset. A Cloud Scheduler
trigger runs the Cloud Run Job weekly (infra).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import httpx

from themis.services.evidence.gene_disease.refresh import job, object_store

_BUCKET_VAR = 'THEMIS_RESOURCES_BUCKET'

# Generous, since the GenCC TSV is a multi-megabyte download; the per-request PanelApp calls are small.
_HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

_logger = logging.getLogger(__name__)


def _resources_bucket() -> str:
    bucket = os.environ.get(_BUCKET_VAR)
    if not bucket:
        raise SystemExit(f'{_BUCKET_VAR} is required: the GCS bucket the refresh job writes the reference dumps to')
    return bucket


async def _run() -> None:
    with contextlib.closing(object_store.GcsReferenceStore(_resources_bucket())) as store:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            report = await job.run(store, client=client)
    for outcome in report.raw_outcomes:
        _logger.info('refreshed %s: %s', outcome.object_name, 'written' if outcome.changed else 'unchanged (304)')
    _logger.info('wrote %s with %d genes', report.panelapp_object, report.panelapp_gene_count)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    asyncio.run(_run())


if __name__ == '__main__':
    main()
