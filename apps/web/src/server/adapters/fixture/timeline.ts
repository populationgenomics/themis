import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  type Analysis,
  type ConversationEvent,
  ConversationEventSchema,
} from "@/models/workbench";

// The scripted run the fixture reveals one stage per poll. Deterministic: a pure
// function of (analysis, revealed-stage-count), so the poll→update loop is
// reproducible. Event ids are stable across ticks so the client reconciles the full
// event list by id (replace-by-id) each poll.

const DOC_VERSION = 1;

// A fixed epoch so each event's occurred_at is deterministic (the run is a pure
// function of its inputs); events are stamped one second apart in emission order.
const TIMELINE_EPOCH_MS = Date.UTC(2024, 0, 1);

function eventTime(order: number) {
  return timestampFromDate(new Date(TIMELINE_EPOCH_MS + order * 1000));
}

/** The working document the run produces at its final stage. Reports the `hello`
 *  result for this analysis, in the markdown grammar the document pane renders
 *  (`### ` headings, `**bold**`, paragraphs). */
function renderDocument(analysis: Analysis): string {
  return [
    "This working document was produced by the fixture backend to exercise the create → poll → document loop end to end.",
    "### Tool activity",
    "The agent wrote this file to `/workspace/document.md` and called the **hello** service over the forward leg in code mode.",
    "### hello result",
    `The **hello** probe resolved the injected session token to its binding — greeting \`${HELLO_GREETING}\`, analysis \`${analysis.id}\`, project \`${analysis.projectId}\`.`,
  ].join("\n\n");
}

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

// The two code-mode shell calls the run makes, revealed at stage 2. `command` is the
// full invocation the tool-call row shows on expand; the result is the call's stdout.
function writeCommand(analysis: Analysis): string {
  return `cat > /workspace/document.md <<'EOF'
${renderDocument(analysis)}
EOF
echo "wrote $(wc -l < /workspace/document.md) lines to /workspace/document.md"`;
}

const HELLO_COMMAND = `python3 - <<'EOF'
from themis.agent import services
from themis.rpc import hello_pb2

reply = services.hello().SayHello(hello_pb2.SayHelloRequest(note="forward-leg probe"))
print("greeting:", reply.greeting)
print("analysis:", reply.analysis_id)
print("project:", reply.project_id)
EOF`;

function narration(
  kind: "assistant" | "user",
  id: string,
  order: number,
  text: string,
): ConversationEvent {
  return create(ConversationEventSchema, {
    id,
    occurredAt: eventTime(order),
    kind: { case: kind, value: { text } },
  });
}

function toolCall(
  id: string,
  order: number,
  intent: string,
  command: string,
  output: string,
): ConversationEvent {
  return create(ConversationEventSchema, {
    id,
    occurredAt: eventTime(order),
    kind: {
      case: "tool",
      value: { name: "shell", intent, command, result: { output } },
    },
  });
}

/** The per-stage event contributions, revealed cumulatively. Stage 1: the user
 *  kickoff + the assistant's opening narration. Stage 2: the two tool-call lines
 *  (each with its result). Stage 3: the closing narration (the run completes here). */
const STAGES: ReadonlyArray<(analysis: Analysis) => ConversationEvent[]> = [
  (analysis) => [
    narration("user", "ev-kickoff", 0, analysis.prompt),
    narration(
      "assistant",
      "ev-open",
      1,
      "Starting the run. Reading the workspace and preparing to write the working document.",
    ),
  ],
  (analysis) => [
    toolCall(
      "ev-write",
      2,
      "write the working document",
      writeCommand(analysis),
      `wrote ${renderDocument(analysis).split("\n").length} lines to /workspace/document.md`,
    ),
    toolCall(
      "ev-hello",
      3,
      "call the hello service over the forward leg",
      HELLO_COMMAND,
      helloOutput(analysis),
    ),
  ],
  () => [
    narration(
      "assistant",
      "ev-close",
      4,
      "Wrote the working document and confirmed the **hello** probe over the forward leg. The run is now complete.",
    ),
  ],
];

/** How many stages the run has. */
export function stageCount(): number {
  return STAGES.length;
}

export interface TimelineTick {
  events: ConversationEvent[];
  /** The produced document version once the run reaches its final stage, else 0. */
  documentVersion: number;
}

/** The run state after `revealed` stages (clamped to [0, stageCount]). */
export function timelineAt(analysis: Analysis, revealed: number): TimelineTick {
  const shown = Math.min(Math.max(revealed, 0), STAGES.length);
  const atFinal = shown >= STAGES.length;
  return {
    events: STAGES.slice(0, shown).flatMap((stage) => stage(analysis)),
    documentVersion: atFinal ? DOC_VERSION : 0,
  };
}

/** The markdown for a produced document version. Throws on an unknown version —
 *  the run produces exactly one. */
export function documentMarkdown(analysis: Analysis, version: number): string {
  if (version !== DOC_VERSION) {
    throw new Error(`no such document version: ${version}`);
  }
  return renderDocument(analysis);
}
