"""Which calibrated predictor a gene's MIS_PRD score must come from, and the two calls it governs.

SM6 bans choosing a predictor per variant, not per gene: it encourages a distinct predictor for a
specific gene provided the choice is made before any VBC is evaluated (SM6 §6). So the choice is
data: a default plus one entry per gene the licence has been exercised for, held in
`data/predictor_policy.json` beside the scoring reference and read at classification time to learn
which score to request. The expert work is done offline against the calibration
literature and then frozen; classification time only reads the answer.

The API is shaped so shopping is not expressible: a caller names ONE gene and gets ONE `Selection`,
whose predictor is the only one `predictors.predictor_points` may then be handed. There is no entry
point taking several predictors, and none returning a ranking.

The two calls a selection governs live here for the same reason. `annotate_request` asks `Vep.Annotate`
for that predictor alone — a second one is a different SVCv4 line, never a second opinion on this one
— and `mis_prd_from_vep` bins that predictor's score off the answer, at the key VEP serves it under.
Between them the single-predictor guarantee is the library's rather than the rpc's, which will serve
any predictor on its allowlist.

An entry's identity is its HGNC id, not its symbol: a symbol can be retired and reassigned, and a
frozen per-gene choice that followed a symbol would move to a different gene without anything
failing. The symbol is carried too and indexed, because that is the key a caller has to hand — VEP
returns both `gene_symbol` and `hgnc_id` on each transcript consequence, and `for_hgnc_id` is the
lookup that survives a rename.

A gene the policy names no entry for gets the default. That is what a default is for, so it is not
a failure — but the resolution is reported rather than assumed: every `Selection` carries the entry
that decided it, its rationale, its source and the policy version, so a run records which predictor
it used and on whose authority. A frozen choice nothing can audit is not frozen in any useful sense.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import json
import pathlib
import re
from collections.abc import Sequence

from themis.rpc import vep_pb2
from themis.svcv4 import payload, predictors, provenance

_DEFAULT_DATA = pathlib.Path(__file__).parent / 'data' / 'predictor_policy.json'

# HGNC ids are "HGNC:" plus the numeric id, which is what every source the repo reads them from
# writes (ClinGen's dosage and validity tables, VEP's per-transcript `hgnc_id`).
_HGNC_ID = re.compile(r'HGNC:\d{1,7}')


class PredictorPolicyError(Exception):
    """The policy file is missing, malformed, or names something it cannot mean."""


@dataclasses.dataclass(frozen=True)
class Gene:
    """A gene the policy holds an entry for, under both keys that entry was written with."""

    hgnc_id: str
    symbol: str


@dataclasses.dataclass(frozen=True)
class Selection:
    """The predictor one gene's MIS_PRD score must come from, and what fixed that choice.

    `gene` names the entry that decided it, and is None where the policy holds no entry for the
    gene asked about, so the default applied.
    """

    predictor: predictors.Predictor
    gene: Gene | None
    rationale: str
    source: str
    version: datetime.date


class Policy:
    """The loaded predictor policy: one default, plus the genes it names an exception for.

    Maintains one invariant the lookups rest on: no HGNC id and no symbol names two entries, so a
    resolution is never the first of several answers. Symbols are matched case-insensitively, since
    a caller handing `pkd1` must not quietly fall through to the default.
    """

    def __init__(self, *, version: datetime.date, default: Selection, entries: Sequence[Selection]) -> None:
        self._version = version
        self._default = default
        self._by_hgnc_id: dict[str, Selection] = {}
        self._by_symbol: dict[str, Selection] = {}
        for entry in entries:
            if entry.gene is None:
                raise PredictorPolicyError('a per-gene policy entry must name its gene')
            self._claim(self._by_hgnc_id, entry.gene.hgnc_id, entry, 'HGNC id')
            self._claim(self._by_symbol, entry.gene.symbol.upper(), entry, 'symbol')

    @staticmethod
    def _claim(index: dict[str, Selection], key: str, entry: Selection, kind: str) -> None:
        if key in index:
            raise PredictorPolicyError(f'two policy entries name the {kind} {key!r}')
        index[key] = entry

    @property
    def version(self) -> datetime.date:
        """The date this policy was frozen, stamped on every `Selection` it resolves."""
        return self._version

    def for_hgnc_id(self, hgnc_id: str) -> Selection:
        """The predictor selected for the gene with this HGNC id, or the default for one with no entry.

        Args:
            hgnc_id: The gene's HGNC id, e.g. `HGNC:9008`. The stable key: a symbol can be retired
                and reassigned, an id cannot.

        Returns:
            The `Selection`, its `gene` set when an entry decided it and None when the default did.

        Raises:
            ValueError: If `hgnc_id` is empty or not an HGNC id. An absent gene is a missing input,
                not a gene the policy holds no entry for.
        """
        stated = hgnc_id.strip()
        if _HGNC_ID.fullmatch(stated) is None:
            raise ValueError(f'predictor policy takes an HGNC id, e.g. HGNC:9008; got {hgnc_id!r}')
        return self._by_hgnc_id.get(stated, self._default)

    def for_symbol(self, symbol: str) -> Selection:
        """The predictor selected for this gene symbol, or the default for one with no entry.

        Args:
            symbol: The gene's HGNC symbol, e.g. `PKD1`, as `Variant.Normalize` and the gene-scoped
                evidence rpcs take it. Matched case-insensitively.

        Returns:
            The `Selection`, its `gene` set when an entry decided it and None when the default did.

        Raises:
            ValueError: If `symbol` is empty — a missing gene, not one without an entry.
        """
        stated = symbol.strip()
        if not stated:
            raise ValueError('predictor policy takes an HGNC symbol; got an empty gene')
        return self._by_symbol.get(stated.upper(), self._default)


def _as_dict(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PredictorPolicyError(f'expected an object at {context}, got {type(value).__name__}')
    return value


def _require_text(mapping: dict[str, object], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PredictorPolicyError(f'{context}.{key} must be a non-empty string; got {value!r}')
    return value.strip()


def _require_predictor(mapping: dict[str, object], context: str) -> predictors.Predictor:
    """The entry's predictor, held to one this build can actually score.

    An entry naming a predictor with no threshold table would resolve happily and then fail on the
    variant it was written for, on a gene someone chose it for deliberately.
    """
    name = _require_text(mapping, 'predictor', context)
    try:
        predictor = predictors.Predictor(name)
    except ValueError as e:
        accepted = ', '.join(p.value for p in predictors.Predictor)
        raise PredictorPolicyError(f"{context}.predictor names {name!r}, not one of SM6's: {accepted}") from e
    if not predictors.implements(predictor):
        raise PredictorPolicyError(f'{context}.predictor names {name!r}, which has no SVCv4 threshold table')
    return predictor


def _build_selection(
    payload: dict[str, object], context: str, *, version: datetime.date, gene: Gene | None
) -> Selection:
    return Selection(
        predictor=_require_predictor(payload, context),
        gene=gene,
        rationale=_require_text(payload, 'rationale', context),
        source=_require_text(payload, 'source', context),
        version=version,
    )


def _build_entries(genes: object, version: datetime.date) -> list[Selection]:
    if not isinstance(genes, list):
        raise PredictorPolicyError(f'expected a list at genes, got {type(genes).__name__}')
    entries = []
    for raw in genes:
        entry = _as_dict(raw, 'genes')
        hgnc_id = _require_text(entry, 'hgnc_id', 'genes')
        if _HGNC_ID.fullmatch(hgnc_id) is None:
            raise PredictorPolicyError(f'genes.hgnc_id must be an HGNC id, e.g. HGNC:9008; got {hgnc_id!r}')
        context = f'genes[{hgnc_id}]'
        gene = Gene(hgnc_id=hgnc_id, symbol=_require_text(entry, 'symbol', context))
        entries.append(_build_selection(entry, context, version=version, gene=gene))
    return entries


def _build_version(payload: dict[str, object]) -> datetime.date:
    """The freeze date, parsed rather than carried as text so "versioned" is a checkable property."""
    stated = _require_text(payload, 'version', 'policy')
    try:
        return datetime.date.fromisoformat(stated)
    except ValueError as e:
        raise PredictorPolicyError(f'policy.version must be an ISO date; got {stated!r}') from e


def load_policy(path: pathlib.Path | None = None) -> Policy:
    """Load and validate the frozen predictor policy.

    Args:
        path: Policy JSON to load; defaults to the packaged `data/predictor_policy.json`.

    Returns:
        The validated `Policy`.

    Raises:
        PredictorPolicyError: If the file is missing or malformed, names a predictor outside SM6's
            seven or one with no threshold table, or names one gene twice.
    """
    source = path or _DEFAULT_DATA
    try:
        with source.open() as f:
            loaded = json.load(f)
    except FileNotFoundError as e:
        raise PredictorPolicyError(f'predictor policy not found: {source}') from e
    except json.JSONDecodeError as e:
        raise PredictorPolicyError(f'invalid JSON in {source}') from e

    payload = _as_dict(loaded, 'policy')
    version = _build_version(payload)
    default = _build_selection(_as_dict(payload.get('default'), 'default'), 'default', version=version, gene=None)
    return Policy(version=version, default=default, entries=_build_entries(payload.get('genes'), version))


@dataclasses.dataclass(frozen=True)
class PredictorScore:
    """The MIS_PRD finding: the policy's predictor, its score, and the bin it fell in.

    A `classify.ScoredCode`. `points` is None wherever the predictor has no score for the
    transcript, which the framework makes no MIS_PRD determination on — distinct from a score that
    binned to 0.0, which is a determination that the substitution is uninformative.

    Attributes:
        selection: The entry that fixed the predictor, so a run records which predictor it used and
            on whose authority.
        score: The predictor's score for the substitution; None where it has none for this
            transcript.
        points: The MIS_PRD initial points, pre-matrix; None wherever `score` is.
        releases: The Ensembl releases behind the annotation.
    """

    selection: Selection
    score: decimal.Decimal | None
    points: decimal.Decimal | None
    releases: tuple[provenance.Release, ...] = ()

    @property
    def code(self) -> str:
        """The evidence code these points are filed under."""
        return 'MIS_PRD'

    @property
    def derivation(self) -> str:
        """The predictor, the score, and which policy entry chose it.

        The entry is named rather than quoted: its rationale is the evidence behind a frozen choice
        and runs to a paragraph, which belongs on the `selection` a report reads, not in a tally line.
        """
        decided = 'the policy default' if self.selection.gene is None else f'the {self.selection.gene.hgnc_id} entry'
        stated = f'{decided}, policy {self.selection.version}'
        if self.score is None:
            return f'{self.selection.predictor.value} ({stated}) has no score for this transcript'
        return f'{self.selection.predictor.value} {self.score} ({stated})'


def annotate_request(selection: Selection, *, variant: str) -> vep_pb2.AnnotateRequest:
    """Build the `Vep.Annotate` request that asks for this gene's predictor and no other.

    Asking for a second predictor is asking about a different SVCv4 line, never a second opinion on
    this one, so the request carries exactly the selection's own. The per-transcript options ride on
    every call, so the HGVS, exon numbers and MANE flags come back regardless.

    Args:
        selection: The gene's entry, from `Policy.for_hgnc_id`.
        variant: The HGVS expression to annotate, over a reference sequence naming its assembly.

    Returns:
        The request.

    Raises:
        ValueError: If `variant` is empty — a missing input, which unchecked would come back as an
            annotation of nothing.
        NotImplementedError: If the selection names a predictor this build cannot bin. `load_policy`
            refuses such an entry, so this catches a `Selection` built by hand — and the rpc would
            refuse two of the seven anyway, serving them by neither wire form.
    """
    if not variant.strip():
        raise ValueError('Vep.Annotate takes an HGVS expression; got an empty variant')
    predictors.score_key(selection.predictor)
    return vep_pb2.AnnotateRequest(variant=variant.strip(), predictors=[selection.predictor.value])


def _consequence_for(raw: dict[str, object], transcript: str) -> dict[str, object]:
    """The transcript's own element of `raw.transcript_consequences`."""
    consequences = payload.at(raw, 'transcript_consequences')
    if not isinstance(consequences, list):
        raise ValueError(f'transcript_consequences carries a {type(consequences).__name__}, expected a list')
    wanted = transcript.split('.', 1)[0]
    annotated = []
    for element in consequences:
        if not isinstance(element, dict):
            raise ValueError(f'a transcript consequence is a {type(element).__name__}, expected an object')
        stated = element.get('transcript_id')
        if isinstance(stated, str) and stated.split('.', 1)[0] == wanted:
            annotated.append(element)
    if not annotated:
        stated_ids = sorted(
            str(element.get('transcript_id'))
            for element in consequences
            if isinstance(element, dict) and 'transcript_id' in element
        )
        raise ValueError(f'the annotation carries nothing for {transcript}; it annotates {stated_ids}')
    if len(annotated) > 1:
        raise ValueError(f'the annotation carries {len(annotated)} consequences for {transcript}; it must carry one')
    return annotated[0]


def mis_prd_from_vep(
    request: vep_pb2.AnnotateRequest,
    response: vep_pb2.AnnotateResponse,
    selection: Selection,
    *,
    transcript: str,
) -> PredictorScore:
    """Bin the policy's predictor score for one transcript off a `Vep.Annotate` response.

    Reads two paths of `raw`: the element of `transcript_consequences` whose `transcript_id` names
    the transcript, and this predictor's score key on it (`predictors.score_key`). The transcript is
    matched without its version run, since the annotation set names its own version of an accession.

    **An absent score key is the one absence that is an answer here**, and it reads as no MIS_PRD
    determination rather than as a broken payload: the rpc holds the predictor *names* to a closed
    set precisely so that a score VEP omits means the predictor has none for this transcript, rather
    than a name Ensembl silently ignored. A key stated as null reads the same way. But that
    guarantee is the **request's**, which is why the request is taken rather than assumed: a response
    fetched without this predictor carries no key for it either, and read against the selection alone
    it would delete MIS_PRD from every variant it was asked about.

    What neither covers is the key itself, which `vep.proto` does not state: `predictors.score_key`
    is where the two spellings are recorded, and a rename upstream would come back as no
    determination rather than as a failure. Pinning both keys in the contract, as `gnomad.proto`
    pins its paths, is what would close that.

    Args:
        request: The request the response answers, whose predictor list has to name the selection's.
        response: The rpc's answer.
        selection: The gene's policy entry — the predictor whose score is binned, and no other.
        transcript: The accession the score is wanted for.

    Returns:
        The `PredictorScore`, stamped with the Ensembl releases the response names.

    Raises:
        ValueError: If the request did not ask for the selection's predictor, if the payload carries
            no `transcript_consequences`, none for this transcript or more than one, if the score is
            not a number, or if it is outside the predictor's own published range. If the response
            states no provenance.
        NotImplementedError: If the policy's predictor has no threshold table here.
    """
    if selection.predictor.value not in request.predictors:
        raise ValueError(
            f'the request asked for {list(request.predictors)}, not {selection.predictor.value}; a response '
            'fetched without this predictor carries no score for it, which is not the predictor having none'
        )
    consequence = _consequence_for(payload.fields(response.raw), transcript)
    key = predictors.score_key(selection.predictor)
    score = payload.number(consequence, key) if key in consequence else None
    return PredictorScore(
        selection=selection,
        score=score,
        points=None if score is None else predictors.predictor_points(selection.predictor, score),
        releases=provenance.releases_of(response.provenance),
    )
