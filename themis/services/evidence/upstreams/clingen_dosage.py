"""ClinGen Dosage Sensitivity: the gene haploinsufficiency score.

The gene-dosage CSV is parsed once from the reference-bucket dump and indexed by HGNC id
(one curation per gene). ``lookup`` returns the ClinGen numeric haploinsufficiency score
— the machine-readable slice of the LoF-mechanism signal ``DescribeGeneResponse`` composes.

The download reports haploinsufficiency as a *text label*, not the numeric score; the
labels map to ClinGen's 0-3 / 30 / 40 scale here. A gene absent from the table is a
``None`` result — distinct from a present gene scored 0 (`No Evidence`).
"""

from __future__ import annotations

import dataclasses

from themis.services.evidence.upstreams import _clingen_download

_SOURCE = 'ClinGen Dosage Sensitivity'

_HGNC_COL = 'HGNC ID'
_HI_COL = 'HAPLOINSUFFICIENCY'

# ClinGen haploinsufficiency text labels -> the numeric dosage score. 30 = the gene
# acts through an autosomal-recessive mechanism (dosage N/A); 40 = dosage sensitivity
# unlikely.
_HI_SCORES = {
    'No Evidence for Haploinsufficiency': 0,
    'Little Evidence for Haploinsufficiency': 1,
    'Emerging Evidence for Haploinsufficiency': 2,
    'Sufficient Evidence for Haploinsufficiency': 3,
    'Gene Associated with Autosomal Recessive Phenotype': 30,
    'Dosage Sensitivity Unlikely for Haploinsufficiency': 40,
}


@dataclasses.dataclass(frozen=True)
class ClinGenDosageResult:
    """One gene's ClinGen haploinsufficiency curation.

    Attributes:
        haploinsufficiency_score: The ClinGen HI score (0-3, 30, 40).
        raw: The curation row verbatim, for the proto ``Struct``.
        source: Provenance source label.
        dataset_versions: The download's ``FILE CREATED`` date — the one release the curation rests on.
        query: The looked-up HGNC id.
    """

    haploinsufficiency_score: int
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _score(label: str) -> int:
    try:
        return _HI_SCORES[label]
    except KeyError:
        raise ValueError(f'unknown ClinGen haploinsufficiency label {label!r}') from None


class ClinGenDosage:
    """The gene-dosage table: HGNC id -> its ClinGen dosage curation.

    Parsed once via :meth:`from_bytes`; ``lookup`` is a synchronous in-memory query. A
    gene absent from the table is a ``None`` result, never a fabricated score.
    """

    def __init__(self, by_hgnc: dict[str, dict[str, str]], dataset_versions: tuple[str, ...]) -> None:
        self._by_hgnc = by_hgnc
        self._dataset_versions = dataset_versions

    @classmethod
    def from_bytes(cls, data: bytes) -> ClinGenDosage:
        """Parse and index the gene-dosage table (raw CSV bytes from the reference bucket).

        Raises:
            ValueError: If the download's preamble/header shape is unexpected.
        """
        rows, file_created = _clingen_download.parse(data.decode('utf-8'))
        by_hgnc = {row[_HGNC_COL].upper(): row for row in rows}
        return cls(by_hgnc, (file_created,))

    def lookup(self, hgnc_id: str) -> ClinGenDosageResult | None:
        """Return the gene's haploinsufficiency curation, or ``None`` if absent.

        Args:
            hgnc_id: HGNC id (``HGNC:nnnn``, case-insensitive).

        Raises:
            ValueError: If the row's haploinsufficiency label is outside the known
                ClinGen vocabulary (a stale label map, not a silent miss).
        """
        row = self._by_hgnc.get(hgnc_id.upper())
        if row is None:
            return None
        return ClinGenDosageResult(
            haploinsufficiency_score=_score(row[_HI_COL]),
            raw={'row': row},
            source=_SOURCE,
            dataset_versions=self._dataset_versions,
            query=hgnc_id,
        )
