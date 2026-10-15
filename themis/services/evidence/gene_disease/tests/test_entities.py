"""Gene-disease record assembly and resolution: the questions code settles, and the ones it refuses.

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

# GenCC titles its HPO mode terms; a term outside this table is titled after its curie.
_MOI_TITLES: Mapping[str, str] = {
    'HP:0000006': 'Autosomal dominant',
    'HP:0000007': 'Autosomal recessive',
    'HP:0001419': 'X-linked recessive',
    'HP:0001423': 'X-linked dominant',
    'HP:0010984': 'Digenic inheritance',
}

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
                moi_title=_MOI_TITLES.get(moi_curie, f'mode {moi_curie}'),
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


def test_each_record_states_its_source_s_own_mode_term() -> None:
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'XL', 'Definitive')),
        _gencc(('a subtype', _SUBTYPE, 'HP:0001419', [('Submitter X', 'Strong', '')])),
    )
    assert [e.inheritance_term for e in curated] == ['XL', 'X-linked recessive']


def test_a_mode_term_outside_the_framework_is_a_record_like_any_other() -> None:
    # A digenic assertion has no place among the framework's five modes, and nothing maps it: the
    # gene's records are answered whole, with that one stating its term.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AR', 'Definitive')),
        _gencc(('a subtype', _SUBTYPE, 'HP:0010984', [('Submitter X', 'Supportive', '')])),
    )
    resolution = gene_disease.resolve(curated, _SUBTYPE, _CLOSURE)
    assert [e.inheritance_term for e in resolution.entities] == ['AR', 'Digenic inheritance']


def test_an_assertion_stating_no_mode_fails_loud() -> None:
    # The analyst reads the record's mode off its term; a blank one describes no entity.
    with pytest.raises(ValueError, match='states no mode of inheritance'):
        gene_disease.entities(_clingen(('a subtype', _SUBTYPE, ' ', 'Definitive')), None)


def test_x_linked_dominant_and_recessive_assertions_stay_two_records() -> None:
    # GenCC states the two X-linked kinds apart, and SVCv4 routes them apart; folding them into one
    # record would hand the analyst one classification for two entities.
    curated = gene_disease.entities(
        None,
        _gencc(
            ('a subtype', _SUBTYPE, 'HP:0001419', [('Submitter X', 'Limited', '')]),
            ('a subtype', _SUBTYPE, 'HP:0001423', [('Submitter Y', 'Definitive', 'a narrative')]),
        ),
    )
    assert [(e.inheritance_term, e.validity_classification) for e in curated] == [
        ('X-linked recessive', 'Limited'),
        ('X-linked dominant', 'Definitive'),
    ]


def test_one_source_stating_one_record_twice_reduces_to_its_strongest() -> None:
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Limited'), ('a subtype', _SUBTYPE, 'AD', 'Definitive')), None
    )
    assert [(e.inheritance_term, e.validity_classification) for e in curated] == [('AD', 'Definitive')]


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


def test_the_requested_term_resolves_to_every_record_on_it_under_every_mode() -> None:
    # One term curated under two modes is two entities; which one the presentation belongs to is the
    # analyst's call, so the resolution hands over both rather than refusing or picking.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive'), ('a subtype', _SUBTYPE, 'AR', 'Limited')),
        _gencc(
            ('a subtype', _SUBTYPE, 'HP:0000007', [('Submitter X', 'Strong', '')]),
            ('a sibling', _SIBLING, 'HP:0000006', [('Submitter Y', 'Definitive', '')]),
        ),
    )
    resolution = gene_disease.resolve(curated, _SUBTYPE, _CLOSURE)
    assert resolution.relation == gene_disease_pb2.TERM_RELATION_SAME
    assert [(e.source, e.inheritance_term, e.validity_classification) for e in resolution.entities] == [
        ('ClinGen Gene Validity', 'AD', 'Definitive'),
        ('ClinGen Gene Validity', 'AR', 'Limited'),
        ('GenCC', 'Autosomal recessive', 'Strong'),
    ]


def test_a_curation_one_level_below_the_requested_term_resolves_through_subsumption() -> None:
    # A caller's term is not the curator's label and the curation sits under it in MONDO; the
    # subclass relation settles that, where comparing the two labels does not.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive')),
        _gencc(('the parent', _PARENT, 'HP:0000006', [('Submitter X', 'Supportive', '')])),
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT) == []  # the parent is itself curated
    resolution = gene_disease.resolve(curated, _PARENT, _CLOSURE)
    assert resolution.mondo_id == _PARENT
    assert resolution.relation == gene_disease_pb2.TERM_RELATION_SAME

    without_the_parent = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive'), ('a subtype', _SUBTYPE, 'AR', 'Limited')), None
    )
    assert gene_disease.terms_needing_closure(without_the_parent, _PARENT) == [_SUBTYPE]
    descended = gene_disease.resolve(without_the_parent, _PARENT, _CLOSURE)
    assert descended.mondo_id == _SUBTYPE
    assert descended.requested_mondo_id == _PARENT
    assert descended.relation == gene_disease_pb2.TERM_RELATION_DESCENDANT
    assert [e.inheritance_term for e in descended.entities] == ['AD', 'AR']


def test_two_terms_under_the_requested_one_raise_rather_than_giving_the_stronger() -> None:
    # A term spanning several curated terms has no one answer, and the strongest of them is the one
    # that lifts the gate cap the others impose.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive'), ('a sibling', _SIBLING, 'AD', 'Limited')), None
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT) == [_SUBTYPE, _SIBLING]
    with pytest.raises(errors.UnresolvedEntityError, match='names no one of them'):
        gene_disease.resolve(curated, _PARENT, _CLOSURE)


def test_a_term_the_gene_is_not_curated_for_raises_rather_than_answering_for_the_gene() -> None:
    # A request naming no entity is what let the gene's strongest tier stand for whichever entity was
    # meant; there is no gene-scoped tier to fall back to.
    curated = gene_disease.entities(_clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive')), None)
    with pytest.raises(errors.UnresolvedEntityError, match='not the nearest level'):
        gene_disease.resolve(curated, _UNRELATED, _CLOSURE)


def test_the_requested_term_decides_even_where_a_descendant_is_curated_too() -> None:
    # The term the caller names is curated: its own records answer, and the subtype's would answer a
    # different entity.
    curated = gene_disease.entities(
        _clingen(('the parent', _PARENT, 'AR', 'Limited'), ('a subtype', _SUBTYPE, 'AD', 'Definitive')), None
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT) == []
    resolution = gene_disease.resolve(curated, _PARENT, _CLOSURE)
    assert [(e.mondo_id, e.validity_classification) for e in resolution.entities] == [(_PARENT, 'Limited')]


def test_an_uncurated_gene_is_not_a_resolution_question() -> None:
    with pytest.raises(ValueError, match='resolves nothing at all'):
        gene_disease.resolve([], _SUBTYPE, {})


def test_a_candidate_missing_from_the_closure_is_refused_not_read_as_unrelated() -> None:
    # An absent closure entry establishes nothing; reading it as "not a descendant" is the answer the
    # closure was fetched to settle.
    curated = gene_disease.entities(
        _clingen(('a subtype', _SUBTYPE, 'AD', 'Definitive'), ('a sibling', _SIBLING, 'AD', 'Limited')), None
    )
    with pytest.raises(ValueError, match='no ancestry for the curated term'):
        gene_disease.resolve(curated, _PARENT, {_SUBTYPE: (_PARENT,)})


def test_a_curated_term_outside_mondo_is_never_asked_about() -> None:
    # A curie the ontology cannot be asked about cannot stand under the requested term; asking would
    # report a reference-table defect as a fault in the caller's request.
    curated = gene_disease.entities(
        None, _gencc(('a subtype', 'OMIM:123456', 'HP:0000006', [('Submitter X', 'Definitive', '')]))
    )
    assert gene_disease.terms_needing_closure(curated, _PARENT) == []
    with pytest.raises(errors.UnresolvedEntityError, match='not the nearest level'):
        gene_disease.resolve(curated, _PARENT, {})
