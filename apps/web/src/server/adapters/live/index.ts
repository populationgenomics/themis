import { randomUUID } from "node:crypto";
import { create } from "@bufbuild/protobuf";
import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import {
  type Analysis,
  AnalysisSchema,
  type DocumentResponse,
  DocumentResponseSchema,
  type PollResponse,
  PollResponseSchema,
} from "@/models/workbench";
import { ResourceNotFoundError } from "../../errors";
import type {
  AnalysisDataPlane,
  CreateAnalysisInput,
  ProjectMembership,
} from "../../ports";
import { AnthropicClient } from "./client";
import {
  loadAnthropicConfig,
  loadGcsConfig,
  loadKmsConfig,
  loadSqlConfig,
} from "./config";
import { hashBearer, KmsSessionTokenDeriver } from "./derive";
import { Gcs } from "./gcs";
import { Membership } from "./membership";
import { Sql } from "./sql";

// The `THEMIS_BACKEND=live` composition: the raw `AnalysisDataPlane` over the
// self-hosted data plane — Anthropic session control, KMS-derived bearer, Cloud SQL
// persistence, GCS-direct working documents. Authorization is the AuthorizedBackend
// decorator's job; this layer trusts the (user, project) its caller resolved.

class DataPlane implements AnalysisDataPlane {
  constructor(
    private readonly anthropic: AnthropicClient,
    private readonly deriver: KmsSessionTokenDeriver,
    private readonly sql: Sql,
    private readonly gcs: Gcs,
  ) {}

  async createAnalysis(input: CreateAnalysisInput): Promise<Analysis> {
    const analysisId = `an_${randomUUID()}`;
    const sessionId = await this.anthropic.createSession(input.prompt);
    const bearer = await this.deriver.deriveBearer(sessionId);
    const createdAt = await this.sql.insertAnalysis({
      id: analysisId,
      sessionId,
      projectId: input.projectId,
      prompt: input.prompt,
      createdBy: input.userEmail,
      tokenHash: hashBearer(bearer),
    });
    return create(AnalysisSchema, {
      id: analysisId,
      sessionId,
      projectId: input.projectId,
      prompt: input.prompt,
      createdAt: timestampFromDate(createdAt),
    });
  }

  async listAnalysesIn(projectIds: readonly string[]): Promise<Analysis[]> {
    return this.sql.listAnalysesIn(projectIds);
  }

  async projectOfAnalysis(analysisId: string): Promise<string> {
    return this.sql.projectOfAnalysis(analysisId);
  }

  async pollEvents(analysisId: string): Promise<PollResponse> {
    const row = await this.sql.getAnalysis(analysisId);
    const { events } = await this.anthropic.listEvents(row.sessionId);
    const document = await this.gcs.latestWorkingDocument(analysisId);
    // The full event list replaces the client's set by id each tick; the event log
    // has no since-cursor, so the whole log is re-projected each poll.
    return create(PollResponseSchema, {
      events,
      // Absent when no document exists yet — proto3-JSON omits an unset optional.
      workingDocumentVersion: document?.version,
    });
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

// One SQL pool + Cloud SQL connector, shared by the backend and membership (both
// query the same instance). Memoized on `globalThis` so Next's dev HMR does not leak
// a fresh pool per reload; `context.ts` builds the backend and membership from
// separate factories, so without this each would open its own.
function sharedSql(): Sql {
  const holder = globalThis as typeof globalThis & {
    __themisLiveSql?: Sql;
  };
  if (!holder.__themisLiveSql)
    holder.__themisLiveSql = new Sql(loadSqlConfig());
  return holder.__themisLiveSql;
}

export function createDataPlane(): AnalysisDataPlane {
  return new DataPlane(
    new AnthropicClient(loadAnthropicConfig()),
    new KmsSessionTokenDeriver(loadKmsConfig()),
    sharedSql(),
    new Gcs(loadGcsConfig()),
  );
}

export function createMembership(): ProjectMembership {
  return new Membership(sharedSql());
}

export { createIdentity } from "./identity";
