"""Gene-disease entity assembly and resolution: the questions code settles, and the ones it refuses.

Pure over in-memory curations; the MONDO closure is supplied rather than fetched, so no test touches
the network. Three cases stand for the three ways a lookup keyed on a free-text disease answered the
wrong entity: a caller's phrasing that is not a substring of the curator's label, a shorter phrasing
that spans several curated entities, and a request that names no entity at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from themis.rpc import gene_disease_pb2
from themis.services.evidence import errors
from themis.services.evidence.gene_disease import entities as gene_disease
from themis.services.evidence.upstreams import clingen_validity, gencc

_AD = gene_disease_pb2.INHERITANCE_AUTOSOMAL_DOMINANT
_AR = gene_disease_pb2.INHERITANCE_AUTOSOMAL_RECESSIVE
_XL = gene_disease_pb2.INHERITANCE_X_LINKED
_UNSTATED = gene_disease_pb2.INHERITANCE_UNSPECIFIED

# One curated subtype and its parent term, plus a sibling subtype under the same parent.
_PARENT = 'MONDO:0000100'
_SUBTYPE = 'MONDO:0000101'
_SIBLING = 'MONDO:0000102'
_UNRELATED = 'MONDO:0000900'
_CLOSURE: Mapping[str, Sequence[str]] = {
    _SUBTYPE: (_PARENT, 'MONDO:0000001'),
    _SIBLING: (_PARENT, 'MONDO:0000001'),
    _UNRELATED: ('MONDO:0000001',),
}


def _clingen(*curations: tuple[str, str, str, str]) -> clingen_validity.ClinGenValidityResult:
    """A ClinGen lookup over `(label, mondo id, MOI, classification)` curations."""
    return clingen_validity.ClinGenValidityResult(
        curations=[
            clingen_validity.Curation(disease_label=label, mondo_id=mondo_id, moi=moi, classification=classification)
            for label, mondo_id, moi, classification in curations
        ],
        raw={},
        source='ClinGen Gene Validity',
        dataset_versions=('2026-07-24',),
        query='HGNC:1',
    )


def _gencc(*entities: tuple[str, str, str, Sequence[tuple[str, str, str]]]) -> gencc.GenCCResult:
    """A GenCC lookup over `(title, curie, moi curie, [(submitter, classification, note)])` entities."""
    return gencc.GenCCResult(
        entities=[
            gencc.Entity(
                disease_title=title,
                disease_curie=curie,
                moi_curie=moi_curie,
                moi_title=f'mode {moi_curie}',
                classification=submissions[0][1],
                submissions=[
                    gencc.Submission(submitter=submitter, classification=classification, mechanism_note=note)
                    for submitter, classification, note in submissions
                ],
            )
            for title, curie, moi_curie, submissions in entities
        ],
        raw={},
        source='GenCC',
        dataset_versions=('2026-07-01',),
        query='HGNC:1',
    )


def test_entities_carry_both_sources_and_reduce_across_neither() -> None:
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive')),
        _gencc(('the parent', _PARENT, 'HP:0000006', [('Submitter X', 'Supportive', '')])),
    )
    assert [(e.source, e.mondo_id, e.validity_classification) for e in curated] == [
        ('ClinGen Gene Validity', _SUBTYPE, 'Definitive'),
        ('GenCC', _PARENT, 'Supportive'),
    ]


def test_every_entity_carries_the_gate_level_its_classification_reads_as() -> None:
    curated = gene_disease.entities(
        _clingen(
            ('a subtype', _SUBTYPE, 'AD', 'Refuted'),
            ('a sibling', _SIBLING, 'AR', 'No Known Disease Relationship'),
        ),
        _gencc(('the parent', _PARENT, 'HP:0000006', [('Submitter X', 'Supportive', '')])),
    )
    # None of these three classifications is a gate level; a caller mapping them by hand is what the
    # field removes.
    assert [e.gate_level for e in curated] == [
        gene_disease_pb2.GATE_LEVEL_DISPUTED_OR_REFUTED,
        gene_disease_pb2.GATE_LEVEL_LESS_THAN_LIMITED,
        gene_disease_pb2.GATE_LEVEL_LIMITED,
    ]


def test_inheritance_is_harmonised_and_the_source_term_is_kept() -> None:
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive')),
        _gencc(('a subtype', _SUBTYPE, 'HP:0000007', [('Submitter X', 'Strong', '')])),
    )
    assert [(e.inheritance, e.inheritance_term) for e in curated] == [(_AD, 'AD'), (_AR, 'mode HP:0000007')]


def test_a_mode_no_harmonised_mode_covers_fails_loud() -> None:
    # The mode is half of what identifies an entity, so a source vocabulary that moves must surface:
    # landing such a mode anywhere would let it be matched, dropped or defaulted wrongly.
    with pytest.raises(ValueError, match='no harmonised mode covers'):
        gene_disease.entities(_clingen(('a subtype', _SUBTYPE, 'NEW-CODE', 'Definitive')), None)
    with pytest.raises(ValueError, match='no harmonised mode covers'):
        gene_disease.entities(None, _gencc(('a subtype', _SUBTYPE, 'HP:9999999', [('X', 'Definitive', '')])))


def test_one_source_spelling_a_mode_two_ways_states_one_entity() -> None:
    # GenCC groups on the HPO term, so X-linked recessive and X-linked arrive apart; they are the
    # same entity, and its classification is the strongest of them with both assertions kept.
    curated = gene_disease.entities(
        None,
        _gencc(
            ('a subtype', _SUBTYPE, 'HP:0001417', [('Submitter X', 'Limited', '')]),
            ('a subtype', _SUBTYPE, 'HP:0001419', [('Submitter Y', 'Definitive', 'a narrative')]),
        ),
    )
    assert len(curated) == 1
    assert (curated[0].inheritance, curated[0].validity_classification) == (_XL, 'Definitive')
    assert curated[0].inheritance_term == 'mode HP:0001417'  # the first spelling the source uses for it
    assert [s.submitter for s in curated[0].submissions] == ['Submitter X', 'Submitter Y']
    assert [s.text for s in curated[0].mechanism_statements] == ['a narrative']
    assert gene_disease.resolve(curated, _SUBTYPE, _XL, _CLOSURE).entities == curated


def test_a_gencc_entity_keeps_every_submitter_assertion_and_note() -> None:
    curated = gene_disease.entities(
        None,
        _gencc(
            (
                'the parent',
                _PARENT,
                'HP:0000006',
                [('Submitter X', 'Definitive', 'a mechanism narrative'), ('Submitter Y', 'Limited', '')],
            )
        ),
    )
    entity = curated[0]
    assert [(s.submitter, s.validity_classification) for s in entity.submissions] == [
        ('Submitter X', 'Definitive'),
        ('Submitter Y', 'Limited'),
    ]
    assert [s.text for s in entity.mechanism_statements] == ['a mechanism narrative']


def test_a_curation_one_level_below_the_requested_term_resolves_through_subsumption() -> None:
    # A caller's term is not the curator's label and the curation sits under it in MONDO; the
    # subclass relation settles that, where comparing the two labels does not.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive')),
        _gencc(('the parent', _PARENT, 'HP:0000006', [('Submitter X', 'Supportive', '')])),
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT, _AD) == []  # the parent is itself curated
    resolution = gene_disease.resolve(curated, _PARENT, _AD, _CLOSURE)
    assert resolution.mondo_id == _PARENT
    assert resolution.relation == gene_disease_pb2.TERM_RELATION_SAME

    without_the_parent = gene_disease.entities(_clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive')), None)
    assert gene_disease.terms_needing_closure(without_the_parent, _PARENT, _AD) == [_SUBTYPE]
    descended = gene_disease.resolve(without_the_parent, _PARENT, _AD, _CLOSURE)
    assert descended.mondo_id == _SUBTYPE
    assert descended.relation == gene_disease_pb2.TERM_RELATION_DESCENDANT
    assert [e.validity_classification for e in descended.entities] == ['Definitive']


def test_two_entities_under_the_requested_term_raise_rather_than_giving_the_stronger() -> None:
    # A term spanning several curated entities has no one answer, and the strongest of them is the
    # one that lifts the gate cap the others impose.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive'), ('a sibling', _SIBLING, 'AD', 'Limited')), None
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT, _AD) == [_SUBTYPE, _SIBLING]
    with pytest.raises(errors.UnresolvedEntityError, match='names no one of them'):
        gene_disease.resolve(curated, _PARENT, _AD, _CLOSURE)


def test_an_entity_the_gene_is_not_curated_for_raises_rather_than_answering_for_the_gene() -> None:
    # A request naming no entity is what let the gene's strongest tier stand for whichever entity was
    # meant; there is no gene-scoped tier to fall back to.
    curated = gene_disease.entities(_clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive')), None)
    with pytest.raises(errors.UnresolvedEntityError, match='not the nearest level'):
        gene_disease.resolve(curated, _UNRELATED, _AD, _CLOSURE)


def test_the_requested_term_curated_under_another_mode_raises() -> None:
    curated = gene_disease.entities(_clingen(('a subtype', _SUBTYPE, 'AR', 'Definitive')), None)
    with pytest.raises(errors.UnresolvedEntityError, match='is not the one asked about'):
        gene_disease.resolve(curated, _SUBTYPE, _AD, _CLOSURE)


def test_the_requested_term_decides_even_where_a_descendant_fits_the_requested_mode() -> None:
    # The term the caller names is curated, under another mode: their entity conflicts with the
    # curation, and substituting the subtype curated under their mode would answer a different one.
    curated = gene_disease.entities(
        _clingen(('the parent', _PARENT, 'AR', 'Limited'), ('a subtype', _SUBTYPE, 'AD', 'Definitive')), None
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT, _AD) == []
    with pytest.raises(errors.UnresolvedEntityError, match='is not the one asked about'):
        gene_disease.resolve(curated, _PARENT, _AD, _CLOSURE)


def test_a_term_curated_under_two_modes_needs_the_mode_stated() -> None:
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive'), ('a subtype', _SUBTYPE, 'AR', 'Limited')), None
    )
    with pytest.raises(errors.UnresolvedEntityError, match='state the entity'):
        gene_disease.resolve(curated, _SUBTYPE, _UNSTATED, _CLOSURE)
    resolved = gene_disease.resolve(curated, _SUBTYPE, _AR, _CLOSURE)
    assert [e.validity_classification for e in resolved.entities] == ['Limited']


def test_both_sources_curating_one_entity_resolve_to_it_together() -> None:
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Limited')),
        _gencc(('a subtype', _SUBTYPE, 'HP:0000006', [('Submitter X', 'Definitive', '')])),
    )
    resolution = gene_disease.resolve(curated, _SUBTYPE, _AD, _CLOSURE)
    assert [(e.source, e.validity_classification) for e in resolution.entities] == [
        ('ClinGen Gene Validity', 'Limited'),
        ('GenCC', 'Definitive'),
    ]
    assert resolution.requested_mondo_id == _SUBTYPE
    assert resolution.requested_inheritance == _AD


def test_an_uncurated_gene_is_not_a_resolution_question() -> None:
    with pytest.raises(ValueError, match='resolves nothing at all'):
        gene_disease.resolve([], _SUBTYPE, _AD, {})


def test_nothing_curated_under_the_requested_mode_says_so() -> None:
    # The curated term IS a MONDO descendant of the requested one — only under another mode — so the
    # refusal must name the mode rather than report a miss in the ontology.
    curated = gene_disease.entities(_clingen(('a subtype', _SUBTYPE, 'AR', 'Definitive')), None)
    assert gene_disease.terms_needing_closure(curated, _PARENT, _AD) == []
    with pytest.raises(errors.UnresolvedEntityError, match='curated only under'):
        gene_disease.resolve(curated, _PARENT, _AD, {})


def test_a_candidate_missing_from_the_closure_is_refused_not_read_as_unrelated() -> None:
    # An absent closure entry establishes nothing; reading it as "not a descendant" is the answer the
    # closure was fetched to settle.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive'), ('a sibling', _SIBLING, 'AD', 'Limited')), None
    )
    with pytest.raises(ValueError, match='no ancestry for the curated term'):
        gene_disease.resolve(curated, _PARENT, _AD, {_SUBTYPE: (_PARENT,)})


def test_a_curated_term_outside_mondo_is_never_asked_about() -> None:
    # A curie the ontology cannot be asked about cannot stand under the requested term; asking would
    # report a reference-table defect as a fault in the caller's request.
    curated = gene_disease.entities(
        None, _gencc(('a subtype', 'OMIM:123456', 'HP:0000006', [('Submitter X', 'Definitive', '')]))
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT, _AD) == []
    with pytest.raises(errors.UnresolvedEntityError, match='not the nearest level'):
        gene_disease.resolve(curated, _PARENT, _AD, {})
