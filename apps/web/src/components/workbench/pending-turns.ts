import type { ConversationEvent } from "@/models/workbench";

// A curator turn the browser shows ahead of the run: sent, and not yet carried by a
// poll. The server mints the event id and the poll replaces the whole list by id, so a
// pending turn has no id to match on — it is matched by its text against how many
// identical turns the run already carried when it was sent.

export interface PendingTurn {
  /** Client-only: the React key, and how a failed send finds its own entry. */
  localId: string;
  text: string;
  /** Settled turns with this text at the moment it was sent. */
  baseline: number;
  /** `sending` until the RPC returns; `sent` until the poll carries the turn. */
  status: "sending" | "sent";
}

/** Settled curator turns carrying exactly this text. The projected `user` stream is
 *  closed — the kickoff and the curator's own turns, nothing else folds to a user
 *  narration — so identical text is an identical turn. */
function settledCount(
  events: readonly ConversationEvent[],
  text: string,
): number {
  return events.filter(
    (event) => event.kind.case === "user" && event.kind.value.text === text,
  ).length;
}

/** Record a turn as pending, against the run as it stands. */
export function enqueue(
  pending: readonly PendingTurn[],
  events: readonly ConversationEvent[],
  text: string,
  localId: string,
): PendingTurn[] {
  return [
    ...pending,
    { localId, text, baseline: settledCount(events, text), status: "sending" },
  ];
}

/** Mark a turn the RPC accepted. It stays shown — the poll is what settles it. */
export function accepted(
  pending: readonly PendingTurn[],
  localId: string,
): PendingTurn[] {
  return pending.map((turn) =>
    turn.localId === localId ? { ...turn, status: "sent" } : turn,
  );
}

/** Drop a turn whose send failed. Its prose goes back to the composer. */
export function dropped(
  pending: readonly PendingTurn[],
  localId: string,
): PendingTurn[] {
  return pending.filter((turn) => turn.localId !== localId);
}

/** The pending list after a poll tick: every turn the run has caught up with is
 *  retired, oldest first.
 *
 *  Turns sharing a text are settled as a group, against the surplus the run carries
 *  over the *oldest* surviving one's baseline. The group anchor is what keeps the
 *  count sound: a per-turn baseline already includes the events its predecessors are
 *  waiting on, so charging each turn its own would count one server event twice as
 *  soon as a predecessor left the list by failing rather than by settling — and the
 *  turn behind it would never retire. Baselines only rise, so the oldest carries the
 *  minimum and the surplus is the number of turns the run has genuinely taken. */
export function reconcile(
  pending: readonly PendingTurn[],
  events: readonly ConversationEvent[],
): PendingTurn[] {
  const surplus = new Map<string, number>();
  for (const turn of pending) {
    if (!surplus.has(turn.text)) {
      surplus.set(turn.text, settledCount(events, turn.text) - turn.baseline);
    }
  }
  return pending.filter((turn) => {
    const left = surplus.get(turn.text) ?? 0;
    if (left <= 0) return true;
    surplus.set(turn.text, left - 1);
    return false;
  });
}

/** Take a turn: retire what the run has caught up with, then record the new one. The
 *  two are one step — a baseline read from a list still holding turns the run has
 *  already carried counts them twice. */
export function taken(
  pending: readonly PendingTurn[],
  events: readonly ConversationEvent[],
  text: string,
  localId: string,
): PendingTurn[] {
  return enqueue(reconcile(pending, events), events, text, localId);
}
