import { openViaRegistry, REGISTRY } from "./content-kinds";
import type { Citation } from "./markdown";
import type { WorkspaceModelController } from "./use-workspace-model";
import type { Source } from "./workspace-model";

// A citation click reveals its paper beside the source. The reducer's `computeTarget` places it —
// splitting the source pane for a document citation, tabbing it into the tab area for a conversation
// one — and surfaces, never duplicates, an already-open paper. The highlight is set only after the open
// resolves, so a failed fetch leaves no orphan quote (nothing would render it, and no tab-close would
// ever clear it). The reveal is authoritative over the highlight, not additive: a bare `:paper` reveal
// sets the empty highlight (`""`, the clear signal both views honour), so surfacing a paper never
// leaves an earlier `:quote`'s highlight or its warning chip standing. A malformed id never reaches
// here (rendered as a broken citation upstream); a rejected open is swallowed. See
// docs/design/document-pane.md §Reveal.

export async function revealCitation(
  controller: Pick<WorkspaceModelController, "openTab" | "setHighlight">,
  src: Source,
  citation: Citation,
  opts?: { forceLocal?: boolean },
): Promise<void> {
  const paperId = REGISTRY.paper.id({ docId: citation.docId });
  try {
    await openViaRegistry(
      controller,
      { kind: "paper", args: { docId: citation.docId }, src },
      opts,
    );
  } catch {
    return;
  }
  controller.setHighlight(
    paperId,
    citation.kind === "quote" ? citation.quote : "",
  );
}
