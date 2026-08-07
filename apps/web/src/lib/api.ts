import { workbench } from "@/lib/rpc";
import type {
  LocateResponse,
  PaperInfo,
  Representation,
} from "@/models/literature";

// The paper read seam. `describePaper`/`locate` are structured proto messages over the Workbench
// Connect client (`@/lib/rpc`) — validated, one Connect error model, IAP-gated, the same seam as the
// analysis methods. The content objects (markdown, pdf, files) are raw bytes the pane fetches from
// their own routes (which 302 to a signed GCS URL on the live adapter, serve seeded bytes offline);
// only their URLs live here.

const enc = encodeURIComponent;

export const api = {
  getPaper: (id: string): Promise<PaperInfo> =>
    workbench.describePaper({ docId: id }),

  locate: (
    id: string,
    quote: string,
    representation: Representation,
  ): Promise<LocateResponse> =>
    workbench.locate({ docId: id, quote, representation }),

  getPaperMarkdownText: async (id: string): Promise<string> => {
    const response = await fetch(paperContent.markdown(id));
    if (!response.ok) {
      throw new Error(`paper markdown ${id}: HTTP ${response.status}`);
    }
    return response.text();
  },
};

/** URLs for a paper's raw content objects — the pane fetches markdown as text, points `react-pdf`
 *  at the PDF, and resolves inline figures / supplementary downloads to the files route. */
export const paperContent = {
  markdown: (id: string): string => `/api/papers/${enc(id)}/markdown`,
  pdf: (id: string): string => `/api/papers/${enc(id)}/pdf`,
  file: (id: string, name: string): string =>
    `/api/papers/${enc(id)}/files/${enc(name)}`,
};
