import { randomUUID } from "node:crypto";
import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { isManagedSession } from "@/lib/harness";
import {
  type Analysis,
  AnalysisSchema,
  type DocumentResponse,
  DocumentResponseSchema,
  type PollResponse,
  PollResponseSchema,
  type ThreadResponse,
  ThreadResponseSchema,
} from "@/models/workbench";
import { ResourceNotFoundError, UnmanagedSessionError } from "../../errors";
import { kickoffText } from "../../kickoff";
import type { AnalysisDataPlane, CreateAnalysisInput } from "../../ports";
import type { AnthropicClient } from "./client";
import { hashBearer, type KmsSessionTokenDeriver } from "./derive";
import type { Gcs } from "./gcs";
import type { Sql } from "./sql";

// The raw `AnalysisDataPlane` over the self-hosted data plane: Anthropic session control, a
// KMS-derived bearer, Cloud SQL persistence, GCS-direct working documents. Authorization is the
// AuthorizedBackend decorator's job; this layer trusts the (user, project) its caller resolved.

/** Refuse a call that needs the platform's session for a run the platform does not hold. */
function refuseUnmanaged(analysis: Analysis, message: string): void {
  if (!isManagedSession(analysis.sessionId))
    throw new UnmanagedSessionError(message);
}

const NO_THREADS =
  "this run is driven outside the workbench and has no threads here";
const NO_STEER =
  "this run is driven outside the workbench and takes no turns from here";
const NO_INTERRUPT =
  "this run is driven outside the workbench and cannot be interrupted from here";

export class DataPlane implements AnalysisDataPlane {
  constructor(
    private readonly anthropic: AnthropicClient,
    private readonly deriver: KmsSessionTokenDeriver,
    private readonly sql: Sql,
    private readonly gcs: Gcs,
  ) {}

  async createAnalysis(input: CreateAnalysisInput): Promise<Analysis> {
    const analysisId = `an_${randomUUID()}`;
    const sessionId = await this.anthropic.createSession(
      kickoffText(input.inputs),
    );
    const bearer = await this.deriver.deriveBearer(sessionId);
    const createdAt = await this.sql.insertAnalysis({
      id: analysisId,
      sessionId,
      projectId: input.projectId,
      inputs: input.inputs,
      createdBy: input.userEmail,
      tokenHash: hashBearer(bearer),
    });
    return create(AnalysisSchema, {
      id: analysisId,
      sessionId,
      projectId: input.projectId,
      inputs: input.inputs,
      createdAt: timestampFromDate(createdAt),
    });
  }

  async listAnalysesIn(projectIds: readonly string[]): Promise<Analysis[]> {
    return this.sql.listAnalysesIn(projectIds);
  }

  async getAnalysis(analysisId: string): Promise<Analysis> {
    return this.sql.getAnalysis(analysisId);
  }

  async pollEvents(analysis: Analysis): Promise<PollResponse> {
    // The version signal rides on this response, so a run with no session here still has to answer:
    // skipping the read is what leaves the document pane something to fetch.
    const events = isManagedSession(analysis.sessionId)
      ? (await this.anthropic.listEvents(analysis.sessionId)).events
      : [];
    const document = await this.gcs.latestWorkingDocument(analysis.id);
    // The full event list replaces the client's set by id each tick; the event log
    // has no since-cursor, so the whole log is re-projected each poll.
    return create(PollResponseSchema, {
      events,
      // Absent when no document exists yet — proto3-JSON omits an unset optional.
      workingDocumentVersion: document?.version,
    });
  }

  async getThread(
    analysis: Analysis,
    threadId: string,
  ): Promise<ThreadResponse> {
    refuseUnmanaged(analysis, NO_THREADS);
    return create(ThreadResponseSchema, {
      events: await this.anthropic.listThreadEvents(
        analysis.sessionId,
        threadId,
      ),
    });
  }

  async steerAnalysis(analysis: Analysis, text: string): Promise<void> {
    refuseUnmanaged(analysis, NO_STEER);
    await this.anthropic.sendUserMessage(analysis.sessionId, text);
  }

  async interruptAnalysis(analysis: Analysis): Promise<void> {
    refuseUnmanaged(analysis, NO_INTERRUPT);
    await this.anthropic.sendInterrupt(analysis.sessionId);
  }

  async getDocument(
    analysisId: string,
    version?: number,
  ): Promise<DocumentResponse> {
    const document =
      version === undefined
        ? await this.gcs.latestWorkingDocument(analysisId)
        : await this.gcs.workingDocumentVersion(analysisId, version);
    if (document === null) {
      if (version !== undefined) {
        throw new ResourceNotFoundError(
          `no version ${version} for ${analysisId}`,
        );
      }
      return create(DocumentResponseSchema, {}); // document unset ⇒ not produced
    }
    return create(DocumentResponseSchema, {
      document: { version: document.version, markdown: document.markdown },
    });
  }
}
