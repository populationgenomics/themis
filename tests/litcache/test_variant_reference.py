"""The reference stores the variant steps open, over hgvs-weaver-data's synthetic reference."""

from __future__ import annotations

import pathlib

import pytest
from weaver_data_provider import genome as genome_mod
from weaver_data_provider import testing

from themis.litcache import variant_reference, variant_validate

# Every store on the one synthetic store and genome, each under the root as a build path names it.
_BUILDS = variant_reference.Builds(
    refseq38='store', ensembl38='store', genome38='genome', refseq37='store', genome37='genome'
)


@pytest.fixture
def references(tmp_path: pathlib.Path) -> variant_reference.References:
    testing.build(tmp_path)
    return variant_reference.References(str(tmp_path), _BUILDS)


def test_an_allele_is_identified_without_reading_its_whole_chromosome(
    references: variant_reference.References, monkeypatch: pytest.MonkeyPatch
) -> None:
    ends: list[int | None] = []
    fetch = genome_mod.Genome.fetch

    def recording(genome: genome_mod.Genome, name: str, start: int, end: int | None = None) -> str:
        ends.append(end)
        return fetch(genome, name, start, end)

    monkeypatch.setattr(genome_mod.Genome, 'fetch', recording)
    # c.2 of FWD, a plus-strand gene whose record is the spliced genome: genome base FWD_CDS[0] + 1, 1-based.
    base = testing.genome_sequence()[testing.FWD_CDS[0]]
    alt = 'C' if base != 'C' else 'G'

    identity = variant_validate.identity(f'NM_000001.1:c.2{base}>{alt}', references)

    assert identity.nucleotide is not None
    # An open-ended read is the rest of the chromosome: what digesting it, rather than asking the catalogue, takes.
    assert ends
    assert None not in ends


def _fwd_change() -> str:
    # c.2 of FWD, a plus-strand gene whose record is the spliced genome: genome base FWD_CDS[0] + 1, 1-based.
    base = testing.genome_sequence()[testing.FWD_CDS[0]]
    return f'NM_000001.1:c.2{base}>{"C" if base != "C" else "G"}'


def test_a_change_off_grch38_has_no_nucleotide_identity_and_is_known_by_its_protein(
    references: variant_reference.References, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The one allele must have one identifier, which is made on GRCh38 only: no lifting over.
    monkeypatch.setattr(references, 'build_of', lambda _accession: 'GRCh37')

    identity = variant_validate.identity(_fwd_change(), references)

    assert identity.nucleotide is None
    assert identity.allele is not None


def test_two_changes_off_grch38_naming_one_protein_change_are_still_two_alleles(
    references: variant_reference.References, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two different third bases of FWD's codon 2: both synonymous, so one protein change; two nucleotide alleles.
    monkeypatch.setattr(references, 'build_of', lambda _accession: 'GRCh37')
    base = testing.genome_sequence()[testing.FWD_CDS[0] + 4]  # c.6, 1-based genome FWD_CDS[0] + 5
    first, second = [b for b in 'ACGT' if b != base][:2]

    one = variant_validate.identity(f'NM_000001.1:c.6{base}>{first}', references)
    other = variant_validate.identity(f'NM_000001.1:c.6{base}>{second}', references)

    assert not one.same(other)


def test_a_former_symbol_printed_in_any_case_names_the_gene_it_now_is(references: variant_reference.References) -> None:
    # The synthetic FWD lists OLDFWD as an alias; no gene holds OLDFWD as its symbol now.
    assert references.current_symbol('oldFWD') == 'FWD'
    assert {t.accession for t in references.transcripts('oldFWD')} == {
        t.accession for t in references.transcripts('FWD')
    }
    assert references.transcripts('FWD')


def test_a_missing_build_fails_to_open(tmp_path: pathlib.Path) -> None:
    testing.build(tmp_path)
    with pytest.raises(FileNotFoundError):
        variant_reference.References(str(tmp_path), variant_reference.BUILDS)
