"""Ensembl VEP REST adapter: molecular consequence + the predictor scores that ride in `raw`.

One VEP call carries several SVCv4 lines: the `most_severe_consequence` SO term is the routing key
(mapped here onto the `Consequence` enum), and the per-transcript predictor scores / `spliceai` /
`mane_select` plus a colocated ClinVar/gnomAD snapshot ride in the payload for the model to read. This
adapter types only the routing consequence and returns the whole variant annotation verbatim as `raw`;
the backend wraps it in `AnnotateResponse` and stamps `retrieved_at`.

GRCh38 is the default host; GRCh37 uses the `grch37.rest.ensembl.org` host (MANE is GRCh38-only, as
is AlphaMissense). The per-transcript fields the contract promises are asked for on every call
through the option set below — Ensembl emits none of them by default — and a requested predictor
rides on top in one of two wire forms, which is why the accepted set is a table here rather than
caller text. Ensembl answers an option or flag it does not recognise with a 200 that simply lacks
the field, so a name this table has no form for is refused rather than sent: sent, it would come
back indistinguishable from the variant having no score.

The annotation carries no version of its own, so the host's `/info/software` release is fetched
alongside and stamped into `dataset_versions` beside the assembly: an annotation is reproducible only
against the VEP release that produced it, and the transcript set it names moves with that release.
"""

from __future__ import annotations

import asyncio
import dataclasses
import urllib.parse
from collections.abc import Mapping, Sequence

import httpx2

from themis.evidence.models import evidence_pb2
from themis.services.evidence import errors, hgvs

_SOURCE = 'Ensembl VEP REST'
_RPC = 'Vep'
_GRCH38_HOST = 'https://rest.ensembl.org'
_GRCH37_HOST = 'https://grch37.rest.ensembl.org'

# The VEP REST options producing the per-transcript fields this adapter's contract promises:
# `hgvs` -> hgvsc/hgvsp, `numbers` -> exon/intron, `mane` -> mane/mane_select/mane_plus_clinical,
# `canonical` -> canonical. Ensembl answers an option it does not recognise with a 200 that simply
# lacks the field, so a misspelling here is invisible in the response.
_ANNOTATION_OPTIONS: Mapping[str, int] = {'hgvs': 1, 'numbers': 1, 'canonical': 1, 'mane': 1}

# Scores VEP serves as first-class fields: `?<name>=1` yields a nested object (`alphamissense`,
# `spliceai`), a scalar (`revel`) or a field pair (`cadd_phred` / `cadd_raw`). Ensembl documents
# REVEL as non-commercial use only; this project's use is academic / non-profit, which that grant
# covers, and REVEL is admitted on that footing.
_FIRST_CLASS_PREDICTORS = frozenset({'AlphaMissense', 'CADD', 'REVEL', 'SpliceAI'})

# Scores with no flag of their own, reached through the dbNSFP plugin by naming a column: `?BayesDel=1`
# is a 200 with the score absent. `BayesDel` resolves to the no-allele-frequency build — the one
# ClinGen calibrated, and the only one with a GRCh38 release. VEST4 is the one predictor SM6 approves
# that neither table holds: Ensembl's dbNSFP build answers `VEST4_score` with the string
# "invalid_field" on every variant.
_DBNSFP_COLUMNS: Mapping[str, str] = {
    'BayesDel': 'BayesDel_noAF_score',
    'ESM1b': 'ESM1b_score',
    'MutPred2': 'MutPred2_score',
    'VARITY_R': 'VARITY_R_score',
}

ACCEPTED_PREDICTORS: frozenset[str] = _FIRST_CLASS_PREDICTORS | frozenset(_DBNSFP_COLUMNS)

# dbNSFP writes a column's per-transcript values as one comma-joined string, "." where it holds
# none; this asks VEP to keep the entries matching the annotated transcript.
_TRANSCRIPT_MATCH = 'transcript_match=1'

# VEP's `most_severe_consequence` SO term -> the SVCv4 routing enum. A term not listed here (e.g. a
# structural or non-coding term VEP emits) maps to CONSEQUENCE_UNSPECIFIED, not a wrong routing key.
_SO_TERM_TO_CONSEQUENCE: Mapping[str, int] = {
    'missense_variant': evidence_pb2.CONSEQUENCE_MISSENSE,
    'stop_gained': evidence_pb2.CONSEQUENCE_NONSENSE,
    'frameshift_variant': evidence_pb2.CONSEQUENCE_FRAMESHIFT,
    'splice_acceptor_variant': evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE,
    'splice_donor_variant': evidence_pb2.CONSEQUENCE_CANONICAL_SPLICE,
    'intron_variant': evidence_pb2.CONSEQUENCE_INTRONIC,
    'synonymous_variant': evidence_pb2.CONSEQUENCE_SYNONYMOUS,
    'inframe_insertion': evidence_pb2.CONSEQUENCE_INFRAME_INDEL,
    'inframe_deletion': evidence_pb2.CONSEQUENCE_INFRAME_INDEL,
    'start_lost': evidence_pb2.CONSEQUENCE_START_LOST,
    'stop_lost': evidence_pb2.CONSEQUENCE_STOP_LOST,
}


@dataclasses.dataclass(frozen=True)
class VepResult:
    """The VEP annotation for one variant.

    Attributes:
        most_severe_consequence: The routing consequence as a `Consequence` enum value (int);
            `CONSEQUENCE_UNSPECIFIED` for an SO term outside the SVCv4 routing set.
        gene_symbol: The canonical transcript's HGNC symbol, or empty if VEP carried none.
        hgnc_id: The canonical transcript's HGNC id (`HGNC:nnnn`), or empty if VEP carried none
            (never fabricated — an empty id signals the downstream reference-table key is unknown).
        raw: The full VEP annotation object for the variant, for the proto `Struct`.
        source: Provenance source label.
        dataset_versions: The VEP release and the assembly it was run against, e.g.
            ``("VEP 116", "GRCh38")``.
        query: The exact request URL issued, for replay.
    """

    most_severe_consequence: int
    gene_symbol: str
    hgnc_id: str
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def consequence_for_so_term(so_term: str) -> int:
    """Map a VEP `most_severe_consequence` SO term onto the `Consequence` enum value."""
    return _SO_TERM_TO_CONSEQUENCE.get(so_term, evidence_pb2.CONSEQUENCE_UNSPECIFIED)


def accepted_predictors(rpc: str, requested: Sequence[str]) -> list[str]:
    """The requested predictor names, held to the set this adapter knows a wire form for.

    Args:
        rpc: The rpc's name, opening the refusal message.
        requested: The predictor names the request carries.

    Returns:
        `requested` as a list, when every name is one of `ACCEPTED_PREDICTORS`.

    Raises:
        errors.InvalidRequestError: Naming the ones that are not. Ensembl ignores a flag it does not
            recognise, so an unchecked name reaches a 200 with the score absent — which reads as the
            variant having none rather than as a request that was never made.
    """
    unknown = sorted({name for name in requested if name not in ACCEPTED_PREDICTORS})
    if unknown:
        raise errors.InvalidRequestError(f'{rpc} takes predictors from {sorted(ACCEPTED_PREDICTORS)}; got {unknown}')
    return list(requested)


def _dbnsfp_columns(predictors: Sequence[str]) -> list[str]:
    """The dbNSFP columns `predictors` names, in request order, deduped."""
    return list(dict.fromkeys(_DBNSFP_COLUMNS[name] for name in predictors if name in _DBNSFP_COLUMNS))


def _predictor_params(predictors: Sequence[str]) -> dict[str, str | int]:
    """The query parameters `predictors` becomes: a flag each, plus one `dbNSFP` naming the columns."""
    params: dict[str, str | int] = {name: 1 for name in predictors if name in _FIRST_CLASS_PREDICTORS}
    columns = _dbnsfp_columns(predictors)
    if columns:
        params['dbNSFP'] = ','.join([_TRANSCRIPT_MATCH, *columns])
    return params


def _dbnsfp_value(column: str, stated: object, transcript: str) -> float | None:
    """One dbNSFP column's value for one transcript consequence, or None where dbNSFP holds none.

    Raises:
        ValueError: If the column still names several different values after `transcript_match`, or
            names one that is not a number. Choosing among several would be a guess about which
            transcript the score belongs to, and the score decides MIS_PRD.
    """
    if isinstance(stated, bool) or not isinstance(stated, int | float | str):
        raise ValueError(f'VEP returned a {type(stated).__name__} for {column} on {transcript}')
    if not isinstance(stated, str):
        return float(stated)
    values = {part.strip() for part in stated.split(',')} - {'.', ''}
    if not values:
        return None
    if len(values) > 1:
        raise ValueError(
            f'VEP returned {len(values)} different {column} values for {transcript} ({stated!r}); '
            f'{_TRANSCRIPT_MATCH} left the column unresolved'
        )
    (value,) = values
    try:
        return float(value)
    except ValueError as e:
        raise ValueError(f'VEP returned a non-numeric {column} for {transcript}: {value!r}') from e


def _resolved_consequence(consequence: Mapping[str, object], columns: Sequence[str]) -> dict[str, object]:
    resolved = dict(consequence)
    transcript = str(consequence.get('transcript_id', '<unnamed transcript>'))
    for column in columns:
        key = column.lower()  # VEP lower-cases a dbNSFP column name in its output
        if key not in resolved:
            continue
        value = _dbnsfp_value(column, resolved[key], transcript)
        if value is None:
            # dbNSFP's "." is an absence; left in place it reads as a score.
            del resolved[key]
        else:
            resolved[key] = value
    return resolved


def _with_resolved_dbnsfp(annotation: Mapping[str, object], columns: Sequence[str]) -> dict[str, object]:
    """`annotation` with each requested dbNSFP column resolved to one value per transcript."""
    consequences = annotation.get('transcript_consequences')
    if not columns or not isinstance(consequences, list):
        return dict(annotation)
    resolved = [_resolved_consequence(c, columns) if isinstance(c, Mapping) else c for c in consequences]
    return {**annotation, 'transcript_consequences': resolved}


def _carries_term(consequence: Mapping[str, object], so_term: object) -> bool:
    terms = consequence.get('consequence_terms')
    return isinstance(so_term, str) and isinstance(terms, list) and so_term in terms


def _canonical_consequence(
    consequences: list[Mapping[str, object]], most_severe: object
) -> Mapping[str, object] | None:
    """The transcript consequence that names the variant's gene.

    Prefers a MANE/canonical-flagged entry, else the one carrying the ``most_severe_consequence``
    term, else the first. Every entry for a coding variant shares the gene, so this only
    disambiguates ties.
    """
    for predicate in (
        lambda c: bool(c.get('mane_select')),
        lambda c: bool(c.get('canonical')),
        lambda c: _carries_term(c, most_severe),
    ):
        for consequence in consequences:
            if predicate(consequence):
                return consequence
    return consequences[0] if consequences else None


def _canonical_gene(annotation: Mapping[str, object]) -> tuple[str, str]:
    """The `(gene_symbol, hgnc_id)` of the annotation's canonical transcript consequence.

    Either element is empty when VEP did not carry it; the hgnc_id is never fabricated.
    """
    raw_consequences = annotation.get('transcript_consequences')
    if not isinstance(raw_consequences, list):
        return '', ''
    consequences = [c for c in raw_consequences if isinstance(c, Mapping)]
    canonical = _canonical_consequence(consequences, annotation.get('most_severe_consequence'))
    if canonical is None:
        return '', ''
    gene_symbol = canonical.get('gene_symbol')
    hgnc_id = canonical.get('hgnc_id')
    return (gene_symbol if isinstance(gene_symbol, str) else ''), (hgnc_id if isinstance(hgnc_id, str) else '')


def _host(genome_build: str) -> str:
    if genome_build == 'GRCh38':
        return _GRCH38_HOST
    if genome_build == 'GRCh37':
        return _GRCH37_HOST
    raise ValueError(f'unsupported genome build {genome_build!r}; expected GRCh38 or GRCh37')


def _dataset_versions(software: object, genome_build: str) -> tuple[str, ...]:
    """The releases the annotation rests on: the host's VEP release and the assembly queried.

    Args:
        software: The parsed `/info/software` body — `{"release": 116}`.
        genome_build: The assembly the annotation was run against.

    Returns:
        e.g. ``("VEP 116", "GRCh38")``.

    Raises:
        ValueError: If the body carries no integer `release`. The assembly alone does not reproduce
            an annotation, so there is no partial stamp to fall back to.
    """
    release = software.get('release') if isinstance(software, Mapping) else None
    if not isinstance(release, int):
        raise ValueError(f'Ensembl /info/software returned no integer release: {software!r}')
    return (f'VEP {release}', genome_build)


def parse_vep(
    annotation: Mapping[str, object], *, dataset_versions: tuple[str, ...], query: str, dbnsfp_columns: Sequence[str]
) -> VepResult:
    """Parse one VEP variant annotation into the routing consequence + raw payload.

    Args:
        annotation: The first (and only) element of the VEP response list — the variant's annotation.
        dataset_versions: The VEP release and assembly, carried into provenance.
        query: The exact request URL issued, carried into provenance for replay.
        dbnsfp_columns: The dbNSFP columns the request asked for, each resolved to one value per
            transcript consequence. Named rather than detected, because the payload carries other
            comma-joined fields (`ensembl_transcriptid`) that must be left as they are.

    Returns:
        The parsed `VepResult`.

    Raises:
        ValueError: If the annotation carries no `most_severe_consequence` (a malformed response),
            or a dbNSFP column resolves to something other than one value for its transcript.
    """
    so_term = annotation.get('most_severe_consequence')
    if not isinstance(so_term, str):
        raise ValueError('VEP annotation has no most_severe_consequence')
    gene_symbol, hgnc_id = _canonical_gene(annotation)
    return VepResult(
        most_severe_consequence=consequence_for_so_term(so_term),
        gene_symbol=gene_symbol,
        hgnc_id=hgnc_id,
        raw=_with_resolved_dbnsfp(annotation, dbnsfp_columns),
        source=_SOURCE,
        dataset_versions=dataset_versions,
        query=query,
    )


async def fetch_vep(
    variant: str, predictors: Sequence[str], genome_build: str, *, http_client: httpx2.AsyncClient
) -> VepResult:
    """Annotate one variant via Ensembl VEP REST (HGVS endpoint).

    Args:
        variant: The HGVS expression to annotate, over a reference sequence that names its assembly;
            ClinVar's decorations are tolerated and stripped before the URL is built.
        predictors: The predictor scores to ask for, from `ACCEPTED_PREDICTORS` — each reaching the
            wire as its own flag or as a dbNSFP column, whichever form Ensembl serves it under.
            Additive to the per-transcript options every call carries; an empty list still gets
            HGVS, exon numbers and MANE flags.
        genome_build: `GRCh38` (default host) or `GRCh37` (the `grch37` host).
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The parsed `VepResult`.

    Raises:
        errors.InvalidRequestError: If `variant` is not such an expression, if `predictors` names one
            this adapter has no wire form for, or if Ensembl rejects the request.
        httpx2.HTTPStatusError: If VEP returns a 429 or a 5xx, or if `/info/software` — which carries
            no caller input, so its failure is never a refusal — returns any non-2xx.
        ValueError: If the response is not a non-empty JSON list of annotation objects, if
            `/info/software` names no release, or if a dbNSFP column does not resolve to one value.
    """
    variant = hgvs.accepted_hgvs(_SOURCE, variant)
    requested = accepted_predictors(_RPC, predictors)
    host = _host(genome_build)
    url = f'{host}/vep/human/hgvs/{urllib.parse.quote(variant, safe="")}'
    params: dict[str, str | int] = {**_ANNOTATION_OPTIONS, **_predictor_params(requested)}
    headers = {'Content-Type': 'application/json'}
    response, software = await asyncio.gather(
        http_client.get(url, params=params, headers=headers),
        http_client.get(f'{host}/info/software', headers=headers),
    )
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'{variant!r}')
    software.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise ValueError(f'VEP returned no annotation for {variant!r}')
    annotation = payload[0]
    if not isinstance(annotation, dict):
        raise ValueError(f'VEP returned a non-object annotation for {variant!r}')
    return parse_vep(
        annotation,
        dataset_versions=_dataset_versions(software.json(), genome_build),
        query=str(response.request.url),
        dbnsfp_columns=_dbnsfp_columns(requested),
    )
