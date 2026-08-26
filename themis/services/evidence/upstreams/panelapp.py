"""PanelApp Australia: per-gene panel confidence, inheritance, the GoF veto, and evaluations.

Parsed once from the reference-bucket ``panelapp/dump.json`` the weekly refresh job builds
by fetching the Mendeliome and Incidentalome panels; indexed by HGNC id. ``lookup`` returns
the maximum confidence level across the two panels, the mode of inheritance of the
highest-confidence panel entry, any mode-of-pathogenicity flag (the sparse gain-of-function
*veto* the mechanism judgement reads), and the free-text evaluation comments. A gene listed
in neither panel is a ``None`` result, never a fabricated zero-confidence entry.

The dump is generous: each gene entry carries the FULL per-panel gene JSON (``entries``, incl. the
``publications`` list the agent pairs with LitVar) alongside the typed convenience fields the
gene-disease gate reads; nothing is hand-picked away.

Dump shape (see docs/design/evidence-interfaces.md)::

    {
      "dataset_versions": ["2026-07-24"],
      "panels": {"137": "Mendeliome", "126": "Incidentalome"},
      "genes": {"HGNC:1100": {"gene_symbol": ..., "max_confidence": 3,
                              "mode_of_inheritance": ..., "mode_of_pathogenicity": ...,
                              "entries": [{<full raw gene JSON per panel: confidence_level,
                                            publications[], phenotypes[], gene_data{...}, ...>}],
                              "evaluations": ["..."]}}
    }
"""

from __future__ import annotations

import dataclasses
import json

_SOURCE = 'PanelApp Australia'


@dataclasses.dataclass(frozen=True)
class PanelAppResult:
    """A gene's aggregated PanelApp Australia signals across the dumped panels.

    Attributes:
        gene_symbol: The HGNC gene symbol the dump recorded for the gene.
        max_confidence: The maximum ``confidence_level`` across the gene's panels
            (3 = green/diagnostic-grade, 1 = red).
        mode_of_inheritance: The MOI of the highest-confidence panel entry.
        mode_of_pathogenicity: A gain-of-function veto flag if any panel sets one,
            else empty (the usual case).
        evaluations: The free-text evaluation comments across the panels, de-duped
            and non-empty — the mechanism narratives ``DescribeGeneResponse`` surfaces.
        entries: The full per-panel gene JSON verbatim (``confidence_level``,
            ``publications``, ``phenotypes``, ``gene_data``, ...) — the generous payload
            the agent mines; nothing hand-picked away.
        panel_scope: The dumped panel names, e.g. ``Mendeliome, Incidentalome`` — the
            provenance scope each evaluation is attributed to.
        raw: The gene's dump entry verbatim, for the proto ``Struct``.
        source: Provenance source label.
        dataset_versions: The releases the dump names — its refresh date.
        query: The looked-up HGNC id.
    """

    gene_symbol: str
    max_confidence: int
    mode_of_inheritance: str
    mode_of_pathogenicity: str
    evaluations: list[str]
    entries: list[dict[str, object]]
    panel_scope: str
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _require(mapping: dict[str, object], key: str, hgnc_id: str) -> object:
    if key not in mapping:
        raise ValueError(f'PanelApp dump entry for {hgnc_id!r} has no {key!r}')
    return mapping[key]


def _int(value: object, key: str, hgnc_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'PanelApp dump entry for {hgnc_id!r} has non-integer {key!r} {value!r}')
    return value


def _str(value: object, key: str, hgnc_id: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'PanelApp dump entry for {hgnc_id!r} has non-string {key!r} {value!r}')
    return value


def _str_list(value: object, key: str, hgnc_id: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f'PanelApp dump entry for {hgnc_id!r} has non-string-list {key!r} {value!r}')
    return list(value)


def _dict_list(value: object, key: str, hgnc_id: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f'PanelApp dump entry for {hgnc_id!r} has non-object-list {key!r} {value!r}')
    return [dict(item) for item in value if isinstance(item, dict)]


class PanelAppTable:
    """The PanelApp Australia dump: HGNC id -> its aggregated panel signals.

    Parsed once via :meth:`from_bytes`; ``lookup`` is a synchronous in-memory query. A
    gene absent from the dump is a ``None`` result, never a fabricated entry.
    """

    def __init__(
        self, by_hgnc: dict[str, dict[str, object]], panel_scope: str, dataset_versions: tuple[str, ...]
    ) -> None:
        self._by_hgnc = by_hgnc
        self._panel_scope = panel_scope
        self._dataset_versions = dataset_versions

    @classmethod
    def from_bytes(cls, data: bytes) -> PanelAppTable:
        """Parse and index the PanelApp dump (``panelapp/dump.json`` bytes).

        Raises:
            ValueError: If the dump lacks its ``dataset_versions``, ``panels``, or ``genes``.
            json.JSONDecodeError: If the bytes are not valid JSON.
        """
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError('PanelApp dump is not a JSON object')
        versions = payload.get('dataset_versions')
        if not isinstance(versions, list) or not versions:
            raise ValueError(f'PanelApp dump names no dataset_versions: {versions!r}')
        if not all(isinstance(version, str) and version for version in versions):
            raise ValueError(f'PanelApp dump has an empty or non-string dataset_versions element: {versions!r}')
        panels = payload.get('panels')
        if not isinstance(panels, dict) or not panels:
            raise ValueError('PanelApp dump has no panels map')
        genes = payload.get('genes')
        if not isinstance(genes, dict):
            raise ValueError('PanelApp dump has no genes map')
        by_hgnc = {hgnc_id.upper(): entry for hgnc_id, entry in genes.items() if isinstance(entry, dict)}
        panel_scope = ', '.join(str(name) for name in panels.values())
        return cls(by_hgnc, panel_scope, tuple(versions))

    def lookup(self, hgnc_id: str) -> PanelAppResult | None:
        """Return the gene's aggregated PanelApp signals, or ``None`` if in neither panel.

        Args:
            hgnc_id: HGNC id (``HGNC:nnnn``, case-insensitive).

        Raises:
            ValueError: If the gene's dump entry is missing a field or carries a
                malformed type (a stale dump shape, not a silent miss).
        """
        entry = self._by_hgnc.get(hgnc_id.upper())
        if entry is None:
            return None
        return PanelAppResult(
            gene_symbol=_str(_require(entry, 'gene_symbol', hgnc_id), 'gene_symbol', hgnc_id),
            max_confidence=_int(_require(entry, 'max_confidence', hgnc_id), 'max_confidence', hgnc_id),
            mode_of_inheritance=_str(_require(entry, 'mode_of_inheritance', hgnc_id), 'mode_of_inheritance', hgnc_id),
            mode_of_pathogenicity=_str(
                _require(entry, 'mode_of_pathogenicity', hgnc_id), 'mode_of_pathogenicity', hgnc_id
            ),
            evaluations=_str_list(_require(entry, 'evaluations', hgnc_id), 'evaluations', hgnc_id),
            entries=_dict_list(_require(entry, 'entries', hgnc_id), 'entries', hgnc_id),
            panel_scope=self._panel_scope,
            raw=dict(entry),
            source=_SOURCE,
            dataset_versions=self._dataset_versions,
            query=hgnc_id,
        )
