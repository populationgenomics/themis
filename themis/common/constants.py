"""Shared constants for themis — values with one correct setting and no per-env variance.

Not deployment config and not secrets: an env seam (with-a-default is out under the
fail-loud norm, env-required would guard a value that never legitimately differs) would
be the wrong shape here.
"""

from __future__ import annotations

# The role-inbox contact themis presents to external data providers — Crossref's
# polite-pool `mailto`, the NCBI eutils `email` — so a provider can reach the team about
# themis's traffic before throttling it. A stable group address (never an individual's,
# since this reaches the public mirror) is what the provider etiquette rewards.
CONTACT_EMAIL = 'themis-dev@populationgenomics.org.au'
