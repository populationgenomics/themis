"""weaver over the reference stores: what the variant extractor's tools and the validation step check against.

A `References` holds three stores, each with its genome: RefSeq on GRCh38 (the current release with NCBI's historical
set stacked under it), Ensembl on GRCh38, and RefSeq on GRCh37. `BUILDS` pins which build of each it opens, under a
root that is a resources bucket or a local copy of one. `check` is the one verdict on an HGVS expression; the
extraction agent sees it through a tool, and the validation step records it.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import posixpath
import re

import weaver
from weaver_data_provider import genome as genome_mod
from weaver_data_provider import provider as provider_mod
from weaver_data_provider import store as store_mod
from weaver_data_provider.v1 import bundle_pb2

from themis.litcache.models import variants_pb2

# RefSeq's MANE Select tag on a transcript record (hgvs-weaver-data's TAG_MANE_SELECT).
_TAG_MANE_SELECT = 1
_ACCESSION = re.compile(r'^(?P<base>[A-Z]{2}_\d+|ENS[TPG]\d{11}|LRG_\d+\w*)(?:\.(?P<version>\d+))?$')
_PROTEIN_PREFIXES = ('NP_', 'XP_', 'YP_', 'ENSP')


@dataclasses.dataclass(frozen=True)
class Builds:
    """Where each store and genome `References` opens lives under a root, as `tools/resources/build.py` names them."""

    refseq38: str
    ensembl38: str
    genome38: str
    refseq37: str
    genome37: str


# The builds the variant steps validate against. A new build lands beside the old one; moving to it is a change here.
BUILDS = Builds(
    refseq38='reference-stores/ncbi/GCF_000001405.40_GRCh38.p14/b1ea3edf9065d545',
    ensembl38='reference-stores/ensembl/116_GRCh38/e652a26210962feb',
    genome38='reference-genomes/ncbi/GCF_000001405.40_GRCh38.p14/8b20218179e0bb5e',
    refseq37='reference-stores/ncbi/GCF_000001405.25_GRCh37.p13/f19a835a4f769fad',
    genome37='reference-genomes/ncbi/GCF_000001405.25_GRCh37.p13/8e7919c943cb7411',
)


@dataclasses.dataclass(frozen=True)
class Check:
    """weaver's verdict on one expression.

    Attributes:
        outcome: What the check found.
        detail: For an outcome other than validated, why, in words an agent or a curator can act on.
    """

    outcome: variants_pb2.Outcome
    detail: str = ''


@dataclasses.dataclass(frozen=True)
class Transcript:
    """One transcript of a gene, as `References.transcripts` lists it."""

    accession: str  # versioned
    protein: str | None  # versioned
    mane_select: bool
    store: str


@dataclasses.dataclass(frozen=True)
class _Store:
    name: str
    provider: provider_mod.BundleProvider
    store: store_mod.BundleStore
    genome: genome_mod.Genome
    # One per store, kept: weaver caches what it fetches for the mapper's life, and the provider answers its refget
    # lookups from the catalogues, where without one weaver digests whole chromosomes.
    mapper: weaver.VariantMapper


def _release(store: store_mod.BundleStore) -> str:
    return '+'.join(shard.release for shard in store.manifest.shards)


def _states_reference(variant: weaver.Variant) -> bool:
    """Whether the expression states a reference base or residue for weaver to check.

    An allele in cis states one when any member does. A description with no position, such as p.? or p.0, states none.
    """
    members = variant.members
    if members is not None:
        return any(_states_reference(member) for member in members)
    posedit = variant.to_dict().get('posedit')
    if not posedit or not posedit.get('pos'):
        return False
    if variant.coordinate_type == 'p':
        return bool(posedit['pos']['start'].get('aa'))
    return bool((posedit.get('edit') or {}).get('ref_'))


class References:
    """The reference stores, opened once and shared by the tools and the validation step.

    Thread-safe for reads: the stores lock their own caches.
    """

    def __init__(self, root: str, builds: Builds) -> None:
        """Open each of `builds` under `root`.

        Args:
            root: A resources bucket as `gs://<bucket>`, or a local directory laid out as one.
            builds: Which build of each store and genome to open; `BUILDS` is the one the variant steps pin.

        Raises:
            FileNotFoundError: If a store or genome is missing from a local root.
            ValueError: If one is missing from a `gs://` root.
        """
        g38 = genome_mod.Genome(posixpath.join(root, builds.genome38))
        g37 = genome_mod.Genome(posixpath.join(root, builds.genome37))
        self._refseq38 = self._open('refseq-grch38', posixpath.join(root, builds.refseq38), g38)
        self._ensembl38 = self._open('ensembl-grch38', posixpath.join(root, builds.ensembl38), g38)
        self._refseq37 = self._open('refseq-grch37', posixpath.join(root, builds.refseq37), g37)
        self._builds = {'GRCh38': self._refseq38, 'GRCh37': self._refseq37}

    @staticmethod
    def _open(name: str, path: str, genome: genome_mod.Genome) -> _Store:
        store = store_mod.BundleStore(path)
        provider = provider_mod.BundleProvider(store, genome)
        return _Store(name, provider, store, genome, weaver.VariantMapper(provider, refget=provider))

    def versions(self) -> list[variants_pb2.StoreVersion]:
        """Each store and the releases it holds, as a validation record states them."""
        return [
            variants_pb2.StoreVersion(name=s.name, release=_release(s.store))
            for s in (self._refseq38, self._ensembl38, self._refseq37)
        ]

    @staticmethod
    def weaver_version() -> str:
        return importlib.metadata.version('hgvs-weaver')

    def _store_for(self, accession: str) -> _Store | None:
        """The store that holds `accession` at exactly its version, if one does."""
        if accession.startswith(('NC_', 'NT_', 'NW_')):
            return next((s for s in self._builds.values() if accession in s.genome.names), None)
        stores = [self._ensembl38] if accession.startswith('ENS') else [self._refseq38, self._refseq37]
        return next((s for s in stores if s.provider.get_refget_accession(accession) is not None), None)

    def build_of(self, accession: str) -> str | None:
        """The build a chromosome accession belongs to, if a held genome names it."""
        return next((build for build, s in self._builds.items() if accession in s.genome.names), None)

    def held_versions(self, accession: str) -> list[str]:
        """Every version of `accession` (versioned or not) that some store holds, oldest first."""
        match = _ACCESSION.match(accession)
        if match is None:
            return []
        base = match['base']
        found: set[int] = set()
        stores = [self._ensembl38] if base.startswith('ENS') else [self._refseq38, self._refseq37]
        for s in stores:
            lookup = s.store.by_protein if base.startswith(_PROTEIN_PREFIXES) else s.store.by_accession
            for bundle in lookup(base):
                for record in [*bundle.transcripts, *bundle.proteins]:
                    if record.accession == base:
                        found.add(record.version)
        return [f'{base}.{v}' for v in sorted(found)]

    def _genes(self, symbol: str) -> set[str]:
        """The current symbols of the genes `symbol` names; empty when it names none.

        The first of three that matches decides: a gene's current symbol, then a previous one, then an alias. Matched
        as the stores' symbol index matches, without case or punctuation: papers print CX26 as Cx26, and NKX6-1 as
        NKX6.1. Decided across both stores at once, so a symbol current in one store only (RefSeq holds no MT- genes)
        is not read in the other as some gene's alias.
        """
        wanted = store_mod.symbol_key(symbol)
        genes = [bundle.gene for s in (self._refseq38, self._ensembl38) for bundle in s.store.by_symbol(symbol)]
        for names in (
            lambda g: [g.symbol],
            lambda g: list(g.previous_symbols),
            lambda g: list(g.alias_symbols),
        ):
            matched = {g.symbol for g in genes if wanted in {store_mod.symbol_key(n) for n in names(g)}}
            if matched:
                return matched
        return set()

    def current_symbol(self, symbol: str) -> str | None:
        """The current HGNC symbol of the gene a symbol names; None when none does, or when it names several genes."""
        genes = self._genes(symbol)
        return next(iter(genes)) if len(genes) == 1 else None

    def transcripts(self, gene: str) -> list[Transcript]:
        """The transcripts of the genes `gene` names (`_genes`) in the current RefSeq and Ensembl releases.

        MANE Select first, then RefSeq first. A gene that lists `gene` only as an alias is not one it names where
        another holds it as its symbol (TTR lists TTN).
        """
        genes = self._genes(gene)
        out: list[Transcript] = []
        for s in (self._refseq38, self._ensembl38):
            seen: set[str] = set()
            for bundle in s.store.by_symbol(gene):
                if bundle.gene.symbol not in genes:
                    continue
                for t in bundle.transcripts:
                    accession = f'{t.accession}.{t.version}'
                    if accession in seen or s.provider.get_refget_accession(accession) is None:
                        continue
                    seen.add(accession)
                    protein = f'{t.protein_accession}.{t.protein_version}' if t.protein_accession else None
                    out.append(Transcript(accession, protein, _TAG_MANE_SELECT in t.tags, s.name))
        current = [t for t in out if self._is_latest(t.accession)]
        return sorted(current, key=lambda t: (not t.mane_select, not t.store.startswith('refseq'), t.accession))

    def _is_latest(self, accession: str) -> bool:
        held = self.held_versions(accession)
        return bool(held) and held[-1] == accession

    def is_mane(self, accession: str) -> bool:
        """Whether `accession` is a MANE Select transcript, or the protein of one, at this version."""
        match = _ACCESSION.match(accession)
        if match is None or match['version'] is None:
            return False
        protein = accession.startswith(_PROTEIN_PREFIXES)
        for s in (self._refseq38, self._ensembl38):
            lookup = s.store.by_protein if protein else s.store.by_accession
            for bundle in lookup(match['base']):
                for t in bundle.transcripts:
                    if _TAG_MANE_SELECT not in t.tags:
                        continue
                    named = f'{t.protein_accession}.{t.protein_version}' if protein else f'{t.accession}.{t.version}'
                    if named == accession:
                        return True
        return False

    def _gene(self, accession: str) -> bundle_pb2.Gene | None:
        """The gene a versioned transcript or protein belongs to, if a store records one."""
        match = _ACCESSION.match(accession)
        if match is None or match['version'] is None:
            return None
        protein = accession.startswith(_PROTEIN_PREFIXES)
        for s in (self._refseq38, self._ensembl38, self._refseq37):
            lookup = s.store.by_protein if protein else s.store.by_accession
            for bundle in lookup(match['base']):
                for t in bundle.transcripts:
                    named = f'{t.protein_accession}.{t.protein_version}' if protein else f'{t.accession}.{t.version}'
                    if named == accession and bundle.gene.symbol:
                        return bundle.gene
        return None

    def hgnc_id(self, accession: str) -> str | None:
        """The HGNC id of the gene a versioned transcript or protein belongs to, if a store records one."""
        gene = self._gene(accession)
        return gene.hgnc_id or None if gene is not None else None

    def gene_symbol(self, accession: str) -> str | None:
        """The current HGNC symbol of the gene a transcript or protein belongs to, if a store records one.

        An accession printed without a version is read at the newest version the stores hold.
        """
        match = _ACCESSION.match(accession)
        if match is None:
            return None
        if match['version'] is None:
            held = self.held_versions(match['base'])
            if not held:
                return None
            accession = held[-1]
        gene = self._gene(accession)
        return gene.symbol if gene is not None else None

    def protein_of(self, accession: str) -> str | None:
        """The versioned protein a versioned transcript encodes, if a store records one."""
        match = _ACCESSION.match(accession)
        if match is None or match['version'] is None:
            return None
        for s in (self._refseq38, self._ensembl38, self._refseq37):
            for bundle in s.store.by_accession(match['base']):
                for t in bundle.transcripts:
                    if f'{t.accession}.{t.version}' == accession and t.protein_accession:
                        return f'{t.protein_accession}.{t.protein_version}'
        return None

    def digest(self, accession: str) -> str | None:
        """The refget digest of a versioned accession's sequence, if a store holds it: equal for equal sequences."""
        s = self._store_for(accession)
        return s.provider.get_refget_accession(accession) if s is not None else None

    def sequence(self, accession: str, start: int, end: int | None = None) -> str:
        """Bases (or residues) `start`..`end` of the accession, 1-based inclusive, in its own numbering.

        A transcript is numbered from its first base, not from the A of its start codon. Without `end`, the bases run
        to the end of the sequence; reading them needs no placement on a genome.

        Raises:
            KeyError: If no store holds the accession.
            ValueError: If the range is empty or reversed.
        """
        if start < 1 or (end is not None and end < start):
            raise ValueError(f'bad range {start}..{end}')
        s = self._store_for(accession)
        if s is None:
            raise KeyError(accession)
        kind = (
            weaver.IdentifierType.GenomicAccession
            if accession.startswith(('NC_', 'NT_', 'NW_'))
            else weaver.IdentifierType.ProteinAccession
            if accession.startswith(_PROTEIN_PREFIXES)
            else weaver.IdentifierType.TranscriptAccession
        )
        return s.provider.get_seq(accession, start - 1, end, kind)

    def cds_start(self, accession: str) -> int | None:
        """The 1-based transcript position of the A of the start codon: c.1 is this base."""
        s = self._store_for(accession)
        if s is None:
            raise KeyError(accession)
        index = s.provider.get_transcript(accession, None)['cds_start_index']
        return None if index is None else index + 1

    def mapper(self, accession: str) -> weaver.VariantMapper:
        """A weaver mapper over the store holding `accession`.

        Raises:
            KeyError: If no store holds it.
        """
        s = self._store_for(accession)
        if s is None:
            raise KeyError(accession)
        return s.mapper

    @staticmethod
    def _disagreement(variant: weaver.Variant, s: _Store) -> str:
        """Why `validate` answered False, in weaver's words: a stated base that differs, or a position outside."""
        try:
            s.mapper.normalize_variant(variant)
        except (weaver.ReferenceMismatchError, weaver.PositionOutsideSequenceError) as error:
            return f'{type(error).__name__}: {error}'
        except weaver.HGVSError:
            pass
        return f'the sequence of {variant.ac} does not hold what the expression states'

    def check(self, expression: str) -> Check:
        """Weaver's verdict on one HGVS expression, accession included."""
        try:
            variant = weaver.parse(expression)
        except weaver.HGVSError as error:
            # Not HGVS, or HGVS that is not one variant, such as alleles in trans.
            return Check(variants_pb2.OUTCOME_UNPARSEABLE, f'{type(error).__name__}: {error}')
        accession = variant.ac
        s = self._store_for(accession)
        if s is None:
            held = self.held_versions(accession)
            detail = f'no store holds {accession}' + (f'; held versions: {", ".join(held)}' if held else '')
            return Check(variants_pb2.OUTCOME_REFERENCE_NOT_HELD, detail)
        try:
            agrees = variant.validate(s.provider)
        except (weaver.ValidationError, weaver.AmbiguousDescriptionError) as error:
            # Not one well-formed reading: no position, a backwards range, or several readings.
            return Check(variants_pb2.OUTCOME_UNPARSEABLE, f'{type(error).__name__}: {error}')
        except weaver.HGVSError as error:
            # Unsupported operations, missing data, unreadable alignments: weaver did not run the check.
            return Check(variants_pb2.OUTCOME_NOT_CHECKED, f'{type(error).__name__}: {error}')
        if not agrees:
            return Check(variants_pb2.OUTCOME_REFERENCE_DISAGREES, self._disagreement(variant, s))
        if not _states_reference(variant):
            return Check(variants_pb2.OUTCOME_NOTHING_TO_CHECK, 'the position exists; no reference base is stated')
        return Check(variants_pb2.OUTCOME_VALIDATED)
