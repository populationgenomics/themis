import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { requireInputs } from "@/lib/scenario";
import {
  type Analysis,
  type ConversationEvent,
  ConversationEventSchema,
  SubAgentStatus,
} from "@/models/workbench";
import { kickoffText } from "../../kickoff";
import { projectToolCall } from "../../tool-projection";
import { DOC_OCR, DOC_XML, XML_QUOTE } from "./literature";

// The scripted run the fixture reveals one stage per poll. Deterministic: a pure
// function of (analysis, run state), so the poll→update loop is reproducible. Event
// ids are stable across ticks so the client reconciles the full event list by id
// (replace-by-id) each poll.
//
// The script is not closed: a curator turn splices two stages into it at the frontier
// where the turn was made. The state that grows is the caller's; this module stays a
// pure function of it.

// A fixed epoch so each event's occurred_at is deterministic (the run is a pure
// function of its inputs); events are stamped one second apart in emission order.
const TIMELINE_EPOCH_MS = Date.UTC(2024, 0, 1);

function eventTime(order: number) {
  return timestampFromDate(new Date(TIMELINE_EPOCH_MS + order * 1000));
}

/** A curator turn the run has taken: the prose, and the reveal frontier it arrived
 *  at — so the turn stays where it was made rather than sliding past the ending as
 *  the script catches up. */
export interface Interjection {
  text: string;
  at: number;
}

/** The whole of a run's state. The run is a pure function of it, so a poll sequence
 *  is reproducible and every event id is stable across ticks. */
export interface RunState {
  revealed: number;
  interjections: readonly Interjection[];
  /** Pinned at the revealed stage: a poll releases nothing further; a curator turn
   *  releases it (docs/design/conversation-view.md). */
  held: boolean;
  /** How many script stages the curator's halt left standing, `null` while the run
   *  was never halted. The stages past it never play; a call the halt caught in
   *  flight closes with an error result. */
  scriptKept: number | null;
}

/** A run that has revealed nothing. */
export function initialRunState(): RunState {
  return { revealed: 0, interjections: [], held: false, scriptKept: null };
}

/** The state after one poll tick: one more stage released. Clamped, so a finished run
 *  does not accumulate a frontier past its end that a later turn would then splice
 *  behind. */
export function afterPoll(state: RunState): RunState {
  if (state.held) return state;
  return {
    ...state,
    revealed: Math.min(state.revealed + 1, stageCount(state)),
  };
}

/** Take a curator turn. It is released at once — the turn did happen, so there is
 *  nothing to reveal progressively — and the agent taking it up is the next stage a
 *  poll reveals.
 *
 *  The turn lands at the frontier, but never before the kickoff stage (a curator
 *  cannot answer a run that has not started) and never inside an earlier turn's block,
 *  which would put the agent's replies in the reverse of the order the turns were made
 *  in. A second turn made inside the first's block therefore also releases the first's
 *  reply, which is what a run that has been spoken to twice looks like. */
export function steered(state: RunState, text: string): RunState {
  const previous = state.interjections[state.interjections.length - 1];
  const earliest = previous ? previous.at + INTERJECTION_STAGES : KICKOFF_STAGE;
  const at = Math.max(Math.min(state.revealed, stageCount(state)), earliest);
  return {
    ...state,
    revealed: at + 1,
    interjections: [...state.interjections, { text, at }],
    held: false,
  };
}

/** Halt the run where it is, as the live `user.interrupt` does to a running session:
 *  the script past the reveal frontier never plays, and a call the halt caught in
 *  flight closes with the error result the live API synthesizes — shown at once. A
 *  pending turn's uptake is NOT released: live processes a queued message only after
 *  the interrupt idles the session, so it stays for later polls. Against a settled
 *  run, or one whose kickoff has not revealed, it is a no-op, matching live's
 *  treatment of an idle session. A held run halts like a running one, and the halt
 *  releases the hold: there is nothing left for a turn to release.
 */
export function interrupted(state: RunState): RunState {
  const total = stageCount(state);
  const frontier = Math.min(state.revealed, total);
  if (frontier < KICKOFF_STAGE || frontier >= total) return state;
  // Capped at a prior halt's script: the closing error stage is not a script index,
  // so a recount against a halted arrangement would resurrect cancelled script.
  const kept = Math.min(
    scriptStagesBelow(frontier, state),
    state.scriptKept ?? SCRIPTED_STAGES,
  );
  // A closure this halt creates sits just past the frontier; revealing it is the
  // halt's one immediate consequence.
  const closureCreated = state.scriptKept === null && haltClosesCall(kept);
  return {
    ...state,
    held: false,
    scriptKept: kept,
    revealed: frontier + (closureCreated ? 1 : 0),
  };
}

/** How many of the frontier's revealed stages came from the script — the rest are
 *  interjection stages, each turn occupying its two slots from `at`. */
function scriptStagesBelow(frontier: number, state: RunState): number {
  const interjectionSlots = state.interjections.reduce(
    (slots, interjection) =>
      slots +
      Math.min(Math.max(frontier - interjection.at, 0), INTERJECTION_STAGES),
    0,
  );
  return frontier - interjectionSlots;
}

/** True while the revealed frontier holds a tool call awaiting its result — the
 *  state in which the live session refuses a curator turn. */
export function awaitingToolResult(
  analysis: Analysis,
  state: RunState,
): boolean {
  return timelineAt(analysis, state).events.some(
    (event) =>
      event.kind.case === "tool" && event.kind.value.result === undefined,
  );
}

/** The Sources paragraph as the agent first drafts it: the papers named, with neither
 *  the quotes nor the malformed reference the run later corrects it to carry. */
const SOURCES_DRAFT = `### Sources

The finding draws on :paper[${DOC_XML}].
A scanned source :paper[${DOC_OCR}] is also cited.`;

/** The same paragraph after the `edit`. Citation directives (`:paper` / `:quote`) the
 *  document pane renders as clickable citations — a locatable quote, a quote absent from
 *  its paper (the warning chip), and a malformed id (the broken-citation marker). The ids
 *  and quote come from the FixtureLiterature corpus. */
const SOURCES_FINAL = `### Sources

The finding draws on :paper[${DOC_XML}], specifically that :quote[${DOC_XML}, ${XML_QUOTE}].
A scanned source :paper[${DOC_OCR}] is also cited, though the phrase :quote[${DOC_OCR}, a sentence not present in the scan] cannot be located in it.
A malformed reference :paper[not-a-real-id] renders as broken.`;

/** The document the agent writes at stage 2, before it corrects the Sources paragraph. */
function documentDraft(analysis: Analysis): string {
  return [
    "This working document was produced by the fixture backend to exercise the create → poll → document loop end to end.",
    "### Tool activity",
    "The agent wrote this file to `/workspace/working_document.md` and called the **hello** service over the forward leg in code mode.",
    "### hello result",
    `The **hello** probe resolved the injected session token to its binding — greeting \`${HELLO_GREETING}\`, analysis \`${analysis.id}\`, project \`${analysis.projectId}\`.`,
    SOURCES_DRAFT,
  ].join("\n\n");
}

/** The working document the run produces at its final stage: the draft with the `edit`
 *  applied. Derived rather than written out, so the edit the conversation shows and the
 *  document the pane serves cannot disagree. */
function renderDocument(analysis: Analysis): string {
  const draft = documentDraft(analysis);
  if (!draft.includes(SOURCES_DRAFT)) {
    throw new Error("the scripted edit does not apply to the drafted document");
  }
  return draft.replace(SOURCES_DRAFT, SOURCES_FINAL);
}

/** The produced document bodies, v1 first: the draft the `write` lands, then the
 *  revision the `edit` corrects it to. Stage `documentVersion`s index into this. */
const DOCUMENT_VERSIONS: ReadonlyArray<(analysis: Analysis) => string> = [
  documentDraft,
  renderDocument,
];

/** The highest version the whole script produces. */
export const FINAL_DOC_VERSION = DOCUMENT_VERSIONS.length;

const HELLO_GREETING = "hello from the forward leg (note: forward-leg probe)";

/** The `hello` call's stdout: the greeting plus the binding the session token
 *  resolved to — the ids of the analysis actually being run. */
function helloOutput(analysis: Analysis): string {
  return [
    `greeting: ${HELLO_GREETING}`,
    `analysis: ${analysis.id}`,
    `project: ${analysis.projectId}`,
  ].join("\n");
}

const DOCUMENT_PATH = "/workspace/working_document.md";

/** Scratch the scripted agent invents for itself; only the working document is contract. */
const SCRATCH_NOTES_PATH = "/workspace/scratch-notes.md";

const CITATION_SCRIPT_PATH = "/workspace/count_citations.py";

const CITATION_SCRIPT = `import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
print("papers:", len(re.findall(r":paper\\[([^\\]]+)\\]", text)))
print("quotes:", len(re.findall(r":quote\\[([^\\]]+)\\]", text)))
`;

const CITATION_COMMAND = `python3 "${CITATION_SCRIPT_PATH}" "${DOCUMENT_PATH}"`;

/** What the citation script prints for a document — derived from the same text the run
 *  writes, so the count and the document cannot disagree. */
function citationCounts(markdown: string): string {
  const found = (directive: string) =>
    markdown.split(`:${directive}[`).length - 1;
  return `papers: ${found("paper")}\nquotes: ${found("quote")}`;
}

/** Lines as a file carries them: a trailing newline ends the last line, rather than
 *  opening an empty one after it. */
function lineCount(text: string): number {
  return (text.endsWith("\n") ? text.slice(0, -1) : text).split("\n").length;
}

const HELLO_COMMAND = `python3 - <<'EOF'
from themis.agent import services
from themis.rpc import hello_pb2

reply = services.hello().SayHello(hello_pb2.SayHelloRequest(note="forward-leg probe"))
print("greeting:", reply.greeting)
print("analysis:", reply.analysis_id)
print("project:", reply.project_id)
EOF`;

// An event before it is stamped. `occurred_at` is assigned by position in the merged
// stream, not written per event: an interjection splices stages in, so a hand-written
// order would have to be kept in sync with every insertion.
// The id is required rather than optional: it is the fold key, and an event without one
// would silently overwrite another.
type EventInit = Omit<
  // The init shape is a union of "a built message" and "a plain object"; only the
  // latter can be spread with a stamp.
  Extract<
    MessageInitShape<typeof ConversationEventSchema>,
    { $typeName?: undefined }
  >,
  "occurredAt" | "id"
> & { id: string };

function narration(
  kind: "assistant" | "user",
  id: string,
  text: string,
): EventInit {
  return { id, kind: { case: kind, value: { text } } };
}

/** A tool call, projected from a real tool input by the same function the live adapter
 *  uses. An absent `result` is a call still in flight. */
function toolEvent(args: {
  id: string;
  name: string;
  input: Record<string, unknown>;
  result?: { output: string; isError?: boolean };
}): EventInit {
  return {
    id: args.id,
    kind: {
      case: "tool",
      value: { ...projectToolCall(args.name, args.input), result: args.result },
    },
  };
}

/** How far a spawned thread has got. `spawned` is the window between the thread being
 *  created and the coordinator's instruction landing — the card has no prompt to show
 *  yet. */
type ThreadPhase = "spawned" | "working" | "returned";

/** One sub-agent the scripted run delegates to. The card and the body it expands to are
 *  both derived from this, so what the fan-out says and what the thread did cannot
 *  disagree. */
interface ThreadScript {
  threadId: string;
  prompt: string;
  summary: string;
  /** Where the thread settles once it has returned — a thread that yielded is idle and
   *  can be followed up; a terminated one is done. */
  finalStatus: SubAgentStatus;
  /** The thread's own events while it works, then what returning added. The second may
   *  re-emit an id from the first, which is how a call in flight is later resolved. */
  working: EventInit[];
  returned: EventInit[];
}

const FREQUENCY_COMMAND = `python3 - <<'EOF'
from themis.agent import services

print(services.gnomad().joint_frequency(transcript, hgvs_c))
EOF`;

// A thread's reply appears twice — the card's summary, and the closing narration that
// repeats it — so each is one constant.
const LITERATURE_REPLY =
  "## Functional evidence — two corpus papers\n\n1. **Transactivation assay**: activity reduced to **21% of wild type** in a luciferase reporter; benign and pathogenic controls behaved as expected.\n2. **Minigene splicing assay**: no effect on splicing — the transcript is spliced normally, so the loss of function is not splice-mediated.\n\nNo patient-RNA study exists in the corpus; the abstract-only literature reports none either.";

const FREQUENCY_REPLY =
  "gnomAD v4 joint: 3 alleles in 1,613,730, allele frequency 1.9e-06. That is two orders of magnitude under the 0.001 BA1 threshold, so BA1 does not apply.";

const SUB_AGENTS: readonly ThreadScript[] = [
  {
    threadId: "sthr_literature",
    prompt:
      "Search the corpus for **functional evidence** on this variant — literature sweep only; I own the judgement and the codes.\n\nReturn every paper that reports an assay, and for each:\n\n- the assay type (transactivation, minigene, RNA from patient cells)\n- what it showed, quoting the exact sentence with the measurement\n- whether controls were run",
    summary: LITERATURE_REPLY,
    finalStatus: SubAgentStatus.IDLE,
    working: [
      narration(
        "assistant",
        "sthr_literature-open",
        "Searching the corpus for assay reports, then reading each hit.",
      ),
      toolEvent({
        id: "sthr_literature-grep",
        name: "grep",
        input: { pattern: "transactivation|minigene", path: "/corpus" },
        result: { output: `/corpus/${DOC_XML}.md\n/corpus/${DOC_OCR}.md` },
      }),
      toolEvent({
        id: "sthr_literature-read",
        name: "read",
        input: { file_path: `/corpus/${DOC_XML}.md`, view_range: [1, 80] },
        result: { output: XML_QUOTE },
      }),
    ],
    returned: [
      narration("assistant", "sthr_literature-close", LITERATURE_REPLY),
    ],
  },
  {
    threadId: "sthr_frequency",
    prompt:
      "Pull the gnomAD v4 joint frequency for this variant and say whether it clears the gene's BA1 threshold.",
    summary: FREQUENCY_REPLY,
    finalStatus: SubAgentStatus.DONE,
    working: [
      narration(
        "assistant",
        "sthr_frequency-open",
        "Querying the gnomAD v4 joint callset over the forward leg.",
      ),
      toolEvent({
        id: "sthr_frequency-query",
        name: "shell",
        input: {
          command: FREQUENCY_COMMAND,
          intent: "read the gnomAD v4 joint frequency",
        },
      }),
    ],
    returned: [
      toolEvent({
        id: "sthr_frequency-query",
        name: "shell",
        input: {
          command: FREQUENCY_COMMAND,
          intent: "read the gnomAD v4 joint frequency",
        },
        result: { output: "AC=3 AN=1613730 AF=1.9e-06 popmax=nfe" },
      }),
      narration("assistant", "sthr_frequency-close", FREQUENCY_REPLY),
    ],
  },
];

/** A sub-agent card at one phase. Re-emitted under the same id as the thread advances,
 *  so it is replaced where it first appeared rather than repeating further down. */
function subAgentEvent(script: ThreadScript, phase: ThreadPhase): EventInit {
  return {
    id: script.threadId,
    kind: {
      case: "subAgent",
      value: {
        threadId: script.threadId,
        status:
          phase === "returned" ? script.finalStatus : SubAgentStatus.RUNNING,
        prompt: phase === "spawned" ? undefined : script.prompt,
        summary: phase === "returned" ? script.summary : undefined,
      },
    },
  };
}

/** The `edit` the run makes to the Sources paragraph — in flight, resolved, or
 *  closed with the error result a halt leaves behind. */
function editSources(result?: {
  output: string;
  isError?: boolean;
}): EventInit {
  return toolEvent({
    id: "ev-edit",
    name: "edit",
    input: {
      file_path: DOCUMENT_PATH,
      old_string: SOURCES_DRAFT,
      new_string: SOURCES_FINAL,
    },
    result,
  });
}

interface Stage {
  events: EventInit[];
  /** The working-document version this stage has produced, 0 for none. */
  documentVersion: number;
}

/** The per-stage contributions of the scripted run, revealed cumulatively. A stage may
 *  re-emit an event id an earlier one carried; the later emission replaces it in place,
 *  which is how a call is shown in flight and then resolved. */
const STAGES: ReadonlyArray<(analysis: Analysis) => Stage> = [
  (analysis) => ({
    documentVersion: 0,
    events: [
      narration("user", "ev-kickoff", kickoffText(requireInputs(analysis))),
      narration(
        "assistant",
        "ev-open",
        "Starting the run. Reading the workspace and preparing to write the working document.",
      ),
    ],
  }),
  (analysis) => ({
    documentVersion: 1,
    events: [
      toolEvent({
        id: "ev-read",
        name: "read",
        input: { file_path: DOCUMENT_PATH, view_range: [1, 120] },
        result: {
          output: `read: ${DOCUMENT_PATH}: No such file or directory`,
          isError: true,
        },
      }),
      toolEvent({
        id: "ev-write",
        name: "write",
        input: { file_path: DOCUMENT_PATH, content: documentDraft(analysis) },
        result: {
          output: `wrote ${lineCount(documentDraft(analysis))} lines to ${DOCUMENT_PATH}`,
        },
      }),
    ],
  }),
  () => ({
    documentVersion: 0,
    events: [
      narration(
        "assistant",
        "ev-fanout",
        "Delegating the evidence gathering: one pass over the literature corpus, one population-frequency check.",
      ),
      ...SUB_AGENTS.map((script) => subAgentEvent(script, "spawned")),
    ],
  }),
  () => ({
    documentVersion: 0,
    events: SUB_AGENTS.map((script) => subAgentEvent(script, "working")),
  }),
  () => ({
    documentVersion: 0,
    events: [subAgentEvent(SUB_AGENTS[0], "returned")],
  }),
  (analysis) => ({
    documentVersion: 0,
    events: [
      subAgentEvent(SUB_AGENTS[1], "returned"),
      toolEvent({
        id: "ev-script",
        name: "write",
        input: {
          file_path: CITATION_SCRIPT_PATH,
          content: CITATION_SCRIPT,
        },
        result: {
          output: `wrote ${lineCount(CITATION_SCRIPT)} lines to ${CITATION_SCRIPT_PATH}`,
        },
      }),
      toolEvent({
        id: "ev-citations",
        name: "shell",
        input: {
          command: CITATION_COMMAND,
          intent: "count the citation directives the draft carries",
        },
        result: { output: citationCounts(documentDraft(analysis)) },
      }),
      toolEvent({
        id: "ev-hello",
        name: "shell",
        input: {
          command: HELLO_COMMAND,
          intent: "call the hello service over the forward leg",
        },
        result: { output: helloOutput(analysis) },
      }),
    ],
  }),
  () => ({ documentVersion: 0, events: [editSources()] }),
  () => ({
    documentVersion: 2,
    events: [
      editSources({ output: `applied 1 edit to ${DOCUMENT_PATH}` }),
      narration(
        "assistant",
        "ev-close",
        `Wrote the working document, confirmed the **hello** probe over the forward leg, and corrected the Sources paragraph. The regulatory finding is supported by :paper[${DOC_XML}]. The run is now complete.`,
      ),
    ],
  }),
];

/** How many stages the run has before any curator turn — what a seeded finished run
 *  is marked with. */
export const SCRIPTED_STAGES = STAGES.length;

/** Released-stage counts a seeded run is held at: every card spawned and none
 *  instructed yet, and one thread returned while its sibling still runs
 *  (docs/design/conversation-view.md). Pinned by test, since a stage inserted before
 *  the fan-out moves both. */
export const FANOUT_SPAWNED_REVEAL = 3;
export const FANOUT_PARTIAL_REVEAL = 5;

/** The index in `STAGES` of the mid-step window — the stage whose `edit` is in
 *  flight; an interrupt truncates the script just past it. Pinned by test for the
 *  reason the reveal counts above are. */
const PENDING_STAGE = 6;

/** What the live API writes into a tool call it halts, mirrored verbatim. */
const INTERRUPTED_RESULT =
  "Tool execution was interrupted before completion. Please retry.";

/** True when the halt caught the edit in flight: the kept script ends on the
 *  mid-step window, so a closing error-result stage follows it. */
function haltClosesCall(scriptKept: number): boolean {
  return scriptKept === PENDING_STAGE + 1;
}

/** How many stages one curator turn contributes: the turn, then the agent taking it
 *  up. */
const INTERJECTION_STAGES = 2;

/** How many leading stages carry the kickoff — the stages no curator turn precedes. */
const KICKOFF_STAGE = 1;

/** How many stages the run has, script plus every turn taken so far. */
function stageCount(state: RunState): number {
  const script =
    state.scriptKept === null
      ? SCRIPTED_STAGES
      : state.scriptKept + (haltClosesCall(state.scriptKept) ? 1 : 0);
  return script + state.interjections.length * INTERJECTION_STAGES;
}

/** The turn a curator made, and the agent taking it up — the two stages an
 *  interjection contributes. `ordinal` is 1-based and keys the ids, so a turn's
 *  events are stable across ticks and across later turns. */
function interjectionStages(text: string, ordinal: number): Stage[] {
  return [
    {
      documentVersion: 0,
      events: [narration("user", `ev-steer-${ordinal}`, text)],
    },
    {
      documentVersion: 0,
      events: [
        toolEvent({
          id: `ev-steer-${ordinal}-note`,
          name: "write",
          input: {
            file_path: SCRATCH_NOTES_PATH,
            content: `## Curator direction ${ordinal}\n\n${text}\n`,
          },
          result: {
            output: `wrote direction ${ordinal} to ${SCRATCH_NOTES_PATH}`,
          },
        }),
        narration(
          "assistant",
          `ev-steer-${ordinal}-ack`,
          `Taking that up: **${firstLine(text)}**\n\nRecorded in the scratch notes \`${SCRATCH_NOTES_PATH}\` and folded into the next pass over the working document.`,
        ),
      ],
    },
  ];
}

/** A turn's opening line, for the acknowledgement that quotes it back. Bounded so a
 *  pasted paragraph does not become the whole narration. */
function firstLine(text: string): string {
  const line = text.trim().split("\n", 1)[0] ?? "";
  return line.length > 120 ? `${line.slice(0, 119)}…` : line;
}

/** The scripted stages, halted or whole. A halted run keeps the script up to where
 *  the halt landed; a call caught in flight closes with the halt's error result, and
 *  the stages past the halt never play. Truncation cannot orphan an interjection:
 *  every recorded `at` sits at or below the frontier the halt was taken at. */
function scriptStages(analysis: Analysis, scriptKept: number | null): Stage[] {
  const stages = STAGES.map((stage) => stage(analysis));
  if (scriptKept === null) return stages;
  const kept = stages.slice(0, scriptKept);
  if (haltClosesCall(scriptKept)) {
    kept.push({
      documentVersion: 0,
      events: [editSources({ output: INTERRUPTED_RESULT, isError: true })],
    });
  }
  return kept;
}

/** The run's stages: the script, with each interjection's two stages spliced in where
 *  it arrived. Applied oldest first; each `at` indexed the list the earlier turns had
 *  already produced, and `steered` keeps them disjoint and ascending. */
function mergedStages(analysis: Analysis, state: RunState): Stage[] {
  const stages = scriptStages(analysis, state.scriptKept);
  state.interjections.forEach((interjection, index) => {
    stages.splice(
      Math.min(interjection.at, stages.length),
      0,
      ...interjectionStages(interjection.text, index + 1),
    );
  });
  return stages;
}

export interface TimelineTick {
  events: ConversationEvent[];
  /** The highest working-document version the revealed stages have produced, else 0. */
  documentVersion: number;
}

/** The run state after `state.revealed` stages (clamped to the merged script). */
export function timelineAt(analysis: Analysis, state: RunState): TimelineTick {
  const stages = mergedStages(analysis, state);
  const shown = stages.slice(
    0,
    Math.min(Math.max(state.revealed, 0), stages.length),
  );
  return {
    events: stamped(shown.map((stage) => stage.events)),
    documentVersion: shown.reduce(
      (highest, stage) => Math.max(highest, stage.documentVersion),
      0,
    ),
  };
}

/** Fold the released stages by id, last emission winning — so a stage that re-emits an
 *  event replaces it where it first appeared rather than repeating it further down —
 *  and stamp each by its position in the folded stream. A splice lands at or after the
 *  reveal frontier, so nothing already shown moves, and no stage carries a hand-written
 *  order that every later insertion would invalidate. */
function stamped(
  stages: ReadonlyArray<readonly EventInit[]>,
): ConversationEvent[] {
  const folded = new Map<string, EventInit>();
  for (const events of stages) {
    for (const init of events) folded.set(init.id, init);
  }
  return [...folded.values()].map((init, order) =>
    create(ConversationEventSchema, { ...init, occurredAt: eventTime(order) }),
  );
}

/** One spawned thread's own stream at the run's current reveal, or null for a thread
 *  the run never spawned. Derived from the card the same reveal produced — its prompt
 *  is the thread's opening turn, and its status is what settles whether the thread has
 *  returned — so the fan-out and the body a curator expands cannot disagree. */
export function threadTimeline(
  analysis: Analysis,
  state: RunState,
  threadId: string,
): ConversationEvent[] | null {
  const script = SUB_AGENTS.find((s) => s.threadId === threadId);
  if (!script) return null;
  const card = timelineAt(analysis, state).events.find(
    (event) => event.id === threadId,
  );
  if (card?.kind.case !== "subAgent") return null;
  const prompt = card.kind.value.prompt;
  if (prompt === undefined) return [];
  const working = [
    narration("user", `${threadId}-instruction`, prompt),
    ...script.working,
  ];
  const stillWorking = card.kind.value.status === SubAgentStatus.RUNNING;
  return stamped(stillWorking ? [working] : [working, script.returned]);
}

/** The markdown for a produced document version (1-based). Throws on a version the
 *  run never produces. */
export function documentMarkdown(analysis: Analysis, version: number): string {
  const render = Number.isInteger(version)
    ? DOCUMENT_VERSIONS[version - 1]
    : undefined;
  if (render === undefined) {
    throw new Error(`no such document version: ${version}`);
  }
  return render(analysis);
}
