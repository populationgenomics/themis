"""MaveDB adapter: the depositor's calibrated MAVE result for one variant (the *_FXN input).

Flow: ask MaveDB which of its scored variants carry the queried ClinGen allele ids -> fetch each
matched variant's record (its functional score plus the score set's ``scoreCalibrations``) until one
carries a calibration bin the score falls in. The bin's OddsPath ratio + ACMG criterion/strength
become the typed fields.

The lookup is keyed on ClinGen allele ids: every mapped variant MaveDB holds carries a
``clingenAlleleId``, and the endpoint answers a list of them in one request. MaveDB bridges the
nucleotide and protein levels itself, through ``equivalentAa``/``equivalentNt``, but not for every
allele — so the caller's expression is registered to *both* kinds
(``allele_registry.fetch_clingen_allele_ids``) and both are asked. ``docs/design/evidence-interfaces.md``
records what that redundancy is worth, and why the keying is not on HGVS text.

Calibration selection: MaveDB deposits several calibrations per score set (controls, ExCALIBR,
investigator classes). The bin carrying an OddsPath ratio is preferred, else one carrying an ACMG
classification. The full set rides in ``raw``. Calibration is often absent; the typed fields are then
unset, never fabricated.

Several deposits can score one allele, and they can disagree on strength for the same score, so the
candidate order is total and derived from the payload rather than taken from it: how directly the
allele matched, then publication date descending, then URN. Among them the first *calibrated*
candidate wins, else the first *scored* one. Every candidate rides in the provenance query.

Deviations from the documented response shape raise ``ValueError`` rather than resolving to an empty
candidate list; ``docs/design/evidence-interfaces.md`` records why.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from collections.abc import Iterable, Sequence
from typing import NamedTuple

import httpx

from themis.services.evidence import errors

_API_URL = 'https://api.mavedb.org/api/v1'
_SOURCE = 'MaveDB'

# How a lookup response reports a hit, most direct first. `exactMatch` arrives as a lone object, the
# other two as lists.
_MATCH_KINDS = ('exactMatch', 'equivalentAa', 'equivalentNt')


class _Candidate(NamedTuple):
    """One deposited measurement the lookup matched, and how it was reached."""

    allele_id: str
    kind: str
    urn: str
    published: str


@dataclasses.dataclass(frozen=True)
class MavedbResult:
    """The depositor's MAVE calibration for one variant (when present), with provenance.

    Attributes:
        oddspath_ratio: The OddsPath from the score's calibration bin; ``None`` when the deposit
            carries no OddsPath for that bin.
        acmg_criterion: The functional ACMG criterion the bin asserts (e.g. ``"PS3"`` / ``"BS3"``);
            empty when uncalibrated.
        acmg_strength: Its evidence strength, verbatim from MaveDB (e.g. ``"STRONG"``,
            ``"MODERATE_PLUS"``, ``"SUPPORTING"``); empty when uncalibrated.
        score: The variant's raw MAVE functional score; ``None`` when the row carries no numeric score.
        raw: The matched variant record (``score_data`` + the score set's ``scoreCalibrations``).
        source: The upstream label.
        dataset_versions: The matched score-set URN (identifies the exact deposit + version).
        query: The allele ids asked, every candidate URN they matched, and the one selected.
    """

    oddspath_ratio: float | None
    acmg_criterion: str
    acmg_strength: str
    score: float | None
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _above_lower(lower: object, score: float, *, inclusive: bool) -> bool:
    if not isinstance(lower, (int, float)) or isinstance(lower, bool):
        return True  # unbounded below
    return score > lower or (inclusive and score == lower)


def _below_upper(upper: object, score: float, *, inclusive: bool) -> bool:
    if not isinstance(upper, (int, float)) or isinstance(upper, bool):
        return True  # unbounded above
    return score < upper or (inclusive and score == upper)


def _in_range(classification: dict[str, object], score: float) -> bool:
    bounds = classification.get('range')
    if not isinstance(bounds, list) or len(bounds) != 2:
        return False
    lower, upper = bounds
    return _above_lower(lower, score, inclusive=bool(classification.get('inclusiveLowerBound'))) and _below_upper(
        upper, score, inclusive=bool(classification.get('inclusiveUpperBound'))
    )


def _depositor_calibrations(calibrations: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """The calibrations to read, in a total order: the one the depositor marked primary, else all.

    A deposit carries several (controls, ExCALIBR, investigator classes) and they disagree — reading
    them in payload order lets serialisation pick an ACMG strength. `primary` is the depositor's own
    answer to which one speaks for the deposit.

    Raises:
        ValueError: If several are marked primary. There is no ground for choosing among them, and
            guessing would put the arbitrariness back where it was, unlabelled.
    """
    primary = [c for c in calibrations if c.get('primary') is True]
    if len(primary) > 1:
        titles = sorted(str(c.get('title')) for c in primary)
        raise ValueError(f'MaveDB score set marks {len(primary)} calibrations primary: {titles}')
    # No primary: every calibration is read, ordered by the id MaveDB gives each one.
    return primary or sorted(calibrations, key=lambda c: str(c.get('urn')))


def _select_classification(calibrations: Sequence[dict[str, object]], score: float | None) -> dict[str, object] | None:
    """Pick the calibration bin containing ``score``, preferring one with an OddsPath, then ACMG."""
    if score is None:
        return None
    with_odds: dict[str, object] | None = None
    with_acmg: dict[str, object] | None = None
    for calibration in _depositor_calibrations(calibrations):
        classifications = calibration.get('functionalClassifications')
        if not isinstance(classifications, list):
            continue
        for classification in classifications:
            if not isinstance(classification, dict) or not _in_range(classification, score):
                continue
            if with_odds is None and isinstance(classification.get('oddspathsRatio'), (int, float)):
                with_odds = classification
            if with_acmg is None and isinstance(classification.get('acmgClassification'), dict):
                with_acmg = classification
    return with_odds or with_acmg


async def _lookup(allele_ids: Sequence[str], *, http_client: httpx.AsyncClient) -> list[dict[str, object]]:
    """MaveDB's answer for each queried ClinGen allele id — one entry per id, hit or not."""
    response = await http_client.post(
        f'{_API_URL}/variants/clingen-allele-id-lookups', json={'clingenAlleleIds': list(allele_ids)}
    )
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'ClinGen alleles {list(allele_ids)}')
    body = response.json()
    if not isinstance(body, list):
        raise ValueError(f'MaveDB allele lookup for {list(allele_ids)} is not a list')
    if not all(isinstance(entry, dict) for entry in body):
        raise ValueError(f'MaveDB allele lookup for {list(allele_ids)} carries a non-object answer')
    return list(body)


def _matched_variants(entry: dict[str, object], kind: str) -> list[dict[str, object]]:
    """The variants one answer reports under ``kind``; an explicit null is the miss, an absent key is not."""
    if kind not in entry:
        raise ValueError(f'MaveDB answer for ClinGen allele {entry.get("clingenAlleleId")!r} reports no {kind!r}')
    matched = entry[kind]
    if matched is None:
        return []
    if isinstance(matched, dict):
        return [matched]
    if isinstance(matched, list) and all(isinstance(variant, dict) for variant in matched):
        return list(matched)
    raise ValueError(f'MaveDB reported {kind!r} for ClinGen allele {entry.get("clingenAlleleId")!r} in no known shape')


def _published(measurement: dict[str, object], urn: str) -> str:
    """The ISO date the measurement's score set was published; empty when it is unpublished."""
    score_set = measurement.get('scoreSet')
    if not isinstance(score_set, dict):
        raise ValueError(f'MaveDB measurement {urn!r} names no score set')
    published = score_set.get('publishedDate')
    if published is None:
        return ''
    if not isinstance(published, str):
        raise ValueError(f'MaveDB measurement {urn!r} carries a publishedDate in no known shape')
    return published


def _measurements(variant: dict[str, object], allele_id: str, kind: str) -> list[_Candidate]:
    """The deposited measurements one matched variant carries — never none, since it is a match."""
    measurements = variant.get('variantEffectMeasurements')
    if not isinstance(measurements, list) or not measurements:
        raise ValueError(f'MaveDB reported {kind} on {allele_id!r} with no readable measurement list')
    found: list[_Candidate] = []
    for measurement in measurements:
        urn = measurement.get('urn') if isinstance(measurement, dict) else None
        if not isinstance(urn, str) or not urn:
            raise ValueError(f'MaveDB reported {kind} on {allele_id!r} with a measurement naming no variant')
        found.append(_Candidate(allele_id, kind, urn, _published(measurement, urn)))
    return found


def _by_allele_id(responses: Iterable[dict[str, object]]) -> dict[str, dict[str, object]]:
    by_id: dict[str, dict[str, object]] = {}
    for entry in responses:
        allele_id = entry.get('clingenAlleleId')
        if not isinstance(allele_id, str):
            raise ValueError('MaveDB returned a lookup answer naming no ClinGen allele')
        if allele_id in by_id:
            raise ValueError(f'MaveDB answered twice for ClinGen allele {allele_id!r}')
        by_id[allele_id] = entry
    return by_id


def _candidates(responses: Iterable[dict[str, object]], allele_ids: Sequence[str]) -> list[_Candidate]:
    """Every deposited measurement the lookup matched, in a total order the payload's own order cannot move.

    Raises:
        ValueError: If MaveDB left an asked id unanswered, answered one twice, or reported a match in
            a shape this cannot read — none of which is an absence, and all of which would otherwise
            reduce to one.
    """
    by_id = _by_allele_id(responses)
    matched: list[_Candidate] = []
    for allele_id in allele_ids:
        entry = by_id.get(allele_id)
        if entry is None:
            raise ValueError(f'MaveDB returned no answer for ClinGen allele {allele_id!r}')
        matched.extend(
            candidate
            for kind in _MATCH_KINDS
            for variant in _matched_variants(entry, kind)
            for candidate in _measurements(variant, allele_id, kind)
        )
    # Least significant key first: a stable sort leaves each earlier ordering intact under the next.
    # `asked` ranks above the date because a deposit reached under the caller's own allele is about
    # the caller's own change, and one reached under a derived allele is about its consequence.
    asked = {allele_id: rank for rank, allele_id in enumerate(allele_ids)}
    ranked = sorted(matched, key=lambda c: c.urn)
    ranked.sort(key=lambda c: c.published, reverse=True)
    ranked.sort(key=lambda c: asked[c.allele_id])
    ranked.sort(key=lambda c: _MATCH_KINDS.index(c.kind))
    unique: dict[str, _Candidate] = {}
    for candidate in ranked:
        unique.setdefault(candidate.urn, candidate)
    return list(unique.values())


async def _variant_record(variant_urn: str, *, http_client: httpx.AsyncClient) -> dict[str, object]:
    """One deposited measurement's record: its score, and its score set's calibrations.

    The URN is MaveDB's own, from the lookup, so a 404 here is an inconsistency between its two
    endpoints rather than a verdict on anything the caller sent — it must not reach the caller as
    INVALID_ARGUMENT, which would send an agent off rewriting a correct expression.
    """
    # The variant URN carries a '#', which must be percent-encoded in the path.
    encoded = urllib.parse.quote(variant_urn, safe='')
    response = await http_client.get(f'{_API_URL}/variants/{encoded}')
    if response.status_code == httpx.codes.NOT_FOUND:
        raise ValueError(f'MaveDB has no record for variant {variant_urn!r}, which its own lookup named')
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'variant {variant_urn!r}')
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f'MaveDB variant {variant_urn!r} is not an object')
    return body


def _score_of(record: dict[str, object]) -> float | None:
    data = record.get('data')
    score_data = data.get('score_data') if isinstance(data, dict) else None
    value = score_data.get('score') if isinstance(score_data, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _calibrations(record: dict[str, object]) -> list[dict[str, object]]:
    score_set = record.get('scoreSet')
    calibrations = score_set.get('scoreCalibrations') if isinstance(score_set, dict) else None
    if not isinstance(calibrations, list):
        return []
    return [c for c in calibrations if isinstance(c, dict)]


def _score_set_urn(record: dict[str, object], variant_urn: str) -> str:
    score_set = record.get('scoreSet')
    urn = score_set.get('urn') if isinstance(score_set, dict) else None
    if not isinstance(urn, str) or not urn:
        raise ValueError(f'MaveDB variant {variant_urn!r} names no score set')
    return urn


async def _selected(
    candidates: Sequence[_Candidate], *, http_client: httpx.AsyncClient
) -> tuple[_Candidate, dict[str, object], dict[str, object] | None] | None:
    """The deposit to answer from and its calibration bin, or ``None`` when nothing was matched.

    Calibrated first, else scored, else the leading candidate — a deposit that carries neither a
    calibration nor a numeric score would otherwise shadow one further down that carries both.
    """
    scored: tuple[_Candidate, dict[str, object]] | None = None
    first: tuple[_Candidate, dict[str, object]] | None = None
    for candidate in candidates:
        record = await _variant_record(candidate.urn, http_client=http_client)
        score = _score_of(record)
        classification = _select_classification(_calibrations(record), score)
        if classification is not None:
            return candidate, record, classification
        if first is None:
            first = (candidate, record)
        if scored is None and score is not None:
            scored = (candidate, record)
    fallback = scored or first
    return None if fallback is None else (fallback[0], fallback[1], None)


def _acmg_fields(classification: dict[str, object] | None) -> tuple[float | None, str, str]:
    if classification is None:
        return None, '', ''
    odds = classification.get('oddspathsRatio')
    oddspath = float(odds) if isinstance(odds, (int, float)) and not isinstance(odds, bool) else None
    acmg = classification.get('acmgClassification')
    if not isinstance(acmg, dict):
        return oddspath, '', ''
    criterion = acmg.get('criterion')
    strength = acmg.get('evidenceStrength')
    return oddspath, criterion if isinstance(criterion, str) else '', strength if isinstance(strength, str) else ''


async def fetch_mavedb(allele_ids: Sequence[str], *, http_client: httpx.AsyncClient) -> MavedbResult:
    """Fetch the depositor's MAVE calibration for one variant, by its ClinGen allele ids.

    Args:
        allele_ids: The ClinGen allele ids the variant registers, in the order to prefer them
            (``allele_registry.fetch_clingen_allele_ids``). All are asked in one request.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The ``MavedbResult``: the score's OddsPath + ACMG criterion/strength (unset when no deposit
        holding the variant is calibrated), the raw functional score, the variant record, and
        provenance.

    Raises:
        ValueError: If ``allele_ids`` is empty, or a response is not the shape MaveDB documents.
        errors.InvalidRequestError: If MaveDB refuses a call (a non-429 4xx).
        httpx.HTTPStatusError: If a MaveDB call returns a 429 or a 5xx.
        errors.UnknownVariantError: If no deposited variant carries any of the ids — MaveDB has
            answered, and the answer is that no assay covers it.
    """
    if not allele_ids:
        raise ValueError('MaveDB lookup needs at least one ClinGen allele id')
    asked = list(dict.fromkeys(allele_ids))  # MaveDB echoes a repeated id twice, which reads as its own fault
    candidates = _candidates(await _lookup(asked, http_client=http_client), asked)

    chosen = await _selected(candidates, http_client=http_client)
    if chosen is None:
        raise errors.UnknownVariantError(f'MaveDB has no scored variant for ClinGen allele(s) {asked}')

    candidate, record, classification = chosen
    oddspath, criterion, strength = _acmg_fields(classification)
    return MavedbResult(
        oddspath_ratio=oddspath,
        acmg_criterion=criterion,
        acmg_strength=strength,
        score=_score_of(record),
        raw=record,
        source=_SOURCE,
        dataset_versions=(_score_set_urn(record, candidate.urn),),
        query=f'clingen_allele_ids={asked}; candidates={[tuple(c) for c in candidates]} -> {candidate.urn}',
    )
