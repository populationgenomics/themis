import {
  Consequence,
  Inheritance,
} from "@/gen/themis/evidence/models/evidence_pb";
import { GateLevel } from "@/gen/themis/rpc/gene_disease_pb";
import {
  AssessmentStatus,
  Classification,
  Confidence,
} from "@/gen/themis/svcv4/models/svcv4_pb";
import {
  type Svcv4Classification,
  type Svcv4Classification_Code,
  Svcv4Classification_Line_Kind,
} from "@/models/widgets";

// What the SVCv4 classification widget draws, read off its payload: labels for the vocabularies it
// names, the groups its codes fall in, which codes a curator should look at, and the geometry of
// the points ruler. Every number here is one the payload carries; nothing is scored in the client
// (docs/design/svcv4-classification-widget.md).

/** A decimal the payload carries, as a number to position and compare. Raises on text the payload's
 *  rules admit none of. */
export function pointsOf(text: string): number {
  if (!/^-?[0-9]+(\.[0-9]+)?$/.test(text)) {
    throw new Error(`not decimal text: ${JSON.stringify(text)}`);
  }
  return Number(text);
}

/** Points as the widget prints them: signed, with a true minus, and no trailing zero (`+7`, `−1.5`,
 *  `0`). */
export function signedPoints(text: string): string {
  return signedNumber(pointsOf(text));
}

/** A number of points computed from the payload's, as `signedPoints` prints them. Rounded to the
 *  hundredth, which is finer than any value the framework states, so a sum's binary noise never
 *  shows. */
export function signedNumber(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  if (rounded === 0) return "0";
  const magnitude = String(Math.abs(rounded));
  return rounded > 0 ? `+${magnitude}` : `−${magnitude}`;
}

export type Tone = "pathogenic" | "benign" | "neutral";

interface ClassLabel {
  label: string;
  short: string;
  tone: Tone;
}

const CLASSES: ReadonlyMap<Classification, ClassLabel> = new Map([
  [
    Classification.PATHOGENIC,
    { label: "Pathogenic", short: "P", tone: "pathogenic" },
  ],
  [
    Classification.LIKELY_PATHOGENIC,
    { label: "Likely pathogenic", short: "LP", tone: "pathogenic" },
  ],
  [
    Classification.VUS,
    { label: "Uncertain significance", short: "VUS", tone: "neutral" },
  ],
  [
    Classification.LIKELY_BENIGN,
    { label: "Likely benign", short: "LB", tone: "benign" },
  ],
  [Classification.BENIGN, { label: "Benign", short: "B", tone: "benign" }],
  [
    Classification.NOT_ESTABLISHED,
    { label: "Class not established", short: "none", tone: "neutral" },
  ],
  [
    Classification.VARIANT_IN_GENE_OF_UNCERTAIN_SIGNIFICANCE,
    {
      label: "Variant in gene of uncertain significance",
      short: "VIGUS",
      tone: "neutral",
    },
  ],
  [
    Classification.DO_NOT_REPORT,
    { label: "Do not report", short: "DNR", tone: "neutral" },
  ],
]);

/** A class as the widget names it; a value this build does not know draws as unknown. */
export function classLabel(value: Classification): ClassLabel {
  return (
    CLASSES.get(value) ?? {
      label: "Unknown class",
      short: "?",
      tone: "neutral",
    }
  );
}

const STATUSES: ReadonlyMap<AssessmentStatus, string> = new Map([
  [AssessmentStatus.SCORED, "scored"],
  [AssessmentStatus.NOT_APPLICABLE, "not applicable"],
  [AssessmentStatus.NO_DATA, "no data"],
]);

export function statusLabel(value: AssessmentStatus): string {
  return STATUSES.get(value) ?? "unknown status";
}

const CONFIDENCES: ReadonlyMap<Confidence, string> = new Map([
  [Confidence.SETTLED, "settled"],
  [Confidence.LEANING, "leaning"],
  [Confidence.OPEN, "open"],
]);

/** How open the agent considers a call; empty where it stated none, as a code not scored does. */
export function confidenceLabel(value: Confidence): string {
  if (value === Confidence.UNSPECIFIED) return "";
  return CONFIDENCES.get(value) ?? "unknown";
}

const INDEPENDENT_FAMILIES: ReadonlyMap<string, string> = new Map([
  ["POP", "Population"],
  ["CLN", "Clinical"],
  ["LOC", "Locus"],
]);

/** A run of codes the widget draws under one heading. */
export interface CodeGroup {
  /** The heading: a family's name, or a path's label. */
  heading: string;
  /** The path the group's codes sit on, or undefined for an independent family. */
  path?: Svcv4Classification["paths"][number];
  codes: Svcv4Classification_Code[];
}

/** The payload's codes grouped as the framework groups them, in the payload's order: each
 *  independent family under its name, and each path's codes under the route's label. */
export function codeGroups(payload: Svcv4Classification): CodeGroup[] {
  const groups: CodeGroup[] = [];
  for (const code of payload.codes) {
    const key =
      code.pathFamily === "" ? code.family : `path:${code.pathFamily}`;
    let group = groups.find((each) => groupKey(each) === key);
    if (group === undefined) {
      group =
        code.pathFamily === ""
          ? {
              heading: INDEPENDENT_FAMILIES.get(code.family) ?? code.family,
              codes: [],
            }
          : {
              heading: routeLabel(payload, code.pathFamily),
              path: payload.paths.find(
                (path) => path.family === code.pathFamily,
              ),
              codes: [],
            };
      groups.push(group);
    }
    group.codes.push(code);
  }
  return groups;
}

function groupKey(group: CodeGroup): string {
  return group.path === undefined
    ? (group.codes[0]?.family ?? "")
    : `path:${group.path.family}`;
}

/** The label the routing gives a path, e.g. "amino-acid (MIS_)". */
export function routeLabel(
  payload: Svcv4Classification,
  family: string,
): string {
  const route = payload.routing?.routes.find((each) => each.family === family);
  return route?.label ?? `${family}_`;
}

/** The families of the paths the tally counts. */
function countedFamilies(payload: Svcv4Classification): Set<string> {
  return new Set(
    payload.paths.filter((path) => path.selected).map((path) => path.family),
  );
}

/** Whether the tally counts `code`: it is scored, and off any path or on the one the tally counts. */
export function counted(
  payload: Svcv4Classification,
  code: Svcv4Classification_Code,
): boolean {
  return (
    code.points !== "" &&
    (code.pathFamily === "" || countedFamilies(payload).has(code.pathFamily))
  );
}

/** An alternative the payload carries, with the letter the ruler, the codes and the table name it
 *  by: letters, so a reader never takes one for a number of points. */
export interface Lettered {
  alternative: Svcv4Classification["sensitivity"][number];
  kind: "open" | "sensitivity";
  mark: string;
}

/** The payload's alternatives, open values first, lettered in that order. */
export function lettered(payload: Svcv4Classification): Lettered[] {
  return [
    ...payload.openValues.map((alternative) => ({
      alternative,
      kind: "open" as const,
    })),
    ...payload.sensitivity.map((alternative) => ({
      alternative,
      kind: "sensitivity" as const,
    })),
  ].map((each, index) => ({ ...each, mark: alternativeMark(index) }));
}

/** The letter of the alternative at `index`: A to Z, then AA, AB and on, as spreadsheet columns
 *  run. */
export function alternativeMark(index: number): string {
  if (!Number.isInteger(index) || index < 0) {
    throw new Error(`no letter for alternative ${index}`);
  }
  let mark = "";
  for (let rest = index + 1; rest > 0; rest = Math.floor((rest - 1) / 26)) {
    mark = String.fromCharCode(65 + ((rest - 1) % 26)) + mark;
  }
  return mark;
}

/** For each code an alternative moves into another class than the tally's, the letters of the
 *  alternatives that do: the calls that decide the class, and what decides them. */
export function decisiveMarks(
  payload: Svcv4Classification,
): Map<string, string[]> {
  const reported = payload.tally?.finalClass;
  const decisive = new Map<string, string[]>();
  for (const { alternative, mark } of lettered(payload)) {
    if (alternative.tally?.finalClass === reported) continue;
    for (const code of alternative.codes) {
      decisive.set(code, [...(decisive.get(code) ?? []), mark]);
    }
  }
  return decisive;
}

/** Why a curator should look at `code`, or undefined where nothing asks them to: nobody has ticked
 *  it, and either the agent does not consider the call settled or an alternative the payload
 *  carries moves it into another class. */
export function reviewReason(
  code: Svcv4Classification_Code,
  decisive: ReadonlyMap<string, readonly string[]>,
  reviewed: boolean,
): string | undefined {
  if (reviewed) return undefined;
  const reasons: string[] = [];
  if (code.confidence === Confidence.OPEN) reasons.push("the call is open");
  if (code.confidence === Confidence.LEANING) reasons.push("the call leans");
  const marks = decisive.get(code.code);
  if (marks !== undefined) {
    reasons.push(
      `${marks.join(", ")} ${marks.length === 1 ? "moves" : "move"} the class`,
    );
  }
  return reasons.length === 0 ? undefined : reasons.join("; ");
}

/** One label over the ruler's needles: the letters of the needles it names, drawn at `at`. */
export interface NeedleLabel {
  at: number;
  marks: string[];
}

/** The needles' labels, merged where they would overlap. A label is `width(marks)` wide and sits
 *  centred on the mean of its needles, moved in where that would take it past `low` or `high`; two
 *  labels closer than `gap` edge to edge become one, and a merged label, being wider, is checked
 *  against the one before it again. Every argument shares the unit the caller positions in. */
export function needleLabels(
  placed: readonly { mark: string; at: number }[],
  width: (marks: readonly string[]) => number,
  gap: number,
  [low, high]: readonly [number, number],
): NeedleLabel[] {
  const labels: { ats: number[]; marks: string[] }[] = [];
  // The mean of a label's needles, moved in far enough that the label stays within [low, high].
  const centre = (label: { ats: number[]; marks: string[] }) => {
    const mean = label.ats.reduce((sum, at) => sum + at, 0) / label.ats.length;
    const half = width(label.marks) / 2;
    if (high - low <= 2 * half) return (low + high) / 2;
    return Math.min(Math.max(mean, low + half), high - half);
  };
  const overlap = (
    left: { ats: number[]; marks: string[] },
    right: { ats: number[]; marks: string[] },
  ) =>
    centre(right) - centre(left) <
    (width(left.marks) + width(right.marks)) / 2 + gap;
  for (const needle of [...placed].sort((a, b) => a.at - b.at)) {
    let label = { ats: [needle.at], marks: [needle.mark] };
    for (
      let last = labels.at(-1);
      last !== undefined && overlap(last, label);
      last = labels.at(-1)
    ) {
      labels.pop();
      label = {
        ats: [...last.ats, ...label.ats],
        marks: [...last.marks, ...label.marks],
      };
    }
    labels.push(label);
  }
  return labels.map((label) => ({ at: centre(label), marks: label.marks }));
}

/** One run of points on the ruler: a code's contribution, or points a cap took away. */
export interface Segment {
  /** The code, or the cap line's label. */
  label: string;
  /** The segment's ends in points, `from` nearer zero. */
  from: number;
  to: number;
  kind: "code" | "cut";
}

/** The ruler's two stacks: the counted codes with positive points stacked rightwards from zero,
 *  those with negative points leftwards, each in the payload's order, then each award the tally
 *  counts on its side, and each cap it counts drawn as a cut over the end of the stack it took
 *  points from. A line of a kind this build does not know is left off. */
export function stacks(payload: Svcv4Classification): Segment[] {
  const segments: Segment[] = [];
  let right = 0;
  let left = 0;
  const push = (label: string, value: number) => {
    if (value > 0) {
      segments.push({ label, from: right, to: right + value, kind: "code" });
      right += value;
    } else if (value < 0) {
      segments.push({ label, from: left, to: left + value, kind: "code" });
      left += value;
    }
  };
  for (const code of payload.codes) {
    if (counted(payload, code)) push(code.code, pointsOf(code.points));
  }
  const families = countedFamilies(payload);
  const lines = payload.lines.filter(
    (line) => line.pathFamily === "" || families.has(line.pathFamily),
  );
  // An award adds points like a code; a cap then cuts the end of the stack it bounded.
  for (const line of lines) {
    if (line.kind === Svcv4Classification_Line_Kind.AWARD) {
      push(line.label, pointsOf(line.points));
    }
  }
  for (const line of lines) {
    if (line.kind !== Svcv4Classification_Line_Kind.CAP) continue;
    const value = pointsOf(line.points);
    // A cap moves a subtotal towards zero: a negative adjustment off the pathogenic stack.
    if (value < 0) {
      segments.push({
        label: line.label,
        from: right + value,
        to: right,
        kind: "cut",
      });
      right += value;
    } else if (value > 0) {
      segments.push({
        label: line.label,
        from: left + value,
        to: left,
        kind: "cut",
      });
      left += value;
    }
  }
  return segments;
}

/** A total the ruler marks: the tally's, or an alternative's. */
export interface Needle {
  total: number;
  classification: Classification;
  /** The assumption behind an alternative; empty for the tally the codes sum to. */
  assumption: string;
  kind: "tally" | "open" | "sensitivity";
  /** The alternative's letter; empty for the tally. */
  mark: string;
}

export function needles(payload: Svcv4Classification): Needle[] {
  const tally = payload.tally;
  if (tally === undefined) throw new Error("the payload carries no tally");
  return [
    {
      total: pointsOf(tally.total),
      classification: tally.finalClass,
      assumption: "",
      kind: "tally",
      mark: "",
    },
    ...lettered(payload).map(({ alternative, kind, mark }) => {
      if (alternative.tally === undefined) {
        throw new Error(
          `the alternative ${alternative.assumption} carries no tally`,
        );
      }
      return {
        total: pointsOf(alternative.tally.total),
        classification: alternative.tally.finalClass,
        assumption: alternative.assumption,
        kind,
        mark,
      };
    }),
  ];
}

/** The span of points the ruler draws: every stack end, every needle and every finite band edge,
 *  with a point's margin, and never narrower than the band edges nearest zero on either side. */
export function domain(payload: Svcv4Classification): [number, number] {
  const values = [0];
  for (const segment of stacks(payload)) values.push(segment.from, segment.to);
  for (const needle of needles(payload)) values.push(needle.total);
  for (const band of payload.bands) {
    if (band.lower !== "") values.push(pointsOf(band.lower));
    if (band.upper !== "") values.push(pointsOf(band.upper));
  }
  return [
    Math.floor(Math.min(...values)) - 1,
    Math.ceil(Math.max(...values)) + 1,
  ];
}

/** Where `value` sits along the ruler, as a percentage of its width. */
export function position(value: number, [low, high]: [number, number]): number {
  return ((value - low) / (high - low)) * 100;
}

/** A band's span on the ruler, its open sides clamped to the ruler's ends. */
export function bandSpan(
  band: Svcv4Classification["bands"][number],
  [low, high]: [number, number],
): [number, number] {
  const lower = band.lower === "" ? low : Math.max(low, pointsOf(band.lower));
  const upper = band.upper === "" ? high : Math.min(high, pointsOf(band.upper));
  return [lower, upper];
}

/** A band's bounds as a reader reads them, e.g. `6 ≤ total < 10`. */
export function bandBounds(band: Svcv4Classification["bands"][number]): string {
  const parts: string[] = [];
  if (band.lower !== "") {
    parts.push(
      `${plain(Number(band.lower))} ${band.lowerInclusive ? "≤" : "<"}`,
    );
  }
  parts.push("total");
  if (band.upper !== "") {
    parts.push(
      `${band.upperInclusive ? "≤" : "<"} ${plain(Number(band.upper))}`,
    );
  }
  return parts.join(" ");
}

/** A number of points unsigned but for a true minus, as an axis labels it. */
export function plain(value: number): string {
  return value < 0 ? `−${Math.abs(value)}` : String(value);
}

/** Whether two decimals the payload carries are the same number, whatever their text. */
export function samePoints(a: string, b: string): boolean {
  return pointsOf(a) === pointsOf(b);
}

const CONSEQUENCES: ReadonlyMap<Consequence, string> = new Map([
  [Consequence.MISSENSE, "Missense"],
  [Consequence.NONSENSE, "Nonsense"],
  [Consequence.FRAMESHIFT, "Frameshift"],
  [Consequence.CANONICAL_SPLICE, "Canonical splice"],
  [Consequence.INTRONIC, "Intronic"],
  [Consequence.SYNONYMOUS, "Synonymous"],
  [Consequence.INFRAME_INDEL, "In-frame indel"],
  [Consequence.START_LOST, "Start lost"],
  [Consequence.STOP_LOST, "Stop lost"],
  [Consequence.EXON_DELETION, "Exon deletion"],
  [Consequence.EXON_DUPLICATION, "Exon duplication"],
  [Consequence.NON_CODING, "Non-coding"],
]);

export function consequenceLabel(value: Consequence): string {
  return CONSEQUENCES.get(value) ?? "unknown consequence";
}

const INHERITANCES: ReadonlyMap<Inheritance, string> = new Map([
  [Inheritance.AUTOSOMAL_DOMINANT, "autosomal dominant"],
  [Inheritance.AUTOSOMAL_RECESSIVE, "autosomal recessive"],
  [Inheritance.X_LINKED, "X-linked"],
  [Inheritance.Y_LINKED, "Y-linked"],
  [Inheritance.MITOCHONDRIAL, "mitochondrial"],
  [Inheritance.SEMIDOMINANT, "semidominant"],
  [Inheritance.UNDETERMINED, "inheritance undetermined"],
]);

export function inheritanceLabel(value: Inheritance): string {
  return INHERITANCES.get(value) ?? "unknown inheritance";
}

const GATE_LEVELS: ReadonlyMap<GateLevel, string> = new Map([
  [GateLevel.DEFINITIVE, "Definitive"],
  [GateLevel.STRONG, "Strong"],
  [GateLevel.MODERATE, "Moderate"],
  [GateLevel.LIMITED, "Limited"],
  [GateLevel.LESS_THAN_LIMITED, "below Limited"],
  [GateLevel.DISPUTED_OR_REFUTED, "Disputed or refuted"],
]);

export function gateLevelLabel(value: GateLevel): string {
  return GATE_LEVELS.get(value) ?? "unknown";
}
