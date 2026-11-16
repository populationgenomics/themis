"""The gene_disease interface's port and its adapters.

The live adapter is the only one in the image whose build is not a constructor call: it loads the
four gene-disease reference dumps and the HGNC complete set from the shared resources bucket once,
at startup, so every request is an in-memory join. A missing object fails the build, which fails the
startup probe; the alternative is a revision that serves "this gene is not curated" for every gene.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import dataclasses
import datetime
from collections.abc import Mapping, Sequence
from typing import override

import httpx2

from themis.evidence.models import evidence_pb2
from themis.rpc import gene_disease_pb2
from themis.services.evidence import errors, fixtures, provenance
from themis.services.evidence.gene_disease import entities as gene_disease
from themis.services.evidence.upstreams import clingen_dosage, clingen_validity, gencc, hgnc, mondo, panelapp

_SECTION = 'describe_gene'
SECTIONS = frozenset({_SECTION})

# The gene-disease reference dumps the weekly refresh job writes to the resources bucket; loaded once
# at startup, keyed by HGNC id. Raw GenCC TSV + ClinGen CSVs, the transformed PanelApp JSON, all under
# the one dataset prefix the bucket's other datasets sit beside.
_DATASET_PREFIX = 'gene-disease'
_GENCC_OBJECT = f'{_DATASET_PREFIX}/gencc/submissions.tsv'
_VALIDITY_OBJECT = f'{_DATASET_PREFIX}/clingen/validity.csv'
_DOSAGE_OBJECT = f'{_DATASET_PREFIX}/clingen/dosage.csv'
_PANELAPP_OBJECT = f'{_DATASET_PREFIX}/panelapp/dump.json'
_REFRESHED_OBJECTS = (_GENCC_OBJECT, _VALIDITY_OBJECT, _DOSAGE_OBJECT, _PANELAPP_OBJECT)

# Every deployed environment's bucket must already hold a release before this names it: see
# docs/design/infrastructure.md.
_HGNC_VERSION = '2026-07-07'
_HGNC_OBJECT = f'reference/hgnc/{_HGNC_VERSION}/complete-set.tsv'

_REFERENCE_OBJECTS = (*_REFRESHED_OBJECTS, _HGNC_OBJECT)


class GeneDiseaseBackend(abc.ABC):
    """The gene-disease port: the seeded or composed curations, or `errors.UnknownVariantError`."""

    @abc.abstractmethod
    async def describe_gene(
        self, request: gene_disease_pb2.DescribeGeneRequest
    ) -> gene_disease_pb2.DescribeGeneResponse: ...


def gene_key(request: gene_disease_pb2.DescribeGeneRequest) -> str:
    """The fixture key for one gene-disease query: the gene's HGNC id, plus the disease term named.

    The gene's curated records are the same for every request, but the response's `resolution` is
    not — keying on the HGNC id alone would answer a request that named a term from a seed built for
    no term, which is the gene-scoped answer this rpc exists to stop giving.
    """
    if not request.mondo_id:
        return request.hgnc_id
    return f'{request.hgnc_id}:{request.mondo_id}'


class FixtureBackend(GeneDiseaseBackend):
    """In-memory backend answering from a seeded `{gene key: curations}` table."""

    def __init__(self, describe_gene: Mapping[str, gene_disease_pb2.DescribeGeneResponse]) -> None:
        self._describe_gene = describe_gene

    @override
    async def describe_gene(
        self, request: gene_disease_pb2.DescribeGeneRequest
    ) -> gene_disease_pb2.DescribeGeneResponse:
        return fixtures.lookup(self._describe_gene, gene_key(request), kind='gene_disease')


def fixture_backend_from_json(raw: str | None, *, var_name: str) -> FixtureBackend:
    """Build the offline backend from its fixture var, or `SystemExit`.

    Raises:
        SystemExit: The var is unset or malformed, or a seeded response names no gene: the contract
            never answers with an empty `gene_symbol`.
    """
    seeds = fixtures.sections_from_json(raw, var_name=var_name, sections=SECTIONS)
    seeded = fixtures.table(seeds, _SECTION, gene_disease_pb2.DescribeGeneResponse, var_name=var_name)
    for key, response in seeded.items():
        if not response.gene_symbol.strip():
            raise SystemExit(
                f'{var_name} section {_SECTION!r} entry {key!r} has no gene_symbol; DescribeGene names the '
                'gene of every answer'
            )
    return FixtureBackend(seeded)


@dataclasses.dataclass(frozen=True)
class ReferenceTables:
    """What the live adapter reads at startup, parsed once and keyed by HGNC id.

    The four gene-disease dumps it joins on, and the HGNC complete set that says which gene an id
    names.
    """

    validity: clingen_validity.ClinGenValidity
    dosage: clingen_dosage.ClinGenDosage
    gencc: gencc.GenCC
    panelapp: panelapp.PanelAppTable
    hgnc: hgnc.HgncCompleteSet


class LiveBackend(GeneDiseaseBackend):
    """The deployed backend: an in-memory join over the reference tables, MONDO reached only on demand."""

    def __init__(self, http_client: httpx2.AsyncClient, tables: ReferenceTables) -> None:
        self._http_client = http_client
        self._tables = tables

    @classmethod
    async def create(cls, *, http_client: httpx2.AsyncClient, resources_bucket: str) -> LiveBackend:
        """Load the reference tables from the bucket once and wire the backend to the shared client.

        Args:
            http_client: The image's shared client, which the MONDO closure call is issued on.
            resources_bucket: The shared resources bucket holding the gene-disease dataset and the
                reference mirror.

        Raises:
            RuntimeError: If a reference object is missing from the bucket.
            ValueError: If a reference object has an unexpected shape, or carries a gene under a
                blank symbol.
        """
        blobs = await asyncio.to_thread(_download_reference_blobs, resources_bucket)
        return cls(
            http_client,
            ReferenceTables(
                validity=clingen_validity.ClinGenValidity.from_bytes(blobs[_VALIDITY_OBJECT]),
                dosage=clingen_dosage.ClinGenDosage.from_bytes(blobs[_DOSAGE_OBJECT]),
                gencc=gencc.GenCC.from_bytes(blobs[_GENCC_OBJECT]),
                panelapp=panelapp.PanelAppTable.from_bytes(blobs[_PANELAPP_OBJECT]),
                hgnc=hgnc.HgncCompleteSet.from_bytes(blobs[_HGNC_OBJECT], version=_HGNC_VERSION),
            ),
        )

    @override
    async def describe_gene(
        self, request: gene_disease_pb2.DescribeGeneRequest
    ) -> gene_disease_pb2.DescribeGeneResponse:
        at = provenance.utcnow()
        approved = self._tables.hgnc.lookup(request.hgnc_id)
        sources = _GeneDiseaseSources(
            validity=self._tables.validity.lookup(request.hgnc_id),
            harmonised=self._tables.gencc.lookup(request.hgnc_id),
            dosage=self._tables.dosage.lookup(request.hgnc_id),
            panel=self._tables.panelapp.lookup(request.hgnc_id),
        )
        gene_symbol = self._gene_symbol(request.hgnc_id, approved, sources)
        curated = gene_disease.entities(sources.validity, sources.harmonised)
        raw, stamped = sources.raw_and_provenance(at)
        if approved is not None:
            stamped.insert(0, provenance.provenance(approved, at))

        # A gene with no curated record has nothing to resolve against and nothing to be wrong about:
        # `coverage` is the answer, and the gene-scoped signals still stand.
        resolution = None
        if request.mondo_id and curated:
            closure = await self._mondo_closure(curated, request)
            if closure is not None:
                raw['mondo_ancestors'] = closure.raw
                stamped.append(provenance.provenance(closure, at))
            ancestors = closure.ancestors if closure is not None else {}
            resolution = gene_disease.resolve(curated, request.mondo_id, ancestors)

        return gene_disease_pb2.DescribeGeneResponse(
            entities=curated,
            resolution=resolution,
            gene_scoped=sources.gene_scoped_signals(),
            coverage=sources.coverage(curated),
            gene_symbol=gene_symbol,
            previous_symbols=approved.previous_symbols if approved is not None else (),
            raw=provenance.struct(raw),
            provenance=stamped,
        )

    def _gene_symbol(self, hgnc_id: str, approved: hgnc.HgncResult | None, sources: _GeneDiseaseSources) -> str:
        """The symbol of the gene the id names: the one HGNC approves, else the one the tables record.

        The four tables are refreshed weekly and the HGNC set is pinned, so a table can carry a gene
        the set does not hold yet.

        Raises:
            errors.UnknownVariantError: If the id names no gene: the set approves no symbol under it
                and no table carries it.
        """
        if approved is not None:
            return approved.gene_symbol
        recorded = sources.gene_symbol()
        if recorded is None:
            raise errors.UnknownVariantError(
                f'no gene under {hgnc_id}: release {self._tables.hgnc.version} of the HGNC complete set approves '
                'no symbol for it and no gene-disease reference table carries it. An id HGNC approved after that '
                'release answers this way until a table carries the gene'
            )
        return recorded

    async def _mondo_closure(
        self, curated: Sequence[gene_disease_pb2.CuratedEntity], request: gene_disease_pb2.DescribeGeneRequest
    ) -> mondo.MondoClosureResult | None:
        """The MONDO closure the request's resolution needs, or None where it needs none.

        The ontology is reached only where the requested term is not itself curated, so an
        exact-term request stays the in-memory join the four reference tables are.
        """
        terms = gene_disease.terms_needing_closure(curated, request.mondo_id)
        if not terms:
            return None
        return await mondo.fetch_subclass_closure(terms, http_client=self._http_client)


type _Lookup = (
    clingen_validity.ClinGenValidityResult
    | gencc.GenCCResult
    | clingen_dosage.ClinGenDosageResult
    | panelapp.PanelAppResult
)


@dataclasses.dataclass(frozen=True)
class _GeneDiseaseSources:
    """One gene's four reference-table lookups, each None where its table does not carry the gene.

    Two of the four assert gene-disease validity per entity (ClinGen validity, GenCC) and two carry
    gene-level signals (ClinGen dosage, PanelApp), which is the split ``DescribeGeneResponse`` keeps.
    """

    validity: clingen_validity.ClinGenValidityResult | None
    harmonised: gencc.GenCCResult | None
    dosage: clingen_dosage.ClinGenDosageResult | None
    panel: panelapp.PanelAppResult | None

    def _held(self) -> list[_Lookup]:
        """The lookups that found the gene, in the order the response names its sources."""
        lookups = (self.validity, self.harmonised, self.dosage, self.panel)
        return [result for result in lookups if result is not None]

    def _holding_the_gene(self) -> list[str]:
        """The sources carrying the gene at all, whether or not they assert its validity."""
        return [result.source for result in self._held()]

    def gene_symbol(self) -> str | None:
        """The symbol the first table carrying the gene records for it; None where none carries it."""
        held = self._held()
        if not held:
            return None
        return held[0].gene_symbol

    def raw_and_provenance(self, at: datetime.datetime) -> tuple[dict[str, object], list[evidence_pb2.Provenance]]:
        """The per-source rows for ``raw``, and one ``Provenance`` per source that answered.

        Every source gets a ``raw`` key whether or not it carries the gene: a null there is the
        source stating it holds nothing, which an absent key would leave the caller guessing at.
        """
        raw: dict[str, object] = {}
        stamped: list[evidence_pb2.Provenance] = []
        for key, result in (
            ('clingen_validity', self.validity),
            ('gencc', self.harmonised),
            ('clingen_dosage', self.dosage),
            ('panelapp', self.panel),
        ):
            raw[key] = result.raw if result is not None else None
            if result is not None:
                stamped.append(provenance.provenance(result, at))
        if self.panel is not None:
            raw['panelapp_max_confidence'] = self.panel.max_confidence
        return raw, stamped

    def gene_scoped_signals(self) -> gene_disease_pb2.GeneScopedSignals:
        """The signals whose sources curate the gene rather than one of its disease entities."""
        signals = gene_disease_pb2.GeneScopedSignals(sources_holding_the_gene=self._holding_the_gene())
        if self.dosage is not None:
            signals.haploinsufficiency_score = self.dosage.haploinsufficiency_score
        if self.panel is not None:
            signals.mode_of_pathogenicity = self.panel.mode_of_pathogenicity
            signals.mode_of_inheritance = self.panel.mode_of_inheritance
            signals.mechanism_statements.extend(
                gene_disease_pb2.MechanismStatement(
                    source=self.panel.source, context=self.panel.panel_scope, text=comment
                )
                for comment in self.panel.evaluations
            )
        return signals

    def coverage(self, curated: Sequence[gene_disease_pb2.CuratedEntity]) -> gene_disease_pb2.GeneCoverage:
        """Which of the two absences an entity-less gene is in; they are different findings."""
        if curated:
            return gene_disease_pb2.GENE_COVERAGE_CURATED
        if self._holding_the_gene():
            return gene_disease_pb2.GENE_COVERAGE_NO_VALIDITY_ASSERTION
        return gene_disease_pb2.GENE_COVERAGE_ABSENT


def _download_reference_blobs(resources_bucket: str) -> dict[str, bytes]:
    """Fetch the reference objects from the bucket, failing loud on a missing one.

    Blocking (google-cloud-storage); the caller offloads it to a thread. The client library is
    imported here so the fixture path never pulls it.
    """
    from google.api_core import exceptions as api_exceptions  # noqa: PLC0415 — deferred for the fixture path
    from google.cloud import storage as gcs  # noqa: PLC0415 — deferred so the fixture path skips the library

    blobs: dict[str, bytes] = {}
    with contextlib.closing(gcs.Client()) as client:
        bucket = client.bucket(resources_bucket)
        for name in _REFERENCE_OBJECTS:
            try:
                blobs[name] = bucket.blob(name).download_as_bytes()
            except api_exceptions.NotFound as e:
                raise RuntimeError(
                    f'gene-disease reference object {name!r} missing from bucket {resources_bucket!r}'
                ) from e
    return blobs
