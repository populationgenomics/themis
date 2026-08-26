"""Broad SpliceAI + Pangolin adapter: the two splice-delta predictors for SPL_PRD.

Every variant type checks a splice effect. This adapter issues the two Broad Cloud Run calls — SpliceAI
and Pangolin — for one variant and reduces each to a gain delta and a loss delta, kept apart so the two
predictors are compared like for like. They do not share a sign convention: SpliceAI's four deltas are
magnitudes (`DS_AG`/`DS_AL`/`DS_DG`/`DS_DL`, all >= 0), while Pangolin signs a loss negative (`DS_SL`).
Every delta here is oriented the same way — larger = stronger predicted effect of that kind — so the
Pangolin loss is `-DS_SL`. A delta is `None` when the predictor returned no score (the variant is
unscored, a valid result — not an error). The per-position deltas the model reasons over ride in `raw`
(both payloads). The backend wraps them in `PredictDeltasResponse` and stamps `retrieved_at`.

The Broad delta fields arrive as strings (`"0.83"`), so they are parsed to float here. The service
expects the `chr` prefix on the variant (`chr8-140300616-T-G`), which is added when absent.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

import httpx

from themis.services.evidence import errors

_SOURCE = 'Broad SpliceAI + Pangolin'
_SPLICEAI_HOST = 'https://spliceai-{hg}-xwkwwwxdwq-uc.a.run.app/spliceai/'
_PANGOLIN_HOST = 'https://pangolin-{hg}-xwkwwwxdwq-uc.a.run.app/pangolin/'
_SPLICEAI_GAIN_KEYS = ('DS_AG', 'DS_DG')
_SPLICEAI_LOSS_KEYS = ('DS_AL', 'DS_DL')
_PANGOLIN_GAIN_KEYS = ('DS_SG',)
_PANGOLIN_LOSS_KEYS = ('DS_SL',)
# Pangolin's loss delta is alt-minus-ref, so a loss is negative; negating it puts both predictors'
# loss on the same scale as their gain.
_PANGOLIN_LOSS_SIGN = -1.0

# The two verdicts these services state in their `error` string, each matched case-insensitively on a
# fragment because the rest of the string echoes the variant. `_UNSCORABLE` is the one that becomes
# evidence, so it is the set matched explicitly: anything outside both — a server-side fault, a
# reworded message, no `error` key at all — is a payload this adapter cannot read, and saying "no
# score here" about it would be inventing the SPL_PRD finding.
_UNPARSABLE = 'unable to parse variant'
_UNSCORABLE = ('did not return any scores', 'was unable to compute scores')


@dataclasses.dataclass(frozen=True)
class SpliceResult:
    """The splice-delta values for one variant, from both Broad predictors.

    Every delta is oriented so that a larger number is a stronger predicted effect of that kind,
    across both predictors; `None` means the predictor returned no score for the variant.

    Attributes:
        spliceai_gain: `max(DS_AG, DS_DG)` — the stronger acceptor/donor gain.
        spliceai_loss: `max(DS_AL, DS_DL)` — the stronger acceptor/donor loss.
        pangolin_gain: `DS_SG` — the splice-site gain.
        pangolin_loss: `-DS_SL` — the splice-site loss (Pangolin signs it negative in `raw`).
        raw: Both payloads verbatim under `spliceai` / `pangolin`, for the proto `Struct`.
        source: Provenance source label (both predictors).
        dataset_versions: The assembly the deltas are against (`GRCh38` or `GRCh37`).
        query: Both request URLs issued, for replay.
    """

    spliceai_gain: float | None
    spliceai_loss: float | None
    pangolin_gain: float | None
    pangolin_loss: float | None
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _hg(genome_build: str) -> str:
    if genome_build == 'GRCh38':
        return '38'
    if genome_build == 'GRCh37':
        return '37'
    raise ValueError(f'unsupported genome build {genome_build!r}; expected GRCh38 or GRCh37')


def _oriented(score: Mapping[str, object], delta_keys: Sequence[str], sign: float) -> list[float]:
    """The score's `delta_keys` deltas, each multiplied by `sign`.

    Raises:
        ValueError: If an oriented delta comes out negative. Both predictors' conventions make that
            impossible, so it means the payload no longer follows the one assumed here — and
            negating a magnitude would turn a strong effect into a strong-looking absence.
    """
    deltas: list[float] = []
    for key in delta_keys:
        value = score.get(key)
        if not isinstance(value, (str, int, float)):
            continue
        oriented = sign * float(value)
        if oriented < 0:
            raise ValueError(
                f'{key}={value!r} contradicts the sign convention this adapter reads Broad deltas by '
                f'(SpliceAI deltas are magnitudes; Pangolin signs a loss negative)'
            )
        deltas.append(oriented)
    return deltas


def _max_delta(scores: Sequence[object], delta_keys: Sequence[str], *, sign: float = 1.0) -> float | None:
    """Max over scores of the strongest `delta_keys` delta; `None` when no score carries one.

    Args:
        scores: The predictor's `scores` array (one entry per scored transcript).
        delta_keys: The delta fields the maximum is taken over.
        sign: Multiplied into each delta before the maximum — `-1.0` for a predictor that signs the
            effect negative, so the result grows with the strength of the effect either way.

    Returns:
        The oriented maximum, or `None` when no score entry carries any of `delta_keys`.

    Raises:
        ValueError: If a delta's sign contradicts its predictor's convention.
    """
    per_score: list[float] = []
    for score in scores:
        if not isinstance(score, Mapping):
            continue
        deltas = _oriented(score, delta_keys, sign)
        if deltas:
            per_score.append(max(deltas))
    return max(per_score) if per_score else None


def _scores(payload: object, *, predictor: str, variant: str) -> Sequence[object]:
    """The `scores` list of a Broad response, or a raise when the call did not score.

    A missing `scores` array is the service's error shape, and the `error` string beside it is the
    only thing that says which kind: an id it could not parse ("Unable to parse variant: …") is the
    caller's, where a position it could not score is the SPL_PRD answer. Both arrive inside a 200, so
    reading the string is what keeps a typo from being returned as "this position is unscorable" —
    and never retried, an unscorable position being a settled answer. An empty array is a valid
    "unscored" result and passes through.

    Raises:
        errors.InvalidRequestError: If the service could not parse `variant`.
        errors.UnknownVariantError: If it scored nothing at a position it did parse.
        ValueError: If the payload carries neither, which is not an answer about the variant — an
            internal error, a reworded verdict, a missing `error` key. Defaulting to "no score" here
            would turn any of them into evidence, and a reworded message into a silent one.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f'{predictor} returned a non-object payload for {variant!r}')
    scores = payload.get('scores')
    if isinstance(scores, list):
        return scores
    reported = payload.get('error')
    if not isinstance(reported, str):
        raise ValueError(f'{predictor} returned neither scores nor an error for {variant!r}: {payload}')
    lowered = reported.lower()
    if _UNPARSABLE in lowered:
        raise errors.InvalidRequestError(f'{predictor} could not parse {variant!r}: {reported}')
    if any(verdict in lowered for verdict in _UNSCORABLE):
        raise errors.UnknownVariantError(f'{predictor} returned no scores for {variant!r}: {reported}')
    raise ValueError(f'{predictor} reported an error this adapter cannot place for {variant!r}: {reported}')


async def _fetch(
    http_client: httpx.AsyncClient, url: str, params: Mapping[str, str | int]
) -> tuple[dict[str, object], str]:
    """GET one predictor's score for a variant.

    Not on ``errors.raise_for_status``; the taxonomy note in ``docs/design/evidence-interfaces.md`` says why.
    A variant-level problem never reaches here — it arrives inside a 200, and ``_scores`` reads it.
    """
    response = await http_client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f'Broad splice service returned a non-object payload from {url}')
    return payload, str(response.request.url)


async def fetch_splice(variant: str, genome_build: str, *, http_client: httpx.AsyncClient) -> SpliceResult:
    """Score one variant against Broad SpliceAI and Pangolin.

    Args:
        variant: The variant as `chr-pos-ref-alt` (the `chr` prefix is added if absent).
        genome_build: `GRCh38` or `GRCh37`.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The parsed `SpliceResult`.

    Raises:
        httpx.HTTPStatusError: If either service returns a non-2xx status.
        ValueError: If either response is not an object, or a delta's sign contradicts its
            predictor's convention.
        errors.InvalidRequestError: If either service could not parse `variant`.
        errors.UnknownVariantError: If either scored nothing at a position it did parse.
    """
    hg = _hg(genome_build)
    normalized = variant if variant.startswith('chr') else f'chr{variant}'
    spliceai_payload, spliceai_query = await _fetch(
        http_client,
        _SPLICEAI_HOST.format(hg=hg),
        {'variant': normalized, 'hg': hg, 'distance': 50, 'mask': 0},
    )
    pangolin_payload, pangolin_query = await _fetch(
        http_client,
        _PANGOLIN_HOST.format(hg=hg),
        {'variant': normalized, 'hg': hg},
    )
    spliceai_scores = _scores(spliceai_payload, predictor='SpliceAI', variant=normalized)
    pangolin_scores = _scores(pangolin_payload, predictor='Pangolin', variant=normalized)
    return SpliceResult(
        spliceai_gain=_max_delta(spliceai_scores, _SPLICEAI_GAIN_KEYS),
        spliceai_loss=_max_delta(spliceai_scores, _SPLICEAI_LOSS_KEYS),
        pangolin_gain=_max_delta(pangolin_scores, _PANGOLIN_GAIN_KEYS),
        pangolin_loss=_max_delta(pangolin_scores, _PANGOLIN_LOSS_KEYS, sign=_PANGOLIN_LOSS_SIGN),
        raw={'spliceai': spliceai_payload, 'pangolin': pangolin_payload},
        source=_SOURCE,
        dataset_versions=(genome_build,),
        query=f'{spliceai_query} | {pangolin_query}',
    )
