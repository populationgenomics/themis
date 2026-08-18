import { create } from "@bufbuild/protobuf";
import { timestampDate, timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  type Analysis,
  type AnalysisInputs,
  AnalysisInputsSchema,
  AnalysisSchema,
  type DocumentResponse,
  DocumentResponseSchema,
  type PollResponse,
  PollResponseSchema,
  type ThreadResponse,
  ThreadResponseSchema,
} from "@/models/workbench";
import { ResourceNotFoundError, SessionBusyError } from "../../errors";
import type { AnalysisDataPlane, CreateAnalysisInput } from "../../ports";
import { FIXTURE_PROJECT, SECOND_FIXTURE_PROJECT } from "./membership";
import {
  afterPoll,
  awaitingToolResult,
  documentMarkdown,
  FANOUT_PARTIAL_REVEAL,
  FANOUT_SPAWNED_REVEAL,
  FINAL_DOC_VERSION,
  initialRunState,
  interrupted,
  type RunState,
  SCRIPTED_STAGES,
  steered,
  threadTimeline,
  timelineAt,
} from "./timeline";

interface Entry {
  analysis: Analysis;
  // The reveal is server-side state: each poll releases one more timeline stage, and
  // a curator turn splices two more into the script at the frontier it arrived at.
  run: RunState;
  // Monotonic: the highest working-document version the poll reveal has released.
  // 0 before the run's first write; read by `getDocument` so the document pane
  // reflects the reveal.
  revealedDocVersion: number;
}

/** Where a seeded run's reveal starts, and whether a poll advances it. A number holds
 *  the run at that many released stages (docs/design/conversation-view.md). */
type SeedReveal = "start" | "finished" | { heldAt: number };

// The prior analyses the offline navigator browses. Nothing persists across a restart, so without
// these the Projects and Project pages render empty and neither can be tried or reviewed. `agedHours`
// is how long before startup the run was created. Every clinical context below is composed for this
// fixture — no case, record, or participant is behind any of them.
const SEEDS: ReadonlyArray<{
  projectId: string;
  agedHours: number;
  reveal: SeedReveal;
  inputs: AnalysisInputs;
}> = [
  {
    projectId: FIXTURE_PROJECT,
    agedHours: 1.5,
    reveal: "finished",
    inputs: variantClassification(
      "NM_001382309.1",
      "c.332del",
      "Proband with global developmental delay, expressive language disorder and hypotonia; exome sequencing identified no other candidate variant, and parental testing confirms the variant is de novo.",
    ),
  },
  {
    projectId: FIXTURE_PROJECT,
    agedHours: 6,
    reveal: "finished",
    inputs: variantClassification(
      "NM_000059.4",
      "c.7007G>A",
      "Unaffected 41-year-old with a maternal history of early-onset breast cancer; predictive testing, no tumour tissue available.",
    ),
  },
  {
    projectId: FIXTURE_PROJECT,
    agedHours: 27,
    reveal: "start",
    inputs: variantClassification(
      "NM_000535.7",
      "c.2117del",
      "34-year-old with a Lynch-consistent tumour panel; MSI-high, loss of PMS2 on IHC, no germline confirmation yet.",
    ),
  },
  {
    projectId: FIXTURE_PROJECT,
    agedHours: 96,
    reveal: "finished",
    inputs: freeForm(
      "Re-review the MYH7 variants this cohort previously called VUS against the current gene-specific criteria, and list the ones whose class would change.",
    ),
  },
  {
    projectId: FIXTURE_PROJECT,
    agedHours: 0.4,
    reveal: { heldAt: FANOUT_SPAWNED_REVEAL },
    inputs: variantClassification(
      "NM_004006.3",
      "c.1704G>A",
      "Boy of 7 with progressive proximal weakness and calf hypertrophy; creatine kinase 14,000 U/L, no muscle biopsy.",
    ),
  },
  {
    projectId: FIXTURE_PROJECT,
    agedHours: 0.7,
    reveal: { heldAt: FANOUT_PARTIAL_REVEAL },
    inputs: variantClassification(
      "NM_000257.4",
      "c.1988G>A",
      "Asymptomatic 29-year-old with a father diagnosed with hypertrophic cardiomyopathy at 44; cascade testing, normal echocardiogram.",
    ),
  },
  {
    projectId: SECOND_FIXTURE_PROJECT,
    agedHours: 10,
    reveal: "finished",
    inputs: variantClassification(
      "NM_021007.3",
      "c.2614C>T",
      "Neonatal-onset epileptic encephalopathy, no family history; SCN2A is the only gene with a candidate variant on the panel.",
    ),
  },
  {
    projectId: SECOND_FIXTURE_PROJECT,
    agedHours: 52,
    reveal: "start",
    inputs: freeForm(
      "Summarise gnomAD v4 constraint for SCN2A and flag which missense regions are depleted.",
    ),
  },
];

function variantClassification(
  transcript: string,
  hgvsC: string,
  clinicalContext: string,
): AnalysisInputs {
  return create(AnalysisInputsSchema, {
    scenario: {
      case: "variantClassification",
      value: { transcript, hgvsC, clinicalContext },
    },
  });
}

function freeForm(prompt: string): AnalysisInputs {
  return create(AnalysisInputsSchema, {
    scenario: { case: "freeForm", value: { prompt } },
  });
}

const HOUR_MS = 60 * 60 * 1000;

/** In-memory, deterministic data plane — the offline/demo path. Holds the created
 *  analyses and advances each run's reveal one stage per poll. Authorization is
 *  `AuthorizedBackend`'s job; this layer trusts the ids it is handed. */
export class FixtureDataPlane implements AnalysisDataPlane {
  private readonly entries = new Map<string, Entry>();
  private counter = 0;

  constructor() {
    const startup = Date.now();
    for (const seed of SEEDS) {
      const entry = this.mint(
        seed.projectId,
        seed.inputs,
        new Date(startup - seed.agedHours * HOUR_MS),
      );
      if (seed.reveal === "finished") {
        entry.run = { ...entry.run, revealed: SCRIPTED_STAGES };
        entry.revealedDocVersion = FINAL_DOC_VERSION;
      } else if (seed.reveal !== "start") {
        entry.run = { ...entry.run, revealed: seed.reveal.heldAt, held: true };
      }
    }
  }

  /** Mint an analysis, hold it, and return its entry — the one place an id and a
   *  session are assigned, so seeded and created runs cannot drift apart. */
  private mint(
    projectId: string,
    inputs: AnalysisInputs,
    createdAt: Date,
  ): Entry {
    this.counter += 1;
    const analysis = create(AnalysisSchema, {
      id: `an_${this.counter}`,
      sessionId: `sess_${this.counter}`,
      projectId,
      inputs,
      createdAt: timestampFromDate(createdAt),
    });
    const entry: Entry = {
      analysis,
      run: initialRunState(),
      revealedDocVersion: 0,
    };
    this.entries.set(analysis.id, entry);
    return entry;
  }

  private require(analysisId: string): Entry {
    const entry = this.entries.get(analysisId);
    if (!entry) {
      throw new ResourceNotFoundError(`analysis not found: ${analysisId}`);
    }
    return entry;
  }

  async createAnalysis(input: CreateAnalysisInput): Promise<Analysis> {
    return this.mint(input.projectId, input.inputs, new Date()).analysis;
  }

  async listAnalysesIn(projectIds: readonly string[]): Promise<Analysis[]> {
    const scope = new Set(projectIds);
    return [...this.entries.values()]
      .map((entry) => entry.analysis)
      .filter((analysis) => scope.has(analysis.projectId))
      .sort((a, b) => createdMs(b) - createdMs(a));
  }

  async getAnalysis(analysisId: string): Promise<Analysis> {
    return this.require(analysisId).analysis;
  }

  async pollEvents(analysis: Analysis): Promise<PollResponse> {
    const entry = this.require(analysis.id);
    entry.run = afterPoll(entry.run);
    const tick = timelineAt(entry.analysis, entry.run);
    if (tick.documentVersion > entry.revealedDocVersion) {
      entry.revealedDocVersion = tick.documentVersion;
    }
    return create(PollResponseSchema, {
      events: tick.events,
      // Absent, not zero: an unset optional int32 omits from proto3-JSON.
      workingDocumentVersion:
        entry.revealedDocVersion === 0 ? undefined : entry.revealedDocVersion,
    });
  }

  /** A spawned thread's body at the run's current reveal. A read: it advances nothing,
   *  so expanding a card never moves the run it belongs to. */
  async getThread(
    analysis: Analysis,
    threadId: string,
  ): Promise<ThreadResponse> {
    const entry = this.require(analysis.id);
    const events = threadTimeline(entry.analysis, entry.run, threadId);
    if (events === null) {
      throw new ResourceNotFoundError(`thread not found: ${threadId}`);
    }
    return create(ThreadResponseSchema, { events });
  }

  async steerAnalysis(analysis: Analysis, text: string): Promise<void> {
    const entry = this.require(analysis.id);
    if (text.trim() === "") {
      throw new Error("refusing a blank curator turn");
    }
    if (awaitingToolResult(entry.analysis, entry.run)) {
      throw new SessionBusyError(`analysis ${analysis.id} is mid-step`);
    }
    entry.run = steered(entry.run, text);
  }

  async interruptAnalysis(analysis: Analysis): Promise<void> {
    const entry = this.require(analysis.id);
    // A settled run no-ops inside `interrupted`, as the live API treats an idle
    // session; racing a completing step is safe on either backend.
    entry.run = interrupted(entry.run);
  }

  async getDocument(
    analysisId: string,
    version?: number,
  ): Promise<DocumentResponse> {
    const entry = this.require(analysisId);
    if (version === undefined && entry.revealedDocVersion === 0) {
      return create(DocumentResponseSchema, {}); // document unset ⇒ not produced
    }
    const wanted = version ?? entry.revealedDocVersion;
    if (wanted < 1 || wanted > entry.revealedDocVersion) {
      throw new ResourceNotFoundError(`no version ${wanted} for ${analysisId}`);
    }
    return create(DocumentResponseSchema, {
      document: {
        version: wanted,
        markdown: documentMarkdown(entry.analysis, wanted),
      },
    });
  }
}

/** Milliseconds since epoch for an analysis's created_at Timestamp — the list sort
 *  key (newest first). */
function createdMs(analysis: Analysis): number {
  return analysis.createdAt ? timestampDate(analysis.createdAt).getTime() : 0;
}
