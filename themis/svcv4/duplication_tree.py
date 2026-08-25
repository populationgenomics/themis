"""The single-/multi-exon duplication/gain (SM14) decision-tree cells: per-path family and bounds.

SM14's six scored paths do not share a code family or a set of caps, and `data/svcv4_scoring_reference.json`
states neither: its `NUL_`/`CDS_` concept and category caps are the union over every supplement that
reaches those families, so a not-tandem duplication passes validation against a parent cap two whole
bands wider than its own. This module holds what the reference does not encode; the workflow
transcription `meta.cited_documents` pins (`svcv4-docs/workflow-images/Single Or Multiexon Dup Gain
Workflow.decision-tree.md`, §9) is its authority.

The three routing decisions the tree makes before any of these — more than one gene affected (out to
the CNV recommendations), a whole-gene duplication (not point-scored: `CDS_PRD_NA` through `CDS_NA`),
and a duplication contained within one exon (out to the in-frame indel flow) — leave the flow
diagram, so none of them has a cell here.

Framework conflicts resolved here:

  1. The blue and violet informative-variant terminal nodes are drawn `-8.0 to +8.0`, which the
     diagram reuses from the generic module, against SM14 §80/§92 and both summing boxes reading
     `-8.0 to +6.0`. The text bound is used, as the tree's own discrepancy note rules.
  2. What `LOWER_ORANGE` — proven tandem, breakpoints outside the CDS — takes from the green path.
     SM14 §55 sends it there under the predictive heading, and §57 then merges its functional and
     informative steps with the *upper orange* path's, which would leave it FXN -8.0 to +8.0, INF
     -8.0 to +8.0 and a parent cap of -8.0 to +10.0 on top of a 0.0 predictive box. The diagram
     routes the whole path into green, which the tree states twice (§4 and its summary table), and
     that narrower reading is used: it is the one the drawn flow supports, and §57 reads equally as
     a carry-over from the section above it.
"""

from __future__ import annotations

import dataclasses
import decimal
import enum
from collections.abc import Mapping

from themis.svcv4 import reference, scoring


class DuplicationPath(enum.Enum):
    """The SM14 flow-diagram path the tandem, breakpoint and NMD decisions select."""

    YELLOW = 'yellow'  # proven tandem, breakpoints inside the CDS, NMD predicted
    UPPER_ORANGE = 'upper orange'  # proven tandem, breakpoints inside the CDS, no NMD
    LOWER_ORANGE = 'lower orange'  # proven tandem, breakpoints outside the CDS (green-path logic)
    BLUE = 'blue'  # not known tandem, breakpoints inside the CDS, NMD predicted
    VIOLET = 'violet'  # not known tandem, breakpoints inside the CDS, no NMD
    GREEN = 'green'  # not known tandem, breakpoints outside the CDS


@dataclasses.dataclass(frozen=True)
class FunctionalStage:
    """The `FXN` code range and the cap on (`PRD` + `FXN`) for a path that considers functional data.

    Held as one object because a path either weighs functional data under both bounds or scores it
    `NA` under neither; the two cannot legitimately disagree about whether the stage exists.
    """

    fxn: reference.CapRange
    combined: reference.CapRange


@dataclasses.dataclass(frozen=True)
class DuplicationCell:
    """One SM14 path: which family it codes under, and every bound it states.

    Attributes:
        family: The evidence family the path codes under, `NUL` or `CDS`.
        prd: The initial predictive tier range, pre-matrix.
        functional: The functional stage, or None where the path scores `FXN_NA` — the not-tandem
            paths and the lower orange one, whose genomic consequence SM14 expects to be unique per
            occurrence.
        inf: The informative-variant range.
        benign_informative_only: Whether the path's informative module scores B/LB alone. A
            pathogenic informative variant there is the tree's "reconsider the use of this path"
            node rather than a total to trim, so it is refused instead of clamped.
        parent: The parent-code cap, applied last.
        scaling: Which matrix axes scale positive initial predictive points.
    """

    family: str
    prd: reference.CapRange
    functional: FunctionalStage | None
    inf: reference.CapRange
    benign_informative_only: bool
    parent: reference.CapRange
    scaling: scoring.Scaling


def _range(low: str, high: str) -> reference.CapRange:
    return reference.CapRange(low=decimal.Decimal(low), high=decimal.Decimal(high))


_UNSCALED_BENIGN_ONLY = DuplicationCell(
    family='CDS',
    prd=_range('0', '0'),
    functional=None,
    inf=_range('-8', '0'),
    benign_informative_only=True,
    parent=_range('-8', '0'),
    scaling=scoring.Scaling.NONE,
)

_CELLS: Mapping[DuplicationPath, DuplicationCell] = {
    DuplicationPath.YELLOW: DuplicationCell(
        family='NUL',
        prd=_range('6', '6'),
        functional=FunctionalStage(fxn=_range('-8', '8'), combined=_range('-8', '10')),
        inf=_range('-8', '8'),
        benign_informative_only=False,
        parent=_range('-8', '10'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
    ),
    DuplicationPath.UPPER_ORANGE: DuplicationCell(
        family='CDS',
        prd=_range('0', '3'),
        functional=FunctionalStage(fxn=_range('-8', '8'), combined=_range('-8', '9')),
        inf=_range('-8', '8'),
        benign_informative_only=False,
        parent=_range('-8', '10'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
    ),
    DuplicationPath.LOWER_ORANGE: _UNSCALED_BENIGN_ONLY,
    DuplicationPath.BLUE: DuplicationCell(
        family='NUL',
        prd=_range('4', '4'),
        functional=None,
        inf=_range('-8', '6'),
        benign_informative_only=False,
        parent=_range('-1', '6'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
    ),
    DuplicationPath.VIOLET: DuplicationCell(
        family='CDS',
        prd=_range('0', '2'),
        functional=None,
        inf=_range('-8', '6'),
        benign_informative_only=False,
        parent=_range('-1', '6'),
        scaling=scoring.Scaling.MECHANISM_AND_EXON,
    ),
    DuplicationPath.GREEN: _UNSCALED_BENIGN_ONLY,
}


def cell_for(path: DuplicationPath) -> DuplicationCell:
    """Return the decision-tree cell for one SM14 path.

    Raises:
        ValueError: If the table has no such cell.
    """
    try:
        return _CELLS[path]
    except KeyError as e:
        raise ValueError(f'no SM14 duplication cell for the {path.value} path') from e
