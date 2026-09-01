"""Fetch the Mendeliome + Incidentalome panels and build the ``panelapp/dump.json`` dump.

PanelApp Australia exposes no ETag, so the dump is rebuilt every run. Each panel's genes come from
the panel-detail endpoint (the full raw gene JSON is kept verbatim in ``entries`` — publications and
all, not hand-picked); each gene's free-text mechanism narratives come from the per-gene evaluations
endpoint (``include_comments=true``). Both panels are aggregated by HGNC id into exactly the shape
``upstreams.panelapp.PanelAppTable`` parses: typed ``max_confidence`` / ``mode_of_inheritance`` /
``mode_of_pathogenicity`` convenience fields for the gate, plus the generous ``entries`` and the
de-duped ``evaluations`` list.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
from collections.abc import Iterable, Mapping

import httpx2

from themis.services.evidence.gene_disease.refresh import _http

_BASE_URL = 'https://panelapp-aus.org/api/v1'

# Mendeliome and Incidentalome — the dump's panel scope (spec-locked). Confidence is the max across
# the two; the dump keys every gene by HGNC id.
_PANEL_IDS = (137, 126)

# Bound the per-gene evaluations fan-out so a full-panel refresh does not open thousands of
# concurrent sockets against PanelApp.
_CONCURRENCY = 10

# A gene's evaluations are the handful of comments its curators left, so a real one is answered in a
# page or two. The cap is what turns a `next` chain that never ends — a page pointing at itself —
# into a loud failure instead of a job that spins against PanelApp until its timeout.
_MAX_EVALUATION_PAGES = 50


@dataclasses.dataclass
class _GeneAccumulator:
    """A gene's raw panel entries and the panels it was found on, gathered across the dump."""

    entries: list[dict[str, object]] = dataclasses.field(default_factory=list)
    panel_ids: list[int] = dataclasses.field(default_factory=list)


async def build_dump(client: httpx2.AsyncClient, *, refresh_date: str | None = None) -> dict[str, object]:
    """Fetch both panels and their evaluations and assemble the ``panelapp/dump.json`` payload.

    Args:
        client: The caller-owned async client every request rides.
        refresh_date: The ISO date stamped as the dump's sole ``dataset_versions`` element; today
            (UTC) if omitted.

    Returns:
        The dump dict in the shape the server's ``PanelAppTable`` parses.

    Raises:
        httpx2.HTTPStatusError: If a panel-detail or evaluations request fails (non-404).
        ValueError: If a panel detail or gene entry has an unexpected shape, or a gene's evaluations
            page off the PanelApp API or never stop paging.
    """
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    fetched = await asyncio.gather(*(_fetch_panel(client, semaphore, panel_id) for panel_id in _PANEL_IDS))

    panels: dict[str, str] = {}
    accumulators: dict[str, _GeneAccumulator] = {}
    for panel_id, (panel_name, genes) in zip(_PANEL_IDS, fetched, strict=True):
        panels[str(panel_id)] = panel_name
        for gene in genes:
            accumulator = accumulators.setdefault(_hgnc_id(gene, panel_id), _GeneAccumulator())
            accumulator.entries.append(gene)
            accumulator.panel_ids.append(panel_id)

    if not accumulators:
        raise ValueError('PanelApp panels yielded no genes; refusing to overwrite the reference with an empty dump')

    evaluations = await _fetch_all_evaluations(client, semaphore, accumulators)
    genes_out = {
        hgnc_id: _gene_record(accumulator, evaluations[hgnc_id]) for hgnc_id, accumulator in accumulators.items()
    }
    version = refresh_date if refresh_date is not None else datetime.datetime.now(datetime.UTC).date().isoformat()
    return {'dataset_versions': [version], 'panels': panels, 'genes': genes_out}


def serialise_dump(dump: Mapping[str, object]) -> bytes:
    """Serialise the dump to the UTF-8 JSON bytes written to ``panelapp/dump.json``."""
    return json.dumps(dump, ensure_ascii=False, indent=2).encode('utf-8')


async def _fetch_panel(
    client: httpx2.AsyncClient, semaphore: asyncio.Semaphore, panel_id: int
) -> tuple[str, list[dict[str, object]]]:
    async with semaphore:
        response = await _http.request_with_retry(client, 'GET', f'{_BASE_URL}/panels/{panel_id}/')
    response.raise_for_status()
    return _panel_detail(response.json(), panel_id)


async def _fetch_all_evaluations(
    client: httpx2.AsyncClient, semaphore: asyncio.Semaphore, accumulators: Mapping[str, _GeneAccumulator]
) -> dict[str, list[str]]:
    """Fetch every ``(gene, panel)`` evaluation set concurrently and merge/de-dupe per HGNC id."""
    requests = [
        (hgnc_id, panel_id) for hgnc_id, accumulator in accumulators.items() for panel_id in accumulator.panel_ids
    ]
    per_request = await asyncio.gather(
        *(_fetch_evaluations(client, semaphore, panel_id, hgnc_id) for hgnc_id, panel_id in requests)
    )
    merged: dict[str, list[str]] = {hgnc_id: [] for hgnc_id in accumulators}
    for (hgnc_id, _panel_id), comments in zip(requests, per_request, strict=True):
        merged[hgnc_id].extend(comments)
    return {hgnc_id: _dedupe(comments) for hgnc_id, comments in merged.items()}


async def _fetch_evaluations(
    client: httpx2.AsyncClient, semaphore: asyncio.Semaphore, panel_id: int, hgnc_id: str
) -> list[str]:
    """The gene's non-empty evaluation comments on ``panel_id``; ``[]`` if it is not on the panel."""
    numeric = hgnc_id.removeprefix('HGNC:')
    page_url = f'{_BASE_URL}/panels/{panel_id}/genes/HGNC:{numeric}/evaluations/?include_comments=true'
    comments: list[str] = []
    for _page in range(_MAX_EVALUATION_PAGES):
        async with semaphore:
            response = await _http.request_with_retry(client, 'GET', page_url)
        if response.status_code == httpx2.codes.NOT_FOUND:
            return []
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or 'results' not in payload:
            raise ValueError(f'PanelApp evaluations for {hgnc_id} on panel {panel_id} lack a results envelope')
        results = payload['results']
        if not isinstance(results, list):
            raise ValueError(f'PanelApp evaluations for {hgnc_id} on panel {panel_id} have a non-list results')
        for result in results:
            comments.extend(_result_comments(result))
        following = _next_page_url(payload, panel_id, hgnc_id)
        if following is None:
            return comments
        page_url = following
    raise ValueError(
        f'PanelApp evaluations for {hgnc_id} on panel {panel_id} page on past {_MAX_EVALUATION_PAGES} pages; '
        'the upstream is looping rather than ending'
    )


def _next_page_url(payload: Mapping[str, object], panel_id: int, hgnc_id: str) -> str | None:
    """The payload's ``next`` link, or ``None`` at the last page.

    The link is upstream text and the refresh client follows redirects, so one leaving the PanelApp
    API would send the job's requests — and whatever an environment attaches to them — somewhere
    else entirely. Only a link under ``_BASE_URL`` is followed.

    Raises:
        ValueError: If ``next`` is neither absent/null nor a URL under the PanelApp API.
    """
    following = payload.get('next')
    if following is None:
        return None
    if not isinstance(following, str) or not following.startswith(f'{_BASE_URL}/'):
        raise ValueError(
            f'PanelApp evaluations for {hgnc_id} on panel {panel_id} page to {following!r}, '
            f'which is not under {_BASE_URL}'
        )
    return following


def _panel_detail(payload: object, panel_id: int) -> tuple[str, list[dict[str, object]]]:
    if not isinstance(payload, dict):
        raise ValueError(f'PanelApp panel {panel_id} detail is not a JSON object')
    name = payload.get('name')
    if not isinstance(name, str) or not name:
        raise ValueError(f'PanelApp panel {panel_id} detail has no name')
    genes = payload.get('genes')
    if not isinstance(genes, list):
        raise ValueError(f'PanelApp panel {panel_id} detail has no genes list')
    records: list[dict[str, object]] = []
    for gene in genes:
        if not isinstance(gene, dict):
            raise ValueError(f'PanelApp panel {panel_id} has a non-object gene entry')
        records.append(gene)
    return name, records


def _result_comments(result: object) -> list[str]:
    if not isinstance(result, dict):
        return []
    comments = result.get('comments', [])
    if not isinstance(comments, list):
        return []
    texts: list[str] = []
    for comment in comments:
        if isinstance(comment, dict):
            text = comment.get('comment')
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return texts


def _gene_record(accumulator: _GeneAccumulator, evaluations: list[str]) -> dict[str, object]:
    """One gene's dump record: the typed convenience fields plus the verbatim entries + evaluations."""
    entries = accumulator.entries
    highest = max(entries, key=_confidence)  # first max on a tie: the earlier-listed panel wins
    veto = next((mop for entry in entries if (mop := _str_field(entry, 'mode_of_pathogenicity'))), '')
    return {
        'gene_symbol': _gene_symbol(entries[0]),
        'max_confidence': max(_confidence(entry) for entry in entries),
        'mode_of_inheritance': _str_field(highest, 'mode_of_inheritance'),
        'mode_of_pathogenicity': veto,
        'entries': entries,
        'evaluations': evaluations,
    }


def _hgnc_id(gene: dict[str, object], panel_id: int) -> str:
    gene_data = gene.get('gene_data')
    if not isinstance(gene_data, dict):
        raise ValueError(f'PanelApp panel {panel_id} gene entry has no gene_data object')
    hgnc_id = gene_data.get('hgnc_id')
    if not isinstance(hgnc_id, str) or not hgnc_id.startswith('HGNC:'):
        raise ValueError(f'PanelApp panel {panel_id} gene entry has no HGNC id (got {hgnc_id!r})')
    return hgnc_id


def _gene_symbol(gene: dict[str, object]) -> str:
    gene_data = gene.get('gene_data')
    if not isinstance(gene_data, dict):
        raise ValueError('PanelApp gene entry has no gene_data object')
    symbol = gene_data.get('gene_symbol')
    if not isinstance(symbol, str) or not symbol:
        raise ValueError(f'PanelApp gene entry has no gene_symbol (got {symbol!r})')
    return symbol


def _confidence(gene: dict[str, object]) -> int:
    level = gene.get('confidence_level')
    if not isinstance(level, str) or not level.isdigit():
        raise ValueError(f'PanelApp gene entry has non-numeric confidence_level {level!r}')
    return int(level)


def _str_field(gene: dict[str, object], key: str) -> str:
    value = gene.get(key)
    if value is None:  # absent or JSON null — the field's legitimate empty (mode_of_pathogenicity is usually null)
        return ''
    if not isinstance(value, str):
        raise ValueError(f'PanelApp gene entry has non-string {key!r} {value!r}')
    return value


def _dedupe(comments: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(comment for comment in comments if comment.strip()))
