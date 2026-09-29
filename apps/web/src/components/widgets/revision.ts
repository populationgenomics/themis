import type { Citation } from "@/components/workbench/citation";

// What a surface hands the markdown renderer to turn widgets on: the revision every `::embed` in the
// document resolves against (docs/design/document-widgets.md, "Recognition is shared; rendering is
// opted into"). A surface without one renders the directive as literal text.

/** A revision of an Analysis's workspace repository, read from the browser's copy. An `::embed`'s path
 *  resolves against the tree's root, where the working document is. */
export interface WidgetRevision {
  analysisId: string;
  /** The branch tip the copy is brought to before a read. */
  tip: string;
  /** The commit the document, and every asset it names, is read at. */
  commit: string;
  /** The email the BFF verified for this page, when the viewer may change what the revision's
   *  widgets show; null for a revision that is only read, such as an earlier version. */
  curatorEmail: string | null;
  /** Whether the document was read as the version the tab pins, not the tip's: a curator's tick
   *  shows over the tip's view alone. */
  pinned: boolean;
}

/** What a widget component receives beside its payload. */
export interface WidgetContext {
  /** The asset's path in the revision's tree. */
  path: string;
  /** The commit the payload was read at: what an edit is built on. */
  drawnAt: string;
  revision: WidgetRevision;
  /** Whether `drawnAt` is the revision's own commit rather than an earlier one still on screen
   *  while the revision's asset loads. */
  current: boolean;
  /** Reveal a citation in the document pane, as `:paper` and `:quote` in prose do. */
  onCitation: (citation: Citation) => void;
}
