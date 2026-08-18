import type { ServiceImpl } from "@connectrpc/connect";
import { Representation } from "@/models/literature";
import type { Workbench } from "@/models/workbench";
import { literaturePort } from "@/server/context";
import { ClientInputError } from "@/server/errors";
import { requireUserContext } from "./context";

// The Workbench service implementation (schema/proto/themis/workbench/rpc/workbench.proto).
// The analysis methods read their caller through `requireUserContext` and reach the data
// plane only through that caller's `AuthorizedBackend`, so each access is membership-scoped by
// construction. The paper methods (`describePaper`/`locate`) read the shared corpus through the
// literature port instead — IAP-only, not Project-scoped — but still call `requireUserContext` as the
// fail-loud backstop every method shares: it raises if the identity interceptor did not run, so an
// unwired chokepoint can never serve them either. Browser-controlled request fields are validated
// here (a blank doc_id, an unspecified representation) so a client slip surfaces as InvalidArgument
// rather than a masked Internal. The methods do no reshaping — the backend and the port already
// return view-model messages, and Connect serializes them.
//
// `listProjects` and `listAnalyses` stay routed with no browser caller: the pages read them through
// `AuthorizedBackend` server-side, but this seam is deliberately consumable by code we do not control
// (proto.md §"Bucket 2"), and a read is what such a caller reaches for first. `handler.test.ts` is
// therefore the only thing exercising their authorization — treat it as load-bearing rather than
// incidental. `Analysis.session_id` rides along on `listAnalyses`; it is not a credential, since a
// session's bearer is a KMS MAC over it and the key material never leaves KMS.

export const workbenchService: ServiceImpl<typeof Workbench> = {
  async listProjects(_request, ctx) {
    const { backend } = requireUserContext(ctx);
    return { projects: await backend.listProjects() };
  },

  async createAnalysis(request, ctx) {
    const { backend } = requireUserContext(ctx);
    // `inputs` is validated present by the boundary interceptor; the backend renders the agent's
    // kickoff text from it.
    if (!request.inputs) throw new Error("create request carries no inputs");
    const analysis = await backend.createAnalysis({
      inputs: request.inputs,
      projectId: request.projectId,
    });
    return { id: analysis.id };
  },

  async listAnalyses(request, ctx) {
    const { backend } = requireUserContext(ctx);
    return { analyses: await backend.listAnalyses(request.projectId) };
  },

  async poll(request, ctx) {
    const { backend } = requireUserContext(ctx);
    return backend.pollEvents(request.analysisId);
  },

  async getThread(request, ctx) {
    const { backend } = requireUserContext(ctx);
    return backend.getThread(request.analysisId, request.threadId);
  },

  async steer(request, ctx) {
    const { backend } = requireUserContext(ctx);
    // `text` is non-blank and bounded by the boundary interceptor, `analysis_id` non-empty.
    await backend.steerAnalysis(request.analysisId, request.text);
    return {};
  },

  async interrupt(request, ctx) {
    const { backend } = requireUserContext(ctx);
    await backend.interruptAnalysis(request.analysisId);
    return {};
  },

  async getDocument(request, ctx) {
    const { backend } = requireUserContext(ctx);
    return backend.getDocument(request.analysisId, request.version);
  },

  async describePaper(request, ctx) {
    requireUserContext(ctx);
    if (request.docId === "") throw new ClientInputError("doc_id is required");
    return literaturePort().describePaper(request.docId);
  },

  async locate(request, ctx) {
    requireUserContext(ctx);
    if (request.docId === "") throw new ClientInputError("doc_id is required");
    if (request.representation === Representation.UNSPECIFIED) {
      throw new ClientInputError("representation must be specified");
    }
    return literaturePort().locate(
      request.docId,
      request.quote,
      request.representation,
    );
  },
};
