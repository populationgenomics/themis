"""The worksheet-level rows a curation worksheet stores beside its workflows.

A worksheet stores every answer as one `Assessment` row addressed by a `workflow_id`. Two of those
ids name the worksheet-level sections rather than a workflow, and each holds one `Assessment.kind`;
every other id holds a `workflow`. `apps/web/src/curation/sections.ts` fixes the ids (`ROUTING_ID`,
`VERDICT_ID`), and nothing in this tree binds them to the writer's, so a reader holds a row to
agreeing with its payload. An id in `RETIRED` named a section since removed, and a migration deleted
its rows, so a row still stored under one is read by no schema this code knows.
"""

from __future__ import annotations

from collections.abc import Mapping

ROUTING = 'routing'
VERDICT = 'verdict'

# The `Assessment.kind` each worksheet-level row holds, by its `workflow_id`.
KIND_OF: Mapping[str, str] = {ROUTING: 'routing', VERDICT: 'verdict'}
# The `Assessment.kind` every other row holds.
WORKFLOW_KIND = 'workflow'

# Each retired section's id, with the migration that deleted every row stored under it.
RETIRED: Mapping[str, str] = {'case': '0017_curation_case_retired'}


def kind_of(workflow_id: str) -> str:
    """The `Assessment.kind` a row stored under `workflow_id` holds.

    Raises:
        ValueError: If `workflow_id` names a retired section, whose rows a migration deleted, so the
            schema the row was read from is not the one this code was written against.
    """
    if workflow_id in RETIRED:
        raise ValueError(
            f'{workflow_id!r} names a retired worksheet section, whose rows {RETIRED[workflow_id]} deleted; the '
            'schema the row was read from is not the one this code was written against'
        )
    return KIND_OF.get(workflow_id, WORKFLOW_KIND)
