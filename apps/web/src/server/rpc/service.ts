import type { ServiceImpl } from "@connectrpc/connect";
import type { Workbench } from "@/models/workbench";
import { requireUserContext } from "./context";

// The Workbench service implementation (schema/proto/themis/workbench/rpc/workbench.proto).
// Every method reads its caller through `requireUserContext` and reaches the data plane
// only through that caller's `AuthorizedBackend`, so each access is membership-scoped by
// construction. The methods do no reshaping: the backend already returns view-model
// messages, and Connect serializes them.

export const workbenchService: ServiceImpl<typeof Workbench> = {
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
};
