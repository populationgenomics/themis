"""The weekly gene-disease reference-refresh job.

Fetches the ClinGen / GenCC / PanelApp upstreams and writes the four reference dumps to the GCS
bucket the gene_disease interface loads at startup
(``themis/services/evidence/gene_disease/backend.py``). Run as
``python -m themis.services.evidence.gene_disease.refresh``. The bucket layout is the parse contract
shared with that loader: ``gencc/submissions.tsv``, ``clingen/validity.csv``, ``clingen/dosage.csv``
(raw, stored verbatim), and ``panelapp/dump.json`` (transformed).

These fetches stay off ``themis.services.evidence.errors`` — that taxonomy classifies a 4xx for a
gRPC caller, and this job has none and sends none a caller chose. A non-retryable status here fails
the run; ``_http`` retries only what a retry can clear.
"""

from __future__ import annotations
