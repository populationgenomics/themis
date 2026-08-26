"""Orchestrate the weekly reference refresh: the three raw dumps plus the PanelApp dump.

``run`` refreshes the GenCC / ClinGen-validity / ClinGen-dosage files conditionally (ETag), rebuilds
the PanelApp dump unconditionally (PanelApp exposes no ETag), and writes all four to the gene-disease
dataset of the resources bucket the gene_disease interface loads at startup. The object names mirror
that loader (``themis/services/evidence/gene_disease/backend.py``) — the dumps are the parse contract
between them.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import httpx

from themis.services.evidence.gene_disease.refresh import object_store, panelapp, raw_files
from themis.services.evidence.upstreams import clingen_dosage, clingen_validity, gencc
from themis.services.evidence.upstreams import panelapp as panelapp_table


@dataclasses.dataclass(frozen=True)
class _RawSource:
    url: str
    object_name: str
    validate: Callable[[bytes], object]


# The dataset this job owns in the shared resources bucket; every object it writes sits under it.
_DATASET_PREFIX = 'gene-disease'

# Upstream download URLs -> the raw (verbatim) bucket objects, each paired with the server loader its
# fresh bytes are round-tripped through before the write. Object names match the server's reference
# loader; the raw bytes are the parse contract (GenCC TSV, ClinGen validity/dosage CSVs).
_RAW_SOURCES = (
    _RawSource(
        'https://search.thegencc.org/download/action/submissions-export-tsv',
        f'{_DATASET_PREFIX}/gencc/submissions.tsv',
        gencc.GenCC.from_bytes,
    ),
    _RawSource(
        'https://search.clinicalgenome.org/kb/gene-validity/download',
        f'{_DATASET_PREFIX}/clingen/validity.csv',
        clingen_validity.ClinGenValidity.from_bytes,
    ),
    _RawSource(
        'https://search.clinicalgenome.org/kb/gene-dosage/download',
        f'{_DATASET_PREFIX}/clingen/dosage.csv',
        clingen_dosage.ClinGenDosage.from_bytes,
    ),
)

_PANELAPP_OBJECT = f'{_DATASET_PREFIX}/panelapp/dump.json'


@dataclasses.dataclass(frozen=True)
class RefreshReport:
    """What the run did, for the entrypoint to log.

    Attributes:
        raw_outcomes: One outcome per conditionally-refreshed raw file.
        panelapp_object: The PanelApp dump object written.
        panelapp_gene_count: How many genes the PanelApp dump carries.
    """

    raw_outcomes: list[raw_files.RefreshOutcome]
    panelapp_object: str
    panelapp_gene_count: int


async def run(store: object_store.ReferenceObjectStore, *, client: httpx.AsyncClient) -> RefreshReport:
    """Refresh all four reference dumps into the bucket ``store`` writes to.

    Args:
        store: The resources-bucket object store the dumps are written to.
        client: The caller-owned async client every upstream request rides.

    Returns:
        A report of what each raw refresh did and the PanelApp gene count written.

    Raises:
        ValueError: If a freshly produced dump fails to parse through its server loader (a format
            regression fails the job rather than poisoning the bucket).
    """
    raw_outcomes = [
        await raw_files.refresh_file(
            client, store, url=source.url, object_name=source.object_name, validate=source.validate
        )
        for source in _RAW_SOURCES
    ]
    dump = await panelapp.build_dump(client)
    serialised = panelapp.serialise_dump(dump)
    try:
        panelapp_table.PanelAppTable.from_bytes(serialised)
    except ValueError as e:
        raise ValueError(f'{_PANELAPP_OBJECT}: rebuilt dump did not parse through the server loader') from e
    await store.write(_PANELAPP_OBJECT, serialised)
    genes = dump['genes']
    gene_count = len(genes) if isinstance(genes, dict) else 0
    return RefreshReport(raw_outcomes=raw_outcomes, panelapp_object=_PANELAPP_OBJECT, panelapp_gene_count=gene_count)
