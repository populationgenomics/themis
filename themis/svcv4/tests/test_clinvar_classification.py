"""Tests for the two gates SVCv4 reads ClinVar's aggregate germline classification through."""

from __future__ import annotations

import pytest

from themis.svcv4 import clinvar_classification

# Real aggregate descriptions, as ClinVar renders them (verified against the live index, July 2026).
_UNQUALIFIED = ['Pathogenic', 'Likely pathogenic', 'Pathogenic/Likely pathogenic']
_QUALIFIED = [
    'Pathogenic, low penetrance',
    'Likely pathogenic, low penetrance',
    'Established risk allele',
    'Likely risk allele',
    'Pathogenic/Pathogenic, low penetrance',
    'Likely pathogenic/Pathogenic, low penetrance',
    'Pathogenic/Likely pathogenic/Pathogenic, low penetrance/Established risk allele',
]
_TAILED = [
    'Pathogenic; risk factor',
    'Pathogenic; other',
    'Likely pathogenic; drug response',
    'Pathogenic/Likely pathogenic; risk factor',  # SERPINA1 PI*Z
    'Pathogenic; protective; risk factor',
]
_NOT_PATHOGENIC = [
    'Benign',
    'Likely benign',
    'Uncertain significance',
    'Uncertain risk allele',
    'Conflicting classifications of pathogenicity',
    'Conflicting interpretations of pathogenicity',
    'drug response',
    '',
]


@pytest.mark.parametrize('classification', [*_UNQUALIFIED, *_QUALIFIED, *_TAILED])
def test_a_pathogenic_call_stays_pathogenic_however_it_is_qualified(classification: str) -> None:
    assert clinvar_classification.is_pathogenic(classification)


@pytest.mark.parametrize('classification', _NOT_PATHOGENIC)
def test_a_non_pathogenic_term_is_not_read_as_pathogenic(classification: str) -> None:
    assert not clinvar_classification.is_pathogenic(classification)


def test_one_non_pathogenic_term_disqualifies_the_whole_aggregate() -> None:
    """The gate is over every term, not any: an aggregate ClinVar could not resolve is not a P/LP call."""
    assert not clinvar_classification.is_pathogenic('Pathogenic/Uncertain significance')


@pytest.mark.parametrize('classification', _UNQUALIFIED)
def test_only_an_unqualified_call_may_anchor_a_threshold(classification: str) -> None:
    assert clinvar_classification.is_unqualified_pathogenic(classification)


@pytest.mark.parametrize('classification', [*_QUALIFIED, *_TAILED, *_NOT_PATHOGENIC])
def test_a_qualified_tailed_or_non_pathogenic_call_may_not_anchor_a_threshold(classification: str) -> None:
    """The DAFT's gate is strictly the narrower one, and both extra conditions are frequency ones.

    A penetrance qualifier and a second non-ACMG assertion each name an allele reaching a frequency
    a fully-penetrant pathogenic variant could not; anchoring on one raises the threshold it derives.
    """
    assert not clinvar_classification.is_unqualified_pathogenic(classification)


@pytest.mark.parametrize('classification', [*_UNQUALIFIED, *_QUALIFIED, *_NOT_PATHOGENIC])
@pytest.mark.parametrize('tail', ['; risk factor', '; protective; risk factor'])
def test_a_non_acmg_tail_leaves_the_pathogenicity_call_and_bars_the_threshold(classification: str, tail: str) -> None:
    """ClinVar appends its non-ACMG assertions after ";": a claim about risk, not about pathogenicity."""
    tailed = f'{classification}{tail}'
    assert clinvar_classification.is_pathogenic(tailed) == clinvar_classification.is_pathogenic(classification)
    assert not clinvar_classification.is_unqualified_pathogenic(tailed)


@pytest.mark.parametrize('classification', [*_UNQUALIFIED, *_QUALIFIED, *_TAILED, *_NOT_PATHOGENIC])
def test_neither_gate_reads_whitespace_or_case(classification: str) -> None:
    shouted = f'  {classification.upper()}  '
    assert clinvar_classification.is_pathogenic(shouted) == clinvar_classification.is_pathogenic(classification)
    assert clinvar_classification.is_unqualified_pathogenic(
        shouted
    ) == clinvar_classification.is_unqualified_pathogenic(classification)


@pytest.mark.parametrize(
    'classification',
    [
        'Pathogenic, reduced penetrance',  # a qualifier ClinVar does not emit
        'Pathogenic/Probably pathogenic',  # one real term, one invented
        'not a classification',
    ],
)
def test_an_unrecognised_term_is_a_fault(classification: str) -> None:
    """Read as "not pathogenic", a renamed term empties both pools as if ClinVar had classified it so."""
    with pytest.raises(ValueError, match='unknown ClinVar germline classification term'):
        clinvar_classification.is_pathogenic(classification)
    with pytest.raises(ValueError, match='unknown ClinVar germline classification term'):
        clinvar_classification.is_unqualified_pathogenic(classification)


@pytest.mark.parametrize(
    ('classification', 'expected'),
    [
        ('Pathogenic', 'P'),
        ('Likely pathogenic', 'LP'),
        ('Benign', 'B'),
        ('Likely benign', 'LB'),
        ('Uncertain significance', 'VUS'),
        ('Uncertain risk allele', 'VUS'),
        ('Pathogenic, low penetrance', 'P'),
        ('Likely risk allele', 'LP'),
        ('Pathogenic; risk factor', 'P'),  # the ";" tail asserts nothing about the classification
    ],
)
def test_a_single_term_aggregate_scores_at_its_own_rung(classification: str, expected: str) -> None:
    assert clinvar_classification.informative_class(classification) == expected


@pytest.mark.parametrize(
    ('classification', 'expected'),
    [
        ('Pathogenic/Likely pathogenic', 'LP'),
        ('Benign/Likely benign', 'LB'),
        ('Uncertain significance/Uncertain risk allele', 'VUS'),
        ('Pathogenic/Pathogenic, low penetrance', 'P'),
    ],
)
def test_a_split_aggregate_scores_at_the_rung_nearest_uncertainty(classification: str, expected: str) -> None:
    # ClinVar's "/" says its submitters are split between two rungs; scoring the stronger would award
    # the split call the first-variant weight, which nothing in SM19 supports.
    assert clinvar_classification.informative_class(classification) == expected


@pytest.mark.parametrize(
    'classification',
    ['Conflicting classifications of pathogenicity', 'drug response', 'not provided', '', 'risk factor'],
)
def test_a_record_stating_no_scored_classification_is_refused(classification: str) -> None:
    with pytest.raises(ValueError, match=r'score|no germline classification'):
        clinvar_classification.informative_class(classification)


def test_every_pathogenic_term_the_pool_gate_admits_reaches_a_scored_rung() -> None:
    """The pool is the *_INF candidate set, so a term it admits that scores at no rung is a gap."""
    for term in clinvar_classification.PATHOGENIC_TERMS:
        assert clinvar_classification.informative_class(term) in {'P', 'LP'}
