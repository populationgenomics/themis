"""Themis infrastructure modules.

The Pulumi program is one `pulumi up` per environment. Its resources are grouped
by concern into modules — `baseline` (enabled GCP services + the shared image
registry), `web` (the Cloud Run web service behind an external HTTPS load
balancer and IAP), and `backend` (the orchestrator backend's runtime identity)
today; database, storage, and audit slot in alongside as they land. The thin
entrypoint (`../__main__.py`) reads stack config and composes them. Every
environment runs the same program against different stack config — see
../README.md.
"""

from __future__ import annotations

from themis_infra import backend, baseline, web

__all__ = ['backend', 'baseline', 'web']
