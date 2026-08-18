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

// `Partial` while a declared method has no handler: Connect answers those Unimplemented, which is
// what a contract carries before its implementation. The total `ServiceImpl` is what makes a missing
// handler a compile error, so narrow back to it as soon as the set is whole.
export const workbenchService: Partial<ServiceImpl<typeof Workbench>> = {
  async listProjects(_request, ctx) {
    const { backend } = requireUserContext(ctx);
    return { projects: await backend.listProjects() };
  },

  async createAnalysis(request, ctx) {
    const { backend } = requireUserContext(ctx);
    const analysis = await backend.createAnalysis({
      prompt: request.prompt,
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
