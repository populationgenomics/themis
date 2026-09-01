"""VariantValidator adapter: HGVS validation + MANE projection + both-build VCF loci.

VariantValidator validates an HGVS expression and projects it onto every requested transcript with the
predicted protein consequence and MANE flags, and it resolves the genomic locus on BOTH assemblies
(GRCh37 + GRCh38) as VCF `chr/pos/ref/alt`. The Resolve remap uses the projections + both-build loci
to build the shared join key (gnomAD ids are `chrom-pos-ref-alt`) and select transcripts. This adapter
returns those parsed fields plus the payload verbatim; the backend maps them and stamps `retrieved_at`.

Calls take seconds, so the request carries a generous per-call timeout (the injected client keeps the
backend's default for the faster upstreams).
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from collections.abc import Mapping, Sequence

import httpx2

from themis.rpc import variant_pb2
from themis.services.evidence import errors

_BASE_URL = 'https://rest.variantvalidator.org/VariantValidator/variantvalidator'
_SOURCE = 'VariantValidator'
_TIMEOUT_SECONDS = 60.0
_MAX_WARNING_DETAIL = 512


@dataclasses.dataclass(frozen=True)
class VcfLocus:
    """A variant's VCF coordinate on one assembly (fields kept as VariantValidator's strings)."""

    chrom: str
    pos: str
    ref: str
    alt: str


@dataclasses.dataclass(frozen=True)
class VariantValidatorResult:
    """The Resolve-remap inputs parsed from one VariantValidator response.

    Attributes:
        transcripts: One projection per transcript entry the response carried.
        grch37_vcf: The GRCh37 VCF locus, or `None` if the response carried none.
        grch38_vcf: The GRCh38 VCF locus, or `None` if the response carried none.
        gene: The HGNC gene symbol (from the first transcript entry).
        raw: The full VariantValidator payload verbatim, for the proto `Struct`.
        source: Provenance source label.
        dataset_versions: The VariantValidator and VVDB versions from the response metadata, one
            element each.
        query: The exact request URL issued, for replay.
    """

    transcripts: list[variant_pb2.TranscriptProjection]
    grch37_vcf: VcfLocus | None
    grch38_vcf: VcfLocus | None
    gene: str
    raw: dict[str, object]
    source: str
    dataset_versions: tuple[str, ...]
    query: str


def _projection(entry: Mapping[str, object], hgvs_c: str) -> variant_pb2.TranscriptProjection:
    protein = entry.get('hgvs_predicted_protein_consequence')
    hgvs_p = protein.get('tlr') if isinstance(protein, Mapping) else None
    annotations = entry.get('annotations')
    mane_select = mane_plus_clinical = False
    if isinstance(annotations, Mapping):
        mane_select = annotations.get('mane_select') is True
        mane_plus_clinical = annotations.get('mane_plus_clinical') is True
    return variant_pb2.TranscriptProjection(
        transcript=hgvs_c.split(':', 1)[0],
        hgvs_c=hgvs_c,
        hgvs_p=hgvs_p if isinstance(hgvs_p, str) else '',
        mane_select=mane_select,
        mane_plus_clinical=mane_plus_clinical,
        sources=[_SOURCE],
    )


def _vcf(build_loci: object) -> VcfLocus | None:
    """The `vcf` block of one `primary_assembly_loci` build, or `None` when malformed/absent."""
    if not isinstance(build_loci, Mapping):
        return None
    vcf = build_loci.get('vcf')
    if not isinstance(vcf, Mapping):
        return None
    chrom, pos, ref, alt = vcf.get('chr'), vcf.get('pos'), vcf.get('ref'), vcf.get('alt')
    if isinstance(chrom, str) and isinstance(pos, str) and isinstance(ref, str) and isinstance(alt, str):
        return VcfLocus(chrom=chrom, pos=pos, ref=ref, alt=alt)
    return None


def _validation_warnings(payload: Mapping[str, object]) -> list[str]:
    """Every `validation_warnings` line across the response's entries — why a validation failed."""
    lines: list[str] = []
    for key, entry in payload.items():
        if key in ('flag', 'metadata') or not isinstance(entry, Mapping):
            continue
        warnings = entry.get('validation_warnings')
        if isinstance(warnings, Sequence) and not isinstance(warnings, str):
            lines.extend(warning for warning in warnings if isinstance(warning, str))
    return lines


def _dataset_versions(metadata: object) -> tuple[str, ...]:
    if not isinstance(metadata, Mapping):
        return ()
    return tuple(
        value for key in ('variantvalidator_version', 'vvdb_version') if isinstance(value := metadata.get(key), str)
    )


def parse_variant_validator(payload: Mapping[str, object], *, query: str) -> VariantValidatorResult:
    """Parse a VariantValidator response into transcript projections + both-build VCF loci.

    The response is keyed by `hgvs_transcript_variant`, alongside a `flag` and `metadata`; each
    transcript entry carries the protein consequence, MANE annotations, and `primary_assembly_loci`.
    The genomic locus is the same across entries, so the VCF loci are taken from the first entry that
    carries them.

    Args:
        payload: The decoded VariantValidator response.
        query: The exact request URL issued, carried into provenance for replay.

    Returns:
        The parsed `VariantValidatorResult`.

    Raises:
        ValueError: If the response carries no transcript variant (a validation failure — the `flag`
            and the response's `validation_warnings` are surfaced in the message), not a fabricated
            empty projection.
    """
    grch37_vcf: VcfLocus | None = None
    grch38_vcf: VcfLocus | None = None
    gene = ''
    projections: list[variant_pb2.TranscriptProjection] = []
    for key, entry in payload.items():
        if key in ('flag', 'metadata') or not isinstance(entry, Mapping):
            continue
        hgvs_c = entry.get('hgvs_transcript_variant')
        if not isinstance(hgvs_c, str) or not hgvs_c:
            continue
        projections.append(_projection(entry, hgvs_c))
        loci = entry.get('primary_assembly_loci')
        if isinstance(loci, Mapping):
            grch37_vcf = grch37_vcf if grch37_vcf is not None else _vcf(loci.get('grch37'))
            grch38_vcf = grch38_vcf if grch38_vcf is not None else _vcf(loci.get('grch38'))
        if not gene:
            symbol = entry.get('gene_symbol')
            gene = symbol if isinstance(symbol, str) else ''
    if not projections:
        warnings = _validation_warnings(payload)
        # Truncated: the message becomes a gRPC trailer, and an over-limit trailer is dropped whole.
        detail = '; '.join(warnings)[:_MAX_WARNING_DETAIL] if warnings else 'no validation warnings returned'
        raise errors.UnknownVariantError(
            f'VariantValidator returned no transcript variant (flag={payload.get("flag")!r}): {detail}'
        )
    return VariantValidatorResult(
        transcripts=projections,
        grch37_vcf=grch37_vcf,
        grch38_vcf=grch38_vcf,
        gene=gene,
        raw=dict(payload),
        source=_SOURCE,
        dataset_versions=_dataset_versions(payload.get('metadata')),
        query=query,
    )


async def fetch_variant_validator(
    genome_build: str, variant: str, select_transcripts: str, *, http_client: httpx2.AsyncClient
) -> VariantValidatorResult:
    """Validate + project one variant via VariantValidator.

    Args:
        genome_build: The selected assembly, `GRCh38` or `GRCh37`.
        variant: The variant to validate (HGVS or a pseudo-VCF the service accepts).
        select_transcripts: Which transcripts to project — `mane`, `all`, or a specific `NM_` id.
        http_client: The async HTTP client (caller owns its lifecycle).

    Returns:
        The parsed `VariantValidatorResult`.

    Raises:
        errors.InvalidRequestError: If VariantValidator refuses the request (a non-429 4xx). A
            variant it cannot validate comes back 200 with `validation_warnings`, so this is the
            request itself being unacceptable, not the variant being unknown.
        httpx2.HTTPStatusError: If VariantValidator returns a 429 or a 5xx.
        errors.UnknownVariantError: If the response carries no transcript variant.
        ValueError: If the response is not a JSON object.
    """
    quoted = urllib.parse.quote(variant, safe='')
    url = f'{_BASE_URL}/{genome_build}/{quoted}/{select_transcripts}'
    response = await http_client.get(url, headers={'Accept': 'application/json'}, timeout=_TIMEOUT_SECONDS)
    errors.raise_for_status(response, upstream=_SOURCE, subject=f'{variant!r} on {genome_build}')
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f'VariantValidator returned a non-object payload for {variant!r}')
    return parse_variant_validator(payload, query=str(response.request.url))
