import { create } from "@bufbuild/protobuf";
import { timestampDate, timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  type Analysis,
  AnalysisSchema,
  type DocumentResponse,
  DocumentResponseSchema,
  type PollResponse,
  PollResponseSchema,
} from "@/models/workbench";
import { ResourceNotFoundError } from "../../errors";
import type { AnalysisDataPlane, CreateAnalysisInput } from "../../ports";
import { documentMarkdown, stageCount, timelineAt } from "./timeline";

interface Entry {
  analysis: Analysis;
  // The reveal is server-side state: each poll releases one more timeline stage.
  revealedStages: number;
  // Monotonic: the highest working-document version the poll reveal has released.
  // 0 until the run reaches its final stage; read by `getDocument` so the document
  // pane reflects the reveal.
  revealedDocVersion: number;
}

/** In-memory, deterministic data plane — the offline/demo path. Holds the created
 *  analyses and advances each run's reveal one stage per poll. Authorization is
 *  `AuthorizedBackend`'s job; this layer trusts the ids it is handed. */
export class FixtureDataPlane implements AnalysisDataPlane {
  private readonly entries = new Map<string, Entry>();
  private counter = 0;

  private require(analysisId: string): Entry {
    const entry = this.entries.get(analysisId);
    if (!entry) {
      throw new ResourceNotFoundError(`analysis not found: ${analysisId}`);
    }
    return entry;
  }

  async createAnalysis(input: CreateAnalysisInput): Promise<Analysis> {
    this.counter += 1;
    const analysis = create(AnalysisSchema, {
      id: `an_${this.counter}`,
      sessionId: `sess_${this.counter}`,
      projectId: input.projectId,
      prompt: input.prompt,
      createdAt: timestampFromDate(new Date()),
    });
    this.entries.set(analysis.id, {
      analysis,
      revealedStages: 0,
      revealedDocVersion: 0,
    });
    return analysis;
  }

  async listAnalysesIn(projectIds: readonly string[]): Promise<Analysis[]> {
    const scope = new Set(projectIds);
    return [...this.entries.values()]
      .map((entry) => entry.analysis)
      .filter((analysis) => scope.has(analysis.projectId))
      .sort((a, b) => createdMs(b) - createdMs(a));
  }

  async projectOfAnalysis(analysisId: string): Promise<string> {
    return this.require(analysisId).analysis.projectId;
  }

  async pollEvents(analysisId: string): Promise<PollResponse> {
    const entry = this.require(analysisId);
    entry.revealedStages = Math.min(entry.revealedStages + 1, stageCount());
    const tick = timelineAt(entry.analysis, entry.revealedStages);
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

  async getDocument(
    analysisId: string,
    version?: number,
  ): Promise<DocumentResponse> {
    const entry = this.require(analysisId);
    if (entry.revealedDocVersion === 0) {
      return create(DocumentResponseSchema, {}); // document unset ⇒ not produced
    }
    if (version !== undefined && version !== entry.revealedDocVersion) {
      throw new ResourceNotFoundError(
        `no version ${version} for ${analysisId}`,
      );
    }
    return create(DocumentResponseSchema, {
      document: {
        version: entry.revealedDocVersion,
        markdown: documentMarkdown(entry.analysis, entry.revealedDocVersion),
      },
    });
  }
}

/** Milliseconds since epoch for an analysis's created_at Timestamp — the list sort
 *  key (newest first). */
function createdMs(analysis: Analysis): number {
  return analysis.createdAt ? timestampDate(analysis.createdAt).getTime() : 0;
}
