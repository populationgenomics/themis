"""The variant-discovery vocabulary: what a request may ask, what its identifiers normalise to.

Pure logic, no I/O: the request grammar, the queries a request issues, the per-identifier verdicts,
and the record budget's round-robin with the verdict on what it left unreached.
"""

from __future__ import annotations

import pytest

from themis.services.evidence.literature import variants
from themis.services.evidence.upstreams import litvar


def _requested(**fields: str) -> variants.RequestedVariant:
    return variants.RequestedVariant(
        **{'gene': '', 'hgvs_c': '', 'protein_change': '', 'rsid': '', 'caid': '', 'entity_id': '', **fields}
    )


def _requested_or_raise(**fields: str) -> variants.RequestedVariant:
    return variants.RequestedVariant.of(
        **{'gene': '', 'hgvs_c': '', 'protein_change': '', 'rsid': '', 'caid': '', 'entity_id': '', **fields}
    )


@pytest.mark.parametrize(
    ('change', 'expected'),
    [
        ('NM_000257.4:c.1063G>A', 'c.1063G>A'),  # transcript prefix stripped, c. kept
        ('c.1063G>A', 'c.1063G>A'),  # already bare
        ('NP_000248.2:p.A355T', 'A355T'),  # transcript prefix and p. stripped
        ('p.A355T', 'A355T'),  # p. stripped
        ('p.(Ala355Thr)', 'Ala355Thr'),  # p. and wrapping parens stripped
        ('(p.Ala355Thr)', 'Ala355Thr'),  # HGVS admits the parens outside the p. too
        ('A355T', 'A355T'),  # nothing to strip
        ('', ''),  # empty in, empty out
    ],
)
def test_bare_change(change: str, expected: str) -> None:
    assert variants.bare_change(change) == expected


def test_litvar_queries_issues_one_per_identifier_most_specific_first() -> None:
    # Every query runs: an identifier that resolves says nothing about whether the others reach the
    # same entity, and the point of the lookup is that they routinely do not.
    queries = variants.litvar_queries(
        _requested(gene='GENE1', hgvs_c='NM_1.1:c.1063G>A', protein_change='NP_1.1:p.A355T', rsid='rs00', caid='CA1')
    )
    assert queries == ['CA1', 'rs00', 'GENE1 A355T', 'GENE1 c.1063G>A']


def test_litvar_queries_skips_absent_identifiers() -> None:
    queries = variants.litvar_queries(_requested(gene='GENE1', hgvs_c='NM_1.1:c.1063G>A'))
    assert queries == ['GENE1 c.1063G>A']


def test_litvar_queries_skips_change_queries_without_a_gene() -> None:
    # A bare change string matches across every gene the index knows, so it is never sent alone.
    queries = variants.litvar_queries(_requested(hgvs_c='c.1063G>A', protein_change='p.A355T', rsid='rs00'))
    assert queries == ['rs00']


def test_litvar_queries_normalise_the_key_shaped_identifiers() -> None:
    # A padded ClinGen id and a bare-digit rsID name the same things as their canonical spellings, so
    # asking upstream under the raw form would resolve a different entity set than the one the
    # agreement verdicts are computed against.
    assert variants.litvar_queries(_requested(caid='CA001000', rsid='00')) == ['CA1000', 'rs00']


@pytest.mark.parametrize(
    'fields',
    [
        pytest.param({'entity_id': 'litvar@rs00##'}, id='entity-id'),
        pytest.param({'rsid': 'rs00'}, id='rsid'),
        pytest.param({'caid': 'CA1'}, id='caid'),
        pytest.param({'caid': 'ca0001'}, id='caid-padded-and-lowercased'),
        pytest.param({'rsid': '00'}, id='rsid-without-its-prefix'),
        pytest.param({'gene': 'GENE1', 'hgvs_c': 'c.1A>G'}, id='gene-and-change'),
    ],
)
def test_requested_variant_accepts_anything_that_can_reach_an_entity(fields: dict[str, str]) -> None:
    assert _requested_or_raise(**fields)


@pytest.mark.parametrize(
    'fields',
    [
        # A gene alone reaches the gene's whole literature, not a variant's, and a change alone
        # matches across every gene the index knows.
        pytest.param({}, id='nothing'),
        pytest.param({'gene': 'GENE1'}, id='gene-alone'),
        pytest.param({'hgvs_c': 'c.1A>G', 'protein_change': 'p.A355T'}, id='change-without-a-gene'),
        # A key of the wrong shape reaches nothing and would come back as a fact about the index.
        pytest.param({'rsid': 'rs00x'}, id='rsid-with-junk'),
        pytest.param({'caid': 'rs00'}, id='caid-that-is-an-rsid'),
        pytest.param({'gene': 'GENE1', 'hgvs_c': 'c.1A>G', 'caid': 'CA-1'}, id='caid-with-junk'),
    ],
)
def test_requested_variant_refuses_what_reaches_no_entity(fields: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=r'rsid|caid|identifier'):
        _requested_or_raise(**fields)


@pytest.mark.parametrize(
    ('written', 'same_as'),
    [
        ('CA001000', 'CA1000'),  # zero-padding is not part of the id
        ('ca1000', 'CA1000'),  # nor is case
        ('  CA1000 ', 'CA1000'),
    ],
)
def test_caid_key_ignores_what_does_not_distinguish_two_ids(written: str, same_as: str) -> None:
    assert variants.caid_key(written) == variants.caid_key(same_as)


@pytest.mark.parametrize(('left', 'right'), [('CA1000', 'CA2000'), ('CA1000', 'CA10000')])
def test_caid_key_keeps_distinct_ids_distinct(left: str, right: str) -> None:
    assert variants.caid_key(left) != variants.caid_key(right)


@pytest.mark.parametrize(
    ('written', 'same_as'),
    [
        ('p.Ala355Thr', 'p.A355T'),  # three-letter and one-letter residues
        ('NP_1.1:p.(Ala355Thr)', 'A355T'),  # transcript prefix and parens
        ('p.Arg406Arg', 'p.R406='),  # the two spellings of a synonymous change
        ('p.R406R', 'p.Arg406='),
        ('p.Gln1352Ter', 'p.Q1352*'),  # the stop codon
    ],
)
def test_protein_change_key_ignores_what_does_not_distinguish_two_changes(written: str, same_as: str) -> None:
    assert variants.protein_change_key(written) == variants.protein_change_key(same_as)


@pytest.mark.parametrize(('left', 'right'), [('p.R385R', 'p.R406='), ('p.A355T', 'p.A355S'), ('p.R406fs', 'p.R406=')])
def test_protein_change_key_keeps_distinct_changes_distinct(left: str, right: str) -> None:
    assert variants.protein_change_key(left) != variants.protein_change_key(right)


@pytest.mark.parametrize(
    ('requested', 'stated', 'expected'),
    [
        pytest.param('p.Ala355Thr', 'p.A355T', variants.Agreement.AGREES, id='residue-spelling'),
        pytest.param('p.Arg100LeufsTer5', 'p.Arg100LeufsTer5', variants.Agreement.AGREES, id='same-frameshift'),
        pytest.param('p.A355T', 'p.A340T', variants.Agreement.DIFFERS, id='both-reconcilable'),
        # One change written two ways. Calling this DIFFERS would have the caller discard the entity
        # that holds its literature, so the verdict that carries no evidence is the honest one.
        pytest.param(
            'p.Arg100LeufsTer5', 'p.R100Lfs*5', variants.Agreement.UNSTATED, id='same-frameshift-spelt-two-ways'
        ),
        pytest.param('p.Arg100_Leu102del', 'p.A355T', variants.Agreement.UNSTATED, id='one-side-unreconcilable'),
    ],
)
def test_protein_agreement_only_differs_where_both_forms_are_reconcilable(
    requested: str, stated: str, expected: variants.Agreement
) -> None:
    assert variants.protein_agreement(requested, stated) is expected


@pytest.mark.parametrize('rsid', ['rs404040', 'RS404040', '404040'])
def test_rsid_key_ignores_the_prefix_and_case(rsid: str) -> None:
    assert variants.rsid_key(rsid) == variants.rsid_key('rs404040')


def _labels(**fields: object) -> litvar.EntityLabels:
    return litvar.EntityLabels(
        **{'id': 'litvar@rs00##', 'rsid': '', 'caids': (), 'genes': (), 'change': '', **fields}  # pyright: ignore[reportArgumentType]
    )


def test_an_identifier_the_request_did_not_supply_is_uncompared() -> None:
    # Distinct from UNSTATED: nothing was asked, so the entity's silence is not evidence either.
    verdicts = variants.identifier_agreement(_requested(rsid='rs00'), _labels(rsid='rs00', genes=('GENE1',)))
    assert verdicts.rsid is variants.Agreement.AGREES
    assert verdicts.gene is variants.Agreement.UNCOMPARED
    assert verdicts.caid is variants.Agreement.UNCOMPARED
    assert verdicts.change is variants.Agreement.UNCOMPARED


def test_an_entity_stating_nothing_comparable_is_unstated() -> None:
    # An rsID-keyed entity usually carries no change string; that is no evidence against it.
    verdicts = variants.identifier_agreement(
        _requested(gene='GENE1', protein_change='p.A355T', caid='CA1000'), _labels(rsid='rs00', genes=('GENE1',))
    )
    assert verdicts.gene is variants.Agreement.AGREES
    assert verdicts.caid is variants.Agreement.UNSTATED
    assert verdicts.change is variants.Agreement.UNSTATED


def test_a_change_in_the_other_notation_is_unstated_not_differs() -> None:
    # Translating between the notations needs the transcript, which this service does not resolve.
    verdicts = variants.identifier_agreement(
        _requested(gene='GENE1', protein_change='p.A355T'), _labels(change='c.1063G>A', genes=('GENE1',))
    )
    assert verdicts.change is variants.Agreement.UNSTATED


def test_a_coding_change_is_compared_stripped_of_its_transcript() -> None:
    verdicts = variants.identifier_agreement(
        _requested(gene='GENE1', hgvs_c='NM_1.1:c.1063G>A'), _labels(change='c.1063G>A', genes=('GENE1',))
    )
    assert verdicts.change is variants.Agreement.AGREES


def test_a_multi_gene_entity_agrees_with_each_of_its_genes() -> None:
    verdicts = variants.identifier_agreement(_requested(rsid='rs00'), _labels(rsid='rs00', genes=('GENE1', 'GENE2')))
    assert verdicts.gene is variants.Agreement.UNCOMPARED
    assert (
        variants.identifier_agreement(
            _requested(gene='gene2', rsid='rs00'), _labels(rsid='rs00', genes=('GENE1', 'GENE2'))
        ).gene
        is variants.Agreement.AGREES
    )


def test_a_gene_level_entity_is_not_a_variant_lookups_answer() -> None:
    # The index keys a gene's whole literature under one id — thousands of records about every
    # variant of the gene. Autocomplete matches it loosely; it answers no variant question.
    assert not _labels(id='litvar@#77#', genes=('GENE1',)).names_an_allele()
    # Keyed on a change string alone — no rsID, no ClinGen id — and still an allele, which is the
    # distinction the predicate has to keep or the enumeration route reaches nothing.
    assert _labels(id='litvar@#77#p.A340T', change='p.A340T').names_an_allele()


def test_round_robin_takes_from_every_entity_before_exhausting_one() -> None:
    ranked = [['a', 'b', 'c', 'd'], ['e', 'f'], ['g']]
    assert variants.round_robin(ranked, 3) == ['a', 'e', 'g']


def test_round_robin_returns_distinct_pmids_within_the_limit() -> None:
    ranked = [['a', 'b', 'c'], ['a', 'b'], ['a']]
    selected = variants.round_robin(ranked, 10)
    assert len(selected) == len(set(selected)) == 3
    assert set(selected) == {'a', 'b', 'c'}


def test_round_robin_never_exceeds_its_limit() -> None:
    assert len(variants.round_robin([[str(n) for n in range(100)]], 7)) == 7


def _listed(entity_id: str, total: int) -> litvar.ListedEntity:
    return litvar.ListedEntity(id=entity_id, rsid='', caid='', total_records=total)


def test_gene_inventory_ranks_most_published_first_and_states_the_census() -> None:
    inventory = variants.gene_inventory(
        [_listed('litvar@#77#p.A340T', 3), _listed('litvar@rs00##', 5)], contains='', max_results=1
    )
    assert [entity.id for entity in inventory.entities] == ['litvar@rs00##']
    assert (inventory.total_in_gene, inventory.total_matched) == (2, 2)  # a prefix, and legible as one


def test_gene_inventory_narrows_on_the_id_case_insensitively() -> None:
    inventory = variants.gene_inventory(
        [_listed('litvar@#77#p.A340T', 3), _listed('litvar@rs00##', 5)], contains='a340', max_results=50
    )
    assert [entity.id for entity in inventory.entities] == ['litvar@#77#p.A340T']
    assert (inventory.total_in_gene, inventory.total_matched) == (2, 1)


def test_gene_inventory_narrows_on_the_change_not_the_gene_id_every_id_carries() -> None:
    # `77` is the index's own gene id, so matching the whole id would keep every entity of the gene
    # and report the census as a narrowing that narrowed nothing.
    inventory = variants.gene_inventory(
        [_listed('litvar@#77#p.A340T', 3), _listed('litvar@#77#c.1063G>A', 5)], contains='77', max_results=50
    )
    assert inventory.entities == ()
    assert (inventory.total_in_gene, inventory.total_matched) == (2, 0)
