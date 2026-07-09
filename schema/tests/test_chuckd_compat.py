"""Test the S0.6 JSON Schema compat gate (``tools.schema.chuckd_compat``).

The pure logic — per-type extraction, ``chuckd`` output parsing, the
open-content-model downgrade — runs without Docker in the ordinary pytest job.
The end-to-end behaviour the ADR calls out (an enum-member removal trips the
gate red; an optional-field addition is tolerated) runs the real ``chuckd``
image and so is gated on a reachable Docker daemon.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from tools.schema import chuckd_compat

_JSONSCHEMA_DIR = pathlib.Path(__file__).resolve().parent / 'jsonschema'


def _features_bundle() -> dict:
    return json.loads((_JSONSCHEMA_DIR / 'features.schema.json').read_text())


def test_extract_types_promotes_each_def_to_a_loadable_root() -> None:
    bundle = _features_bundle()
    types = chuckd_compat.extract_types(bundle)

    assert types.keys() == bundle['$defs'].keys()
    colour = types['Colour']
    # The type's own body is preserved...
    assert colour['enum'] == ['red', 'green', 'blue']
    # ...with an absolute $id (jsonsKema rejects a relative one) and the whole
    # $defs retained so #/$defs refs still resolve when loaded as a root.
    assert colour['$id'] == chuckd_compat._TYPE_ID_BASE + 'Colour'
    assert colour['$schema'] == chuckd_compat._DRAFT_2020_12
    assert colour['$defs'] == bundle['$defs']


def test_parse_findings_extracts_error_types_from_chuckd_output() -> None:
    # Verbatim shape chuckd emits (description is single-quoted / unterminated).
    output = (
        '{errorType:"COMBINED_TYPE_SUBSCHEMAS_CHANGED", description:"A type at path \'#/\' is different\'}\n'
        "{oldSchema: '{...}'}\n"
    )
    findings = chuckd_compat.parse_findings(output, 'Colour')
    assert [f.error_type for f in findings] == ['COMBINED_TYPE_SUBSCHEMAS_CHANGED']
    assert findings[0].type_name == 'Colour'


def test_parse_findings_compatible_output_is_empty() -> None:
    assert chuckd_compat.parse_findings('', 'Colour') == []


def test_removed_type_is_a_hard_failure_without_invoking_chuckd() -> None:
    # A $def gone from the new bundle is a removal/rename — breaking under
    # additive-only evolution. It's caught by set difference (no surviving type
    # to diff), so this needs no Docker.
    baseline = _features_bundle()
    new = copy.deepcopy(baseline)
    del new['$defs']['ScalarHolder']  # referenced by nothing: no other type's body changes

    hard, soft = chuckd_compat.diff_bundles(new, baseline)
    assert [(f.type_name, f.error_type) for f in hard] == [('ScalarHolder', 'TYPE_REMOVED')]
    assert soft == []


def test_classify_downgrades_only_open_content_model_addition() -> None:
    findings = [
        chuckd_compat.Finding('EnumHolder', chuckd_compat._OPEN_CONTENT_ADDED, 'detail'),
        chuckd_compat.Finding('Colour', 'COMBINED_TYPE_SUBSCHEMAS_CHANGED', 'detail'),
    ]
    hard, soft = chuckd_compat.classify(findings)
    assert [f.error_type for f in hard] == ['COMBINED_TYPE_SUBSCHEMAS_CHANGED']
    assert [f.error_type for f in soft] == [chuckd_compat._OPEN_CONTENT_ADDED]


# End-to-end: the real chuckd image. These encode the ADR's S0.6 acceptance
# criteria directly, so they also prove the per-type extraction actually defeats
# chuckd's root-only traversal of the $defs bundle.
@pytest.mark.usefixtures('docker_daemon')
def test_enum_member_removal_is_a_hard_failure() -> None:
    baseline = _features_bundle()
    new = copy.deepcopy(baseline)
    new['$defs']['Colour']['enum'] = ['red', 'green']  # drop "blue": narrows the value set

    hard, soft = chuckd_compat.diff_bundles(new, baseline)
    assert [f.error_type for f in hard] == ['COMBINED_TYPE_SUBSCHEMAS_CHANGED']
    assert soft == []


@pytest.mark.usefixtures('docker_daemon')
def test_optional_field_addition_is_tolerated_on_open_content_model() -> None:
    baseline = _features_bundle()
    new = copy.deepcopy(baseline)
    new['$defs']['EnumHolder']['properties']['extra'] = {'type': 'string'}  # additive, optional

    hard, soft = chuckd_compat.diff_bundles(new, baseline)
    assert hard == []
    assert [f.error_type for f in soft] == [chuckd_compat._OPEN_CONTENT_ADDED]


@pytest.mark.usefixtures('docker_daemon')
def test_unchanged_bundle_has_no_findings() -> None:
    bundle = _features_bundle()
    hard, soft = chuckd_compat.diff_bundles(copy.deepcopy(bundle), bundle)
    assert hard == []
    assert soft == []
