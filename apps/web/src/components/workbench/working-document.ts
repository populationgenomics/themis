"use client";

import { useMemo, useState } from "react";
import {
  type DocumentRead,
  useWorkspaceDocument,
  useWorkspaceHistory,
} from "@/lib/queries";
import type { PollResponse, WorkspaceTip } from "@/models/workbench";
import type { RecordedTip } from "@/workspace-copy/copy";
import { WORKSPACE_DAMAGED } from "@/workspace-copy/protocol";
import type {
  DocumentFetchKey,
  ReadTip,
  WorkingDocumentSignal,
} from "./workspace-sync";

// The working document as a window renders it, read from the browser's copy of the workspace
// repository: the main window keys the read on the Poll's tip, a mirror on the broadcast one.

/** What the working-document tab shows. `noRepository`: the Poll reports no branch tip, so the
 *  Analysis has no workspace repository yet. `unavailable`: no Poll has been able to read the
 *  branch's tip, so there is nothing to show yet. `absent`: the tip has no working document yet.
 *  `failed`: the Poll never answered. `copyFailed`: the browser's copy could not be read, which
 *  clearing it may fix. `tooLarge`: the workspace is over the copy's ceiling. `damaged`: the repository is damaged: the
 *  Poll found its ref document does not parse, or the copy cannot be brought to the tip. */
export type WorkingDocumentState =
  | { kind: "loading" }
  | { kind: "noRepository" }
  | { kind: "unavailable" }
  | { kind: "absent" }
  | { kind: "tooLarge" }
  | { kind: "damaged" }
  | { kind: "failed" }
  | { kind: "copyFailed" }
  /** `commit` is the one `markdown` was read at, which is the pinned or tip commit once it has
   *  loaded and the previous version's until then; `pinned`, whether it was read as the version
   *  the tab pins, which the view shows only to read, rather than as the tip's. */
  | { kind: "shown"; markdown: string; commit: string; pinned: boolean };

/** One row of the version picker: a tip the reflog recorded, numbered from the oldest. */
export interface DocumentVersion {
  commit: string;
  number: number;
  /** When the publish that made it the tip was recorded, in seconds. */
  timestamp: number;
}

/** The picker's versions from the recorded tips, newest first as the copy lists them. */
export function documentVersions(
  tips: readonly RecordedTip[],
): DocumentVersion[] {
  return tips.map((tip, index) => ({
    commit: tip.commit,
    number: tips.length - index,
    timestamp: tip.timestamp,
  }));
}

/** What one Poll tick found: a tip, a ref document it could not read, or one that does not parse. */
export type PolledTip = ReadTip | { kind: "unavailable" } | { kind: "damaged" };

/** What one Poll tick found. Raises on a tip with no state, which the Poll's contract rules out. */
export function polledTip(tip: WorkspaceTip | undefined): PolledTip {
  switch (tip?.state.case) {
    case "commit":
      return { kind: "commit", commit: tip.state.value };
    case "noCommit":
      return { kind: "noCommit" };
    case "unavailable":
      return { kind: "unavailable" };
    case "damaged":
      return { kind: "damaged" };
    case undefined:
      throw new Error("a Poll answered with no workspace tip");
  }
}

/** The tip a window follows after a tick that found `polled`, when it followed `last` before: the
 *  tip the tick read, or `last` on a tick that read none. `last` is returned as it is while the
 *  tick agrees with it, so the tip keeps its identity across ticks. */
export function followTip(
  last: ReadTip | null,
  polled: PolledTip,
): ReadTip | null {
  if (polled.kind === "unavailable" || polled.kind === "damaged") return last;
  if (last !== null && sameTip(last, polled)) return last;
  return polled;
}

function sameTip(a: ReadTip, b: ReadTip): boolean {
  return a.kind === "commit"
    ? b.kind === "commit" && a.commit === b.commit
    : b.kind === a.kind;
}

/** The tip a window follows, and the Analysis whose Poll read it. */
export interface Followed {
  analysisId: string;
  tip: ReadTip;
}

/** What a window follows for `analysisId` after the Poll answered `polled` (undefined until it
 *  answers), when it followed `followed` before. A tip followed for another Analysis is dropped,
 *  never carried over; `followed` is returned as it is while nothing changes, so the tip keeps its
 *  identity and the document is not read again. */
export function nextFollowed(
  followed: Followed | null,
  analysisId: string,
  polled: PolledTip | undefined,
): Followed | null {
  const last = followed?.analysisId === analysisId ? followed : null;
  if (polled === undefined) return last;
  const tip = followTip(last === null ? null : last.tip, polled);
  if (tip === null) return null;
  return last !== null && tip === last.tip ? last : { analysisId, tip };
}

/** The working-document signal from the main window's Poll for `analysisId`: null until the Poll
 *  answers, and across ticks that read no tip, the tip the last one that read one found. */
export function useWorkingDocumentSignal(
  analysisId: string,
  poll: {
    data: PollResponse | undefined;
    isLoadingError: boolean;
    isRefetchError: boolean;
  },
): WorkingDocumentSignal | null {
  const [followed, setFollowed] = useState<Followed | null>(null);
  const polled =
    poll.data === undefined ? undefined : polledTip(poll.data.workspaceTip);
  const next = nextFollowed(followed, analysisId, polled);
  // Adjusted during render, React's way to keep what an earlier render saw: the render restarts
  // at once with it.
  if (next !== followed) setFollowed(next);
  const tip = next === null ? null : next.tip;
  const kind = polled?.kind;
  const { isLoadingError, isRefetchError } = poll;
  return useMemo(
    () => signalFrom(analysisId, tip, kind, { isLoadingError, isRefetchError }),
    [analysisId, tip, kind, isLoadingError, isRefetchError],
  );
}

/** The signal for `analysisId` at the followed `tip`, after the latest answered tick found
 *  `polled` (undefined until the Poll answers): null until then, unless the Poll's first answer
 *  failed. A Poll that fails after answering keeps its last answer, which may be out of date, so
 *  its failure reads as unavailable. */
export function signalFrom(
  analysisId: string,
  tip: ReadTip | null,
  polled: PolledTip["kind"] | undefined,
  poll: { isLoadingError: boolean; isRefetchError: boolean },
): WorkingDocumentSignal | null {
  if (poll.isLoadingError) {
    return {
      analysisId,
      tip: null,
      pollFailed: true,
      unavailable: false,
      damaged: false,
    };
  }
  if (polled === undefined) return null;
  return {
    analysisId,
    tip,
    pollFailed: false,
    unavailable: polled === "unavailable" || poll.isRefetchError,
    damaged: polled === "damaged",
  };
}

/** The working document a window renders for `signal` (null until the Poll answers) and the
 *  commit `pinned` in the working-doc tab, and the versions the picker lists (null until read). */
export function useWorkingDocument(
  signal: WorkingDocumentSignal | null,
  pinned: string | null,
): {
  document: WorkingDocumentState;
  versions: DocumentVersion[] | null;
} {
  const key: DocumentFetchKey | null =
    signal?.tip?.kind === "commit"
      ? {
          analysisId: signal.analysisId,
          tip: signal.tip.commit,
          commit: pinned ?? signal.tip.commit,
          pinned: pinned !== null,
        }
      : null;
  const document = useWorkspaceDocument(key);
  const history = useWorkspaceHistory(
    key === null ? null : { analysisId: key.analysisId, tip: key.tip },
  );
  return {
    document: documentState(signal, document),
    versions:
      history.data === undefined ? null : documentVersions(history.data),
  };
}

/** The state a window shows, from the Poll's signal and the document read at it. No tip is no
 *  repository yet: the worker's seed commit creates the branch in an Analysis's first session, so a
 *  repository without a branch commit does not last. */
export function documentState(
  signal: WorkingDocumentSignal | null,
  document: {
    isError: boolean;
    error: Error | null;
    data: DocumentRead | undefined;
  },
): WorkingDocumentState {
  if (signal === null) return { kind: "loading" };
  if (signal.pollFailed) return { kind: "failed" };
  if (signal.damaged) return { kind: "damaged" };
  if (signal.tip === null) {
    if (!signal.unavailable) {
      throw new Error(
        "a working-document signal with no tip names no unavailable workspace",
      );
    }
    return { kind: "unavailable" };
  }
  if (signal.tip.kind === "noCommit") return { kind: "noRepository" };
  if (document.isError) {
    switch (document.error?.name) {
      case "CopyTooLargeError":
        return { kind: "tooLarge" };
      case WORKSPACE_DAMAGED:
        return { kind: "damaged" };
      default:
        return { kind: "copyFailed" };
    }
  }
  if (document.data === undefined) return { kind: "loading" };
  if (document.data.value === null) return { kind: "absent" };
  return {
    kind: "shown",
    markdown: document.data.value,
    commit: document.data.commit,
    pinned: document.data.pinned,
  };
}
