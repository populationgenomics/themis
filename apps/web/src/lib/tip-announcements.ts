"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

// A window that moved an Analysis's workspace tip — a curator's widget edit — tells every window of
// the workbench, so each asks the Poll again at once rather than on its next tick. Only a main window
// polls; a mirror follows the tip main broadcasts, so an edit made in a mirror reaches it through
// main's refreshed Poll.

const CHANNEL = "themis-workspace-tip";

interface TipMoved {
  analysisId: string;
}

/** Ask the Poll of `analysisId` again, in this window and in every other window of the workbench. */
export function announceTipMoved(
  queryClient: ReturnType<typeof useQueryClient>,
  analysisId: string,
): void {
  void queryClient.invalidateQueries({ queryKey: ["poll", analysisId] });
  const channel = new BroadcastChannel(CHANNEL);
  channel.postMessage({ analysisId } satisfies TipMoved);
  channel.close();
}

/** In a window that polls `analysisId`, ask the Poll again whenever another window announces that its
 *  tip moved. */
export function useTipAnnouncements(analysisId: string): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    const channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = (event: MessageEvent<unknown>) => {
      const data = event.data as Partial<TipMoved> | null;
      if (data?.analysisId === analysisId) {
        void queryClient.invalidateQueries({ queryKey: ["poll", analysisId] });
      }
    };
    return () => channel.close();
  }, [analysisId, queryClient]);
}
