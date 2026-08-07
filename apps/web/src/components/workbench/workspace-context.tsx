"use client";

import { createContext, type ReactNode, useContext } from "react";
import type { ConversationEvent, WorkingDocument } from "@/models/workbench";

// The per-window data a self-fetching content kind needs to render (the conversation event stream and
// the working document). The main window fills it from its live queries. `Pane` reads it and, per
// active tab, assembles the full RenderContext.

export interface WorkspaceData {
  events: ConversationEvent[];
  workingDocument: WorkingDocument | null;
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
