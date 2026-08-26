"""ClinGen Gene-Disease Validity: the gene's curated disease entities.

The gene-validity CSV is parsed once from the reference-bucket dump and indexed by HGNC id.
``lookup`` returns every curation the gene carries, one per row, plus the rows verbatim for ``raw``.
It selects nothing: which curated entity a presentation belongs to is the analyst's call, made
against the whole list.
"""

from __future__ import annotations

import dataclasses

from themis.services.evidence.upstreams import _clingen_download
from themis.svcv4 import gene_disease_validity

_SOURCE = 'ClinGen Gene Validity'

_HGNC_COL = 'GENE ID (HGNC)'
_DISEASE_LABEL_COL = 'DISEASE LABEL'
_DISEASE_ID_COL = 'DISEASE ID (MONDO)'
_MOI_COL = 'MOI'
_CLASSIFICATION_COL = 'CLASSIFICATION'


@dataclasses.dataclass(frozen=True)
class Curation:
    """One ClinGen curation: a gene-disease entity and the strength ClinGen assigns it.

    Attributes:
        disease_label: ClinGen's own label for the entity, one alias of ``mondo_id``.
        mondo_id: The MONDO term curated — the entity's identity, which the label is not.
        moi: ClinGen's mode-of-inheritance code (``AD``, ``AR``, ``XL``, ``SD``, ``MT``, ``UD``).
        classification: The validity classification, in ClinGen's own vocabulary.
    """

    disease_label: str
    mondo_id: str
    moi: str
    classification: str


@dataclasses.dataclass(frozen=True)
class ClinGenValidityResult:
    """Every ClinGen validity curation for one gene.

    Attributes:
        curations: One per curated row: a (disease term, mode of inheritance) and its classification.
        raw: The curation rows verbatim, for the proto ``Struct``.
        source: Provenance source label.
        dataset_versions: The download's ``FILE CREATED`` date — the one release the curations rest on.
        query: The lookup key (HGNC id).
    """

    curations: list[Curation]
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


class ClinGenValidity:
    """The gene-validity table: HGNC id -> its ClinGen validity curations.

    Parsed once via :meth:`from_bytes`; ``lookup`` is a synchronous in-memory query. A gene absent
    from the table is a ``None`` result, never a fabricated classification.
    """

    def __init__(self, by_hgnc: dict[str, list[dict[str, str]]], dataset_versions: tuple[str, ...]) -> None:
        self._by_hgnc = by_hgnc
        self._dataset_versions = dataset_versions

    @classmethod
    def from_bytes(cls, data: bytes) -> ClinGenValidity:
        """Parse and index the gene-validity table (raw CSV bytes from the reference bucket).

        Raises:
            ValueError: If the download's preamble/header shape is unexpected.
        """
        rows, file_created = _clingen_download.parse(data.decode('utf-8'))
        by_hgnc: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            by_hgnc.setdefault(row[_HGNC_COL].upper(), []).append(row)
        return cls(by_hgnc, (file_created,))

    def lookup(self, hgnc_id: str) -> ClinGenValidityResult | None:
        """Return every validity curation the gene carries, or ``None`` if it carries none.

        Args:
            hgnc_id: HGNC id (``HGNC:nnnn``, case-insensitive).

        Raises:
            ValueError: If a row carries a classification outside the curated vocabulary (a stale
                classification set, not a silent miss).
        """
        rows = self._by_hgnc.get(hgnc_id.upper())
        if not rows:
            return None
        curations = []
        for row in rows:
            gene_disease_validity.validate(row[_CLASSIFICATION_COL])
            curations.append(
                Curation(
                    disease_label=row[_DISEASE_LABEL_COL],
                    mondo_id=row[_DISEASE_ID_COL],
                    moi=row[_MOI_COL],
                    classification=row[_CLASSIFICATION_COL],
                )
            )
        return ClinGenValidityResult(
            curations=curations,
            raw={'rows': rows},
            source=_SOURCE,
            dataset_versions=self._dataset_versions,
            query=hgnc_id,
        )
