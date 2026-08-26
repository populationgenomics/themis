"""GenCC: harmonised gene-disease submissions aggregated across curators.

The GenCC submissions export (one row per submitter assertion, columns already harmonised to the
ClinGen classification vocabulary) is parsed once from the reference-bucket dump and indexed by HGNC id
(``gene_curie``). ``lookup`` groups a gene's submissions into the entities they assert about — one
per (``disease_curie``, ``moi_curie``) — and returns each entity's strongest classification on the
harmonised rank alongside every submitter's own assertion and mechanism note.

Reducing across submitters *within* an entity has a defined answer under a published vocabulary, so
it is made here; reducing across entities does not, and is not. A third of GenCC's entities carry
submitters that disagree, and a gene's entities routinely differ in classification and in mode.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import sys

from themis.svcv4 import gene_disease_validity

_SOURCE = 'GenCC'

_GENE_CURIE_COL = 'gene_curie'
_DISEASE_COL = 'disease_title'
_DISEASE_CURIE_COL = 'disease_curie'
_MOI_CURIE_COL = 'moi_curie'
_MOI_COL = 'moi_title'
_CLASSIFICATION_COL = 'classification_title'
_SUBMITTER_COL = 'submitter_title'
_NOTES_COL = 'submitted_as_notes'
_RUN_DATE_COL = 'submitted_run_date'

_REQUIRED_COLS = (
    _GENE_CURIE_COL,
    _DISEASE_COL,
    _DISEASE_CURIE_COL,
    _MOI_CURIE_COL,
    _MOI_COL,
    _CLASSIFICATION_COL,
    _SUBMITTER_COL,
    _NOTES_COL,
    _RUN_DATE_COL,
)


@dataclasses.dataclass(frozen=True)
class Submission:
    """One submitter's assertion about one entity, unreduced.

    Attributes:
        submitter: The submitting curator (``submitter_title``).
        classification: That submitter's own harmonised classification.
        mechanism_note: Its ``submitted_as_notes`` free text, empty where it carries none.
    """

    submitter: str
    classification: str
    mechanism_note: str


@dataclasses.dataclass(frozen=True)
class Entity:
    """One gene-disease entity GenCC carries, with the submissions behind it.

    Attributes:
        disease_title: The title the entity's first submission spells it with; ``disease_curie`` is
            the identity, of which the title is one alias.
        disease_curie: The MONDO term the submissions were harmonised onto.
        moi_curie: The HPO mode-of-inheritance term (``HP:0000006`` …), the mode's identity.
        moi_title: The mode as its first submission spells it.
        classification: The strongest classification across ``submissions`` on the harmonised rank.
        submissions: Every submitter assertion behind the entity, in export order.
    """

    disease_title: str
    disease_curie: str
    moi_curie: str
    moi_title: str
    classification: str
    submissions: list[Submission]


@dataclasses.dataclass(frozen=True)
class GenCCResult:
    """Every GenCC entity one gene carries.

    Attributes:
        entities: One per (disease term, mode of inheritance), in first-seen export order.
        raw: The submission rows verbatim, for the proto ``Struct``.
        source: Provenance source label.
        dataset_versions: The newest ``submitted_run_date`` in the export — the one release the
            submissions rest on.
        query: The lookup key (HGNC id).
    """

    entities: list[Entity]
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _entity(rows: list[dict[str, str]]) -> Entity:
    """Group one (disease term, mode) key's submissions into the entity they assert about."""
    submissions = [
        Submission(
            submitter=row[_SUBMITTER_COL],
            classification=row[_CLASSIFICATION_COL],
            mechanism_note=row[_NOTES_COL],
        )
        for row in rows
    ]
    strongest = max(submissions, key=lambda s: gene_disease_validity.rank(s.classification))
    first = rows[0]
    return Entity(
        disease_title=first[_DISEASE_COL],
        disease_curie=first[_DISEASE_CURIE_COL],
        moi_curie=first[_MOI_CURIE_COL],
        moi_title=first[_MOI_COL],
        classification=strongest.classification,
        submissions=submissions,
    )


class GenCC:
    """The GenCC submissions table: HGNC id -> its harmonised submissions.

    Parsed once via :meth:`from_bytes`; ``lookup`` is a synchronous in-memory query. A gene absent
    from the export is a ``None`` result, never a fabricated classification.
    """

    def __init__(self, by_hgnc: dict[str, list[dict[str, str]]], dataset_versions: tuple[str, ...]) -> None:
        self._by_hgnc = by_hgnc
        self._dataset_versions = dataset_versions

    @classmethod
    def from_bytes(cls, data: bytes) -> GenCC:
        """Parse and index the submissions export (raw TSV bytes from the reference bucket).

        The export is tab-separated with ``submitted_as_notes`` carrying embedded newlines, so it is
        read with the ``csv`` reader (which honours quoted multi-line fields), not by line-splitting.

        Raises:
            ValueError: If the export is empty or a required column is absent.
        """
        csv.field_size_limit(sys.maxsize)  # notes are long quoted fields with embedded newlines
        reader = csv.DictReader(io.StringIO(data.decode('utf-8')), delimiter='\t')
        missing = [col for col in _REQUIRED_COLS if col not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f'GenCC export is missing columns {missing}')
        by_hgnc: dict[str, list[dict[str, str]]] = {}
        newest_run = ''
        for row in reader:
            by_hgnc.setdefault(row[_GENE_CURIE_COL].upper(), []).append(row)
            newest_run = max(newest_run, row[_RUN_DATE_COL])
        if not by_hgnc:
            raise ValueError('GenCC export has no submission rows')
        return cls(by_hgnc, (newest_run,))

    def lookup(self, hgnc_id: str) -> GenCCResult | None:
        """Return every entity the gene's submissions assert about, or ``None`` if it has none.

        Args:
            hgnc_id: HGNC id (``HGNC:nnnn``, case-insensitive).

        Raises:
            ValueError: If a row carries a classification outside the curated vocabulary (a stale
                classification set, not a silent miss).
        """
        rows = self._by_hgnc.get(hgnc_id.upper())
        if not rows:
            return None
        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in rows:
            grouped.setdefault((row[_DISEASE_CURIE_COL], row[_MOI_CURIE_COL]), []).append(row)
        return GenCCResult(
            entities=[_entity(group) for group in grouped.values()],
            raw={'rows': rows},
            source=_SOURCE,
            dataset_versions=self._dataset_versions,
            query=hgnc_id,
        )
