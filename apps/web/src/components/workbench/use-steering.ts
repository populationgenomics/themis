"use client";

import { useRef, useState } from "react";
import { useInterrupt, useSteer } from "@/lib/queries";
import { isAgentBusy } from "@/lib/rpc";
import type { ConversationEvent } from "@/models/workbench";
import {
  accepted,
  dropped,
  type PendingTurn,
  reconcile,
  taken,
} from "./pending-turns";

// The steering half of the conversation region: the draft, the mutation, and the turns
// shown ahead of the run. It lives in the Analysis island beside `usePoll`, which is
// also what keeps a half-written turn alive across a layout change that remounts the
// composer.
//
// Pending turns are deliberately not merged into the events the workspace provider
// carries: that context feeds panes, including popped-out mirror windows, which hold
// no poll and so could never retire one.

/** A failed act, labelled with which one failed so the composer never attributes a
 *  failed stop to the turn it did not send. */
export interface SteeringFailure {
  act: "send" | "stop";
  cause: unknown;
}

export interface Steering {
  /** Turns shown ahead of the run, oldest first — rendered after every settled event. */
  pending: readonly PendingTurn[];
  draft: string;
  setDraft: (text: string) => void;
  /** False until the first poll resolves, when a turn's place in the run is knowable. */
  ready: boolean;
  send: () => void;
  /** Halt the run's current step. Safe against an idle run (a no-op there), so the
   *  control needs no busy-gating of its own. */
  stop: () => void;
  error: SteeringFailure | null;
}

export function useSteering(
  analysisId: string,
  events: ConversationEvent[] | undefined,
): Steering {
  const steer = useSteer(analysisId);
  const interrupt = useInterrupt(analysisId);
  const [pending, setPending] = useState<PendingTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<SteeringFailure | null>(null);
  const localIds = useRef(0);
  // The poll snapshot the busy refusal was set against — every poll decodes a fresh
  // array, so a different identity marks a snapshot newer than the refusal. The
  // mirror tracks the newest snapshot for the rejection callback, whose closure
  // holds the render it was created in.
  const refusedAgainst = useRef<ConversationEvent[] | undefined>(undefined);
  const latestEvents = useRef(events);
  latestEvents.current = events;

  const carried = events ?? [];

  // Retired during render, not in an effect: an effect leaves one painted frame in
  // which a retired turn sits beside the settled twin that retired it.
  const visible = reconcile(pending, carried);

  // The busy refusal asserts a live condition, so it retires with the condition: any
  // snapshot newer than the refusal that shows no call in flight clears it. Newer is
  // load-bearing — the refusal can outrun the poll that first shows the in-flight
  // call, and clearing against the stale snapshot would swallow the alert in the
  // very render that set it. Cleared during render for the reason `reconcile` runs
  // there.
  if (
    error !== null &&
    isAgentBusy(error.cause) &&
    events !== undefined &&
    events !== refusedAgainst.current &&
    !events.some(
      (event) =>
        event.kind.case === "tool" && event.kind.value.result === undefined,
    )
  ) {
    setError(null);
  }

  const send = () => {
    const text = draft.trim();
    if (text === "" || events === undefined) return;
    localIds.current += 1;
    const localId = `pending-${localIds.current}`;
    setDraft("");
    setError(null);
    setPending((current) => taken(current, events, text, localId));
    // `mutateAsync`, not `mutate`: the shared observer drops a superseded call's
    // per-invocation callbacks, so a first turn failing while a second is in flight
    // would strand its bubble and lose its prose.
    steer.mutateAsync(text).then(
      () => setPending((current) => accepted(current, localId)),
      (failure: unknown) => {
        setPending((current) => dropped(current, localId));
        refusedAgainst.current = latestEvents.current;
        setError({ act: "send", cause: failure });
        // Prepended, so a turn typed in the meantime survives alongside it.
        setDraft((current) =>
          current === "" ? text : `${text}\n\n${current}`,
        );
      },
    );
  };

  const stop = () => {
    // Clears a shown busy refusal too: the halt is the act that refusal asked for.
    setError(null);
    // `mutateAsync` for `send`'s reason: a per-invocation callback survives a
    // concurrent call on the shared observer.
    interrupt
      .mutateAsync()
      .catch((failure: unknown) => setError({ act: "stop", cause: failure }));
  };

  return {
    pending: visible,
    draft,
    setDraft,
    ready: events !== undefined,
    send,
    stop,
    error,
  };
}
