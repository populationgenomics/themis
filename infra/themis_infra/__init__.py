"""Themis infrastructure modules.

The Pulumi program is one `pulumi up` per environment. Its resources are grouped
by concern into modules — `baseline` (enabled GCP services + the shared image
registry), `web` (the Cloud Run web service behind an external HTTPS load
balancer and IAP — its runtime SA is also the Managed-Agents client identity),
`storage` (the literature full-text store bucket), and `sql` (the Cloud SQL
Postgres instance with IAM database auth) today; audit slots in alongside as it
lands. The thin entrypoint (`../__main__.py`) reads stack config and composes
them. Every environment runs the same program against different stack config —
see ../README.md.
"""

from __future__ import annotations

from themis_infra import baseline, sql, storage, web

__all__ = ['baseline', 'sql', 'storage', 'web']
