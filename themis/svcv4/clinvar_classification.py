"""ClinVar's aggregate germline classification vocabulary, and the three readings SVCv4 takes of it.

Two SVCv4 rules take a gene's ClinVar-classified variants as comparators and want different sets of
one, so the vocabulary carries two gates rather than one (`docs/design/evidence-interfaces.md` argues the
split; SM19 states no frequency or penetrance condition on an informative variant, SM3's threshold
is a frequency claim and states both).

- `is_pathogenic` — every germline term asserts pathogenicity. The gate on the shared pool: SM19's
  informative-variant candidate set, which an analyst then judges eligibility over, plus the P/LP
  density count.
- `is_unqualified_pathogenic` — additionally, ClinVar attaches no qualifier and no second assertion
  to that call. The gate SM3's pathogenic-variants DAFT anchors on.

`informative_class` is the third reading, and it resolves rather than gates: the `*_INF` rules weigh a
P differently from an LP, so a record has to reach them as one token. Where the aggregate names
several — "Pathogenic/Likely pathogenic" is ClinVar's spelling for submitters split between two
adjacent rungs — **the term nearest uncertainty is the one scored**, which awards the split call less
evidence than either the strong reading or a first-term-wins rule would, in whichever direction it
runs. Nothing in SM19 settles that, so the reading errs toward less evidence and states itself; an
analyst who reads the split otherwise builds the `grantham.InformativeMissense` directly.

ClinVar renders an aggregate classification as its submitted germline terms joined by "/", then ";"
and the non-ACMG assertion types ("Pathogenic/Likely pathogenic; risk factor"). Both gates parse
that structure rather than matching whole descriptions, which do not enumerate: the 340 records
registry-wide carrying a qualified term spell it 19 different ways (measured July 2026).

The `;` tail is where the two gates part. It bears on no pathogenicity claim, so `is_pathogenic`
ignores it; it bears directly on a frequency one, so `is_unqualified_pathogenic` rejects any record
carrying a tail rather than ranking which tails disqualify.
"""

from __future__ import annotations

_TERM_SEPARATOR = '/'
_OTHER_ASSERTIONS_SEPARATOR = ';'

_UNQUALIFIED_PATHOGENIC = frozenset({'pathogenic', 'likely pathogenic'})

# ClinVar runs three parallel scales — Mendelian, low-penetrance, risk-allele — so these are the P-
# and LP-strength rungs of the latter two. "Uncertain risk allele" is the VUS rung, and sits below
# with "Uncertain significance".
_QUALIFIED_PATHOGENIC = frozenset(
    {
        'pathogenic, low penetrance',
        'likely pathogenic, low penetrance',
        'established risk allele',
        'likely risk allele',
    }
)

PATHOGENIC_TERMS = _UNQUALIFIED_PATHOGENIC | _QUALIFIED_PATHOGENIC

# The rest of ClinVar's germline vocabulary, enumerated so that an unrecognised term raises rather
# than reading as "not pathogenic".
_NOT_PATHOGENIC = frozenset(
    {
        'benign',
        'likely benign',
        'uncertain significance',
        'uncertain risk allele',
        'conflicting classifications of pathogenicity',
        'conflicting interpretations of pathogenicity',  # ClinVar's pre-2024 spelling
        'no classification for the single variant',
        'no classifications from unflagged records',
        'no classification provided',
        'not provided',
        'association',
        'association not found',
        'affects',
        'confers sensitivity',
        'drug response',
        'other',
        'protective',
        'risk factor',
    }
)

_VOCABULARY = PATHOGENIC_TERMS | _NOT_PATHOGENIC

# The terms the *_INF rules score, each with the token they score as and its distance from
# uncertainty. The distance is what resolves a multi-term aggregate: the nearest-to-uncertain term
# wins, so "Pathogenic/Likely pathogenic" scores LP (module docstring). Every other germline term
# states no classification the rules weigh, and reaches `informative_class` as a refusal.
_INFORMATIVE_TERMS: dict[str, tuple[str, int]] = {
    'pathogenic': ('P', 2),
    'pathogenic, low penetrance': ('P', 2),
    'established risk allele': ('P', 2),
    'likely pathogenic': ('LP', 1),
    'likely pathogenic, low penetrance': ('LP', 1),
    'likely risk allele': ('LP', 1),
    'uncertain significance': ('VUS', 0),
    'uncertain risk allele': ('VUS', 0),
    'likely benign': ('LB', 1),
    'benign': ('B', 2),
}

_PATHOGENIC_TOKENS = frozenset({'P', 'LP'})
_BENIGN_TOKENS = frozenset({'B', 'LB'})


def _germline_terms(classification: str) -> tuple[list[str], bool]:
    """Split an aggregate classification into its germline terms and whether a `;` tail follows.

    Args:
        classification: The aggregate germline classification verbatim
            (`ClinVarRecord.classification`). Empty means ClinVar holds no germline classification
            for the record, which is no terms rather than a fault.

    Returns:
        The terms lower-cased and in the order stated, and whether ClinVar appended any non-ACMG
        assertion type. The tail itself is not returned: no caller ranks its contents.

    Raises:
        ValueError: If a term is not in ClinVar's germline classification vocabulary.
    """
    head, separator, _ = classification.partition(_OTHER_ASSERTIONS_SEPARATOR)
    normalised = head.strip().lower()
    if not normalised:
        return [], bool(separator)
    terms = [term.strip() for term in normalised.split(_TERM_SEPARATOR)]
    unknown = [term for term in terms if term not in _VOCABULARY]
    if unknown:
        raise ValueError(
            f'unknown ClinVar germline classification term(s) {unknown} in {classification!r}; '
            'the vocabulary is enumerated so a renamed or added one surfaces here'
        )
    return terms, bool(separator)


def is_pathogenic(classification: str) -> bool:
    """Whether ClinVar classifies the variant pathogenic, in any of the spellings it aggregates.

    The gate on the gene pool SM19's informative-variant rules and the P/LP density read. Every
    germline term must be a pathogenic assertion, so "Pathogenic/Likely pathogenic" and
    "Pathogenic/Pathogenic, low penetrance" qualify and "Conflicting classifications of
    pathogenicity" does not. A `;` tail does not bear on the question and is ignored.

    Args:
        classification: The aggregate germline classification verbatim.

    Returns:
        Whether every germline term in it asserts pathogenicity. False for an empty classification.

    Raises:
        ValueError: If a term is not in ClinVar's germline classification vocabulary.
    """
    terms, _ = _germline_terms(classification)
    return bool(terms) and all(term in PATHOGENIC_TERMS for term in terms)


def is_unqualified_pathogenic(classification: str) -> bool:
    """Whether ClinVar calls the variant pathogenic and asserts nothing else about it.

    Strictly narrower than `is_pathogenic`: no penetrance qualifier on any term, and no non-ACMG
    assertion after the ";". The gate SM3's pathogenic-variants DAFT anchors on, and the pool's
    other readers do not (module docstring).

    Args:
        classification: The aggregate germline classification verbatim.

    Returns:
        Whether the classification is an unqualified pathogenic call with no other assertion.

    Raises:
        ValueError: If a term is not in ClinVar's germline classification vocabulary.
    """
    terms, other_assertions = _germline_terms(classification)
    return bool(terms) and not other_assertions and all(term in _UNQUALIFIED_PATHOGENIC for term in terms)


def informative_class(classification: str) -> str:
    """The token an informative variant is scored at, from ClinVar's aggregate classification (SM19).

    A `;` tail is a non-ACMG assertion and bears on none of this, so it is ignored, exactly as
    `is_pathogenic` ignores it.

    Args:
        classification: The aggregate germline classification verbatim.

    Returns:
        One of 'P', 'LP', 'VUS', 'LB', 'B' — the tokens `scoring.informative_points` and
        `grantham.mis_inf_points` weigh.

    Raises:
        ValueError: If a term is not in ClinVar's germline vocabulary; if the aggregate states no
            classification the rules score — a conflicting record, an evidence-only one, a drug
            response — which is a record to leave out rather than one to score at zero; or if it
            straddles the pathogenic and benign directions, which no aggregate ClinVar computes
            does, so the record is one nothing here can read.
    """
    terms, _ = _germline_terms(classification)
    if not terms:
        raise ValueError(f'{classification!r} states no germline classification the *_INF rules score')
    scored = [_INFORMATIVE_TERMS[term] for term in terms if term in _INFORMATIVE_TERMS]
    if len(scored) != len(terms):
        outside = sorted(set(terms) - set(_INFORMATIVE_TERMS))
        raise ValueError(
            f'{classification!r} carries {outside}, which the *_INF rules score at no strength; such a '
            'record is left out of the informative set rather than counted at zero'
        )
    tokens = {token for token, _ in scored}
    if tokens & _PATHOGENIC_TOKENS and tokens & _BENIGN_TOKENS:
        raise ValueError(f'{classification!r} asserts pathogenicity and benignity at once')
    return min(scored, key=lambda entry: entry[1])[0]
