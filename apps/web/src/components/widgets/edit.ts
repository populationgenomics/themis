"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { announceTipMoved } from "@/lib/tip-announcements";
import {
  CopyRequestError,
  PublishUnconfirmedError,
  workspaceCopy,
} from "@/workspace-copy/client";
import {
  type EditFile,
  PUBLISH_DAMAGED_AFTER_UNKNOWN,
  PUBLISH_OUTCOME_UNKNOWN,
  WORKSPACE_DAMAGED,
} from "@/workspace-copy/protocol";
import type { EditOutcome } from "@/workspace-copy/publish";
import type { WidgetContext } from "./revision";

// A widget's edit, published through the browser's copy as the curator this page verified: the new
// bytes of the files it replaces, made from the files at the commit the widget was drawn from.

/** What a widget's change writes: the commit it was made from, the commit message, and each file's
 *  new bytes. */
export interface WidgetEdit {
  base: string;
  message: string;
  files: readonly EditFile[];
}

/** Whether a publish failed with its outcome unknown: its worker was lost with the publish in flight,
 *  or the worker's last publish went unanswered and the document did not show it landed. Sending it
 *  again could apply it twice, so the widget draws the tip and asks the curator to redo the change if
 *  it is missing. */
export function outcomeUnknown(error: unknown): boolean {
  return (
    error instanceof PublishUnconfirmedError ||
    (error instanceof CopyRequestError &&
      error.name === PUBLISH_OUTCOME_UNKNOWN)
  );
}

/** How the repository's damage ended a publish: `damaged`, the edit was not saved;
 *  `damagedAfterUnknown`, a send of it went unanswered before the damage was found, so it may have
 *  been. Undefined for any other failure. Nothing is sent again after either. */
export function workspaceDamage(
  error: unknown,
): "damaged" | "damagedAfterUnknown" | undefined {
  if (!(error instanceof CopyRequestError)) return undefined;
  if (error.name === WORKSPACE_DAMAGED) return "damaged";
  if (error.name === PUBLISH_DAMAGED_AFTER_UNKNOWN)
    return "damagedAfterUnknown";
  return undefined;
}

/** Publish `edit` on the widget's revision; null when the widget is not editable, because the
 *  revision is only read or what is on screen is not yet the revision's own asset. Unless the
 *  publish was refused, every window asks the Poll again, so a tip the edit moved, or one the edit
 *  found changed, and the widget with it, moves to the new commit. */
export function useWidgetEdit(
  context: WidgetContext,
): ((edit: WidgetEdit) => Promise<EditOutcome>) | null {
  const queryClient = useQueryClient();
  const { analysisId, curatorEmail } = context.revision;
  const { current } = context;
  const publish = useCallback(
    async ({ base, message, files }: WidgetEdit) => {
      if (curatorEmail === null) {
        throw new Error("this revision is not one a curator edits");
      }
      const refetchTip = () => announceTipMoved(queryClient, analysisId);
      try {
        const outcome = await workspaceCopy.publish({
          analysisId,
          base,
          curatorEmail,
          message,
          files,
        });
        refetchTip();
        return outcome;
      } catch (error) {
        if (
          outcomeUnknown(error) ||
          workspaceDamage(error) === "damagedAfterUnknown"
        )
          refetchTip();
        throw error;
      }
    },
    [analysisId, curatorEmail, queryClient],
  );
  return curatorEmail !== null && current ? publish : null;
}
