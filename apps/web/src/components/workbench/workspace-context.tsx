"use client";

import { createContext, type ReactNode, useContext } from "react";
import type { ConversationEvent } from "@/models/workbench";
import type { DocumentVersion, WorkingDocumentState } from "./working-document";
import type { WorkingDocumentSignal } from "./workspace-sync";

// The per-window data a self-fetching content kind needs to render (the conversation event stream and
// the working document). The main window fills it from its live queries, a mirror from the broadcast
// snapshot plus its own read of the browser's copy of the repository. `Pane` reads it and, per active tab, assembles the full
// RenderContext.

export interface WorkspaceData {
  events: ConversationEvent[];
  /** The working document at the commit the window renders: the tip, or the pinned version. */
  workingDocument: WorkingDocumentState;
  /** The workspace branch's tip — never the pinned commit; null while the branch has no commit. */
  documentSignal: WorkingDocumentSignal | null;
  /** The versions the picker lists, newest first; null until the copy has been read. */
  documentVersions: DocumentVersion[] | null;
}

const WorkspaceDataContext = createContext<WorkspaceData | null>(null);

export function WorkspaceDataProvider({
  value,
  children,
}: {
  value: WorkspaceData;
  children: ReactNode;
}): React.ReactElement {
  return (
    <WorkspaceDataContext.Provider value={value}>
      {children}
    </WorkspaceDataContext.Provider>
  );
}

export function useWorkspaceData(): WorkspaceData {
  const data = useContext(WorkspaceDataContext);
  if (data === null) {
    throw new Error("useWorkspaceData used outside a WorkspaceDataProvider");
  }
  return data;
}
