"""MaveDB adapter: the depositor's calibrated MAVE result for one variant (the *_FXN input).

Flow: ask MaveDB which deposited measurements it holds on each queried ClinGen allele id -> read the
score set behind each measurement (its publication date and its ``scoreCalibrations``) -> answer
from the first measurement whose score falls in a calibration bin. The bin's OddsPath ratio + ACMG
criterion/strength become the typed fields, and that measurement's variant record rides in ``raw``
with its score set.

The lookup is keyed on ClinGen allele ids, one request per id. MaveDB relates the nucleotide and
protein levels itself — it labels each measurement ``direct``, ``protein_consequence`` or
``nucleotide_encoding`` — and the caller's expression is registered to *both* kinds
(``allele_registry.fetch_clingen_allele_ids``), with both asked.
``docs/design/evidence-interfaces.md`` records what that redundancy is worth, and why the keying is
not on HGVS text.

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

import asyncio
import dataclasses
import urllib.parse
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from typing import NamedTuple

import httpx2

from themis.services.evidence import errors

_API_URL = 'https://api.mavedb.org/api/v1'
_SOURCE = 'MaveDB'

# How a measurement relates to the allele asked about, most direct first.
_RELATIONSHIPS = ('direct', 'protein_consequence', 'nucleotide_encoding')

# Of the other sources a variant record cites, the ones ``raw`` keeps: these annotations, and
# cross-references into this identifier system.
_ANNOTATION_SOURCES = frozenset({'vep', 'gnomad'})
_CLINGEN_SYSTEM = 'https://reg.clinicalgenome.org/'


class _Candidate(NamedTuple):
    """One deposited measurement MaveDB lists for an asked allele, and how it was reached."""

    allele_id: str
    relationship: str
    urn: str
    score_set_urn: str
    score: float | None


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
        raw: The matched variant record (its ``scores`` row), with its score set — title, experiment,
            ``scoreCalibrations`` — under ``scoreSet``. ClinVar's entries on the record and the
            calibrations' control variants are left out.
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


async def _gathered[T](calls: Iterable[Coroutine[object, None, T]]) -> list[T]:
    """The results of independent MaveDB calls made together; a failed one surfaces as itself, not as a group."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(call) for call in calls]
    except BaseExceptionGroup as failures:
        raise errors.first_failure(failures) from failures
    return [task.result() for task in tasks]


def _score(value: object, urn: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'MaveDB measurement {urn!r} carries a score in no known shape')
    return float(value)


def _candidate(entry: object, allele_id: str) -> _Candidate | None:
    """One listed measurement, or ``None`` for one from a superseded version of its score set.

    Read whole either way: an entry skipped as unreadable would count as an absence.
    """
    if not isinstance(entry, dict):
        raise ValueError(f'MaveDB listed a non-object measurement for ClinGen allele {allele_id!r}')
    urn = entry.get('variantUrn')
    if not isinstance(urn, str) or not urn:
        raise ValueError(f'MaveDB listed a measurement for ClinGen allele {allele_id!r} naming no variant')
    score_set_urn = entry.get('scoreSetUrn')
    if not isinstance(score_set_urn, str) or not score_set_urn:
        raise ValueError(f'MaveDB measurement {urn!r} names no score set')
    relationship = entry.get('relationship')
    if not isinstance(relationship, str) or relationship not in _RELATIONSHIPS:
        raise ValueError(f'MaveDB measurement {urn!r} relates to ClinGen allele {allele_id!r} in no known way')
    current = entry.get('isCurrent')
    if not isinstance(current, bool):
        raise ValueError(f'MaveDB measurement {urn!r} does not state whether its score set version is current')
    candidate = _Candidate(allele_id, relationship, urn, score_set_urn, _score(entry.get('score'), urn))
    return candidate if current else None


async def _measurements(allele_id: str, *, http_client: httpx2.AsyncClient) -> list[_Candidate]:
    """MaveDB's current measurements on one ClinGen allele; an allele it holds nothing on lists none."""
    encoded = urllib.parse.quote(allele_id, safe='')
    response = await http_client.get(f'{_API_URL}/clingen-alleles/{encoded}/measurements')
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'ClinGen allele {allele_id!r}')
    body = response.json()
    if not isinstance(body, list):
        raise ValueError(f'MaveDB measurements for ClinGen allele {allele_id!r} are not a list')
    listed = [_candidate(entry, allele_id) for entry in body]
    return [candidate for candidate in listed if candidate is not None]


async def _record(collection: str, urn: str, *, http_client: httpx2.AsyncClient) -> dict[str, object]:
    """One record MaveDB itself named, by URN: a variant's (``variants``) or a score set's (``score-sets``).

    The URN comes from MaveDB's own measurement list, so a 404 here is an inconsistency between its
    endpoints rather than a verdict on anything the caller sent — it must not reach the caller as
    INVALID_ARGUMENT, which would send an agent off rewriting a correct expression.
    """
    # A variant URN carries a '#', which must be percent-encoded in the path.
    encoded = urllib.parse.quote(urn, safe='')
    response = await http_client.get(f'{_API_URL}/{collection}/{encoded}')
    if response.status_code == httpx2.codes.NOT_FOUND:
        raise ValueError(f'MaveDB has no {collection} record for {urn!r}, which its own measurements named')
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'{collection} record {urn!r}')
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f'MaveDB {collection} record {urn!r} is not an object')
    return body


def _published(score_set: Mapping[str, object], urn: str) -> str:
    """The ISO date the score set was published; empty when it is unpublished."""
    published = score_set.get('publishedDate')
    if published is None:
        return ''
    if not isinstance(published, str):
        raise ValueError(f'MaveDB score set {urn!r} carries a publishedDate in no known shape')
    return published


def _calibrations(score_set: Mapping[str, object], urn: str) -> list[dict[str, object]]:
    """The score set's calibrations; none where it states an empty list, a null, or nothing."""
    calibrations = score_set.get('scoreCalibrations')
    if calibrations is None:
        return []
    if not isinstance(calibrations, list) or not all(isinstance(c, dict) for c in calibrations):
        raise ValueError(f'MaveDB score set {urn!r} carries its scoreCalibrations in no known shape')
    return list(calibrations)


def _ranked(matched: Iterable[_Candidate], published: Mapping[str, str], allele_ids: Sequence[str]) -> list[_Candidate]:
    """Every matched measurement once, in a total order the payloads' own order cannot move."""
    # Least significant key first: a stable sort leaves each earlier ordering intact under the next.
    # `asked` ranks above the date because a deposit reached under the caller's own allele is about
    # the caller's own change, and one reached under a derived allele is about its consequence.
    asked = {allele_id: rank for rank, allele_id in enumerate(allele_ids)}
    ranked = sorted(matched, key=lambda c: c.urn)
    ranked.sort(key=lambda c: published[c.score_set_urn], reverse=True)
    ranked.sort(key=lambda c: asked[c.allele_id])
    ranked.sort(key=lambda c: _RELATIONSHIPS.index(c.relationship))
    unique: dict[str, _Candidate] = {}
    for candidate in ranked:
        unique.setdefault(candidate.urn, candidate)
    return list(unique.values())


def _selected(
    candidates: Sequence[_Candidate], calibrations: Mapping[str, Sequence[dict[str, object]]]
) -> tuple[_Candidate, dict[str, object] | None] | None:
    """The deposit to answer from and its calibration bin, or ``None`` when nothing was matched.

    Calibrated first, else scored, else the leading candidate — a deposit that carries neither a
    calibration nor a numeric score would otherwise shadow one further down that carries both.
    """
    scored: _Candidate | None = None
    for candidate in candidates:
        classification = _select_classification(calibrations[candidate.score_set_urn], candidate.score)
        if classification is not None:
            return candidate, classification
        if scored is None and candidate.score is not None:
            scored = candidate
    if scored is not None:
        return scored, None
    return (candidates[0], None) if candidates else None


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


def _kept_annotations(record: Mapping[str, object], urn: str) -> dict[str, object]:
    """Each linked allele's annotations, from the sources the answer keeps."""
    annotations = record.get('annotations')
    if not isinstance(annotations, dict) or not all(isinstance(sources, dict) for sources in annotations.values()):
        raise ValueError(f'MaveDB variant {urn!r} carries its annotations in no known shape')
    return {
        allele: {source: held for source, held in sources.items() if source in _ANNOTATION_SOURCES}
        for allele, sources in annotations.items()
    }


def _kept_mappings(representation: object, urn: str) -> dict[str, object]:
    """The variant's GA4GH representation, cross-referenced to ClinGen alleles alone."""
    if not isinstance(representation, dict):
        raise ValueError(f'MaveDB variant {urn!r} carries its molecularRepresentation in no known shape')
    mappings = representation.get('mappings')
    if mappings is None:
        return dict(representation)
    if not isinstance(mappings, list):
        raise ValueError(f'MaveDB variant {urn!r} carries its cross-references in no known shape')
    kept: list[object] = []
    for mapping in mappings:
        coding = mapping.get('coding') if isinstance(mapping, dict) else None
        if not isinstance(coding, dict):
            raise ValueError(f'MaveDB variant {urn!r} carries a cross-reference in no known shape')
        if coding.get('system') == _CLINGEN_SYSTEM:
            kept.append(mapping)
    return {**representation, 'mappings': kept}


def _raw(
    record: Mapping[str, object],
    score_set: Mapping[str, object],
    calibrations: Iterable[Mapping[str, object]],
    urn: str,
) -> dict[str, object]:
    """The variant's record with its score set under ``scoreSet``, as the answer passes them through.

    Raises:
        ValueError: If the record's annotations or cross-references are in a shape this cannot read:
            what cannot be read cannot be shown to hold no ClinVar entry.
    """
    # ClinVar's annotations and ids and the calibrations' control labels stay out: the ClinVar service reports those.
    raw = {**record, 'annotations': _kept_annotations(record, urn)}
    if record.get('molecularRepresentation') is not None:
        raw['molecularRepresentation'] = _kept_mappings(record['molecularRepresentation'], urn)
    raw['scoreSet'] = {
        **score_set,
        'scoreCalibrations': [
            {field: held for field, held in calibration.items() if field != 'controls'} for calibration in calibrations
        ],
    }
    return raw


async def fetch_mavedb(allele_ids: Sequence[str], *, http_client: httpx2.AsyncClient) -> MavedbResult:
    """Fetch the depositor's MAVE calibration for one variant, by its ClinGen allele ids.

    Args:
        allele_ids: The ClinGen allele ids the variant registers, in the order to prefer them
            (``allele_registry.fetch_clingen_allele_ids``). Each is asked in a request of its own.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The ``MavedbResult``: the score's OddsPath + ACMG criterion/strength (unset when no deposit
        holding the variant is calibrated), the raw functional score, the variant record, and
        provenance.

    Raises:
        ValueError: If ``allele_ids`` is empty, or a response is not the shape MaveDB documents.
        errors.InvalidRequestError: If MaveDB refuses a call (``errors.raise_for_status``).
        httpx2.HTTPStatusError: If a MaveDB call returns any other non-2xx: a 429 or a 5xx, or a
            status that names the endpoint rather than the request.
        errors.UnknownVariantError: If no deposited variant carries any of the ids — MaveDB has
            answered, and the answer is that no assay covers it.
    """
    if not allele_ids:
        raise ValueError('MaveDB lookup needs at least one ClinGen allele id')
    asked = list(dict.fromkeys(allele_ids))
    listed = await _gathered(_measurements(allele_id, http_client=http_client) for allele_id in asked)
    matched = [candidate for measurements in listed for candidate in measurements]

    score_set_urns = sorted({candidate.score_set_urn for candidate in matched})
    records = await _gathered(_record('score-sets', urn, http_client=http_client) for urn in score_set_urns)
    score_sets = dict(zip(score_set_urns, records, strict=True))
    published = {urn: _published(score_set, urn) for urn, score_set in score_sets.items()}
    calibrations = {urn: _calibrations(score_set, urn) for urn, score_set in score_sets.items()}

    candidates = _ranked(matched, published, asked)
    chosen = _selected(candidates, calibrations)
    if chosen is None:
        raise errors.UnknownVariantError(f'MaveDB has no scored variant for ClinGen allele(s) {asked}')

    candidate, classification = chosen
    record = await _record('variants', candidate.urn, http_client=http_client)
    oddspath, criterion, strength = _acmg_fields(classification)
    considered = [(c.allele_id, c.relationship, c.urn, published[c.score_set_urn]) for c in candidates]
    return MavedbResult(
        oddspath_ratio=oddspath,
        acmg_criterion=criterion,
        acmg_strength=strength,
        score=candidate.score,
        raw=_raw(record, score_sets[candidate.score_set_urn], calibrations[candidate.score_set_urn], candidate.urn),
        source=_SOURCE,
        dataset_versions=(candidate.score_set_urn,),
        query=f'clingen_allele_ids={asked}; candidates={considered} -> {candidate.urn}',
    )
