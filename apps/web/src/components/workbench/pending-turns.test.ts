import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  type ConversationEvent,
  ConversationEventSchema,
} from "@/models/workbench";
import {
  dropped,
  enqueue,
  type PendingTurn,
  reconcile,
  taken,
} from "./pending-turns";

// The poll replaces the whole event list by id and the server mints the ids, so a
// locally-echoed turn is matched by text against a baseline count. These pin the cases
// where a naive "does this text appear" would retire the wrong turn.

function event(
  kind: "user" | "assistant",
  id: string,
  text: string,
): ConversationEvent {
  return create(ConversationEventSchema, {
    id,
    kind: { case: kind, value: { text } },
  });
}

function toolEvent(id: string, text: string): ConversationEvent {
  return create(ConversationEventSchema, {
    id,
    kind: {
      case: "tool",
      value: { name: "shell", intent: text, command: text },
    },
  });
}

/** Send `texts` in order against `events`, as the composer does. */
function sendAll(
  events: readonly ConversationEvent[],
  ...texts: string[]
): PendingTurn[] {
  return texts.reduce<PendingTurn[]>(
    (pending, text, index) => enqueue(pending, events, text, `p${index}`),
    [],
  );
}

describe("pending curator turns", () => {
  test("a turn retires when the poll carries it", () => {
    const pending = sendAll([], "Most");
    expect(reconcile(pending, [])).toHaveLength(1);
    expect(reconcile(pending, [event("user", "ev1", "Most")])).toEqual([]);
  });

  test("a turn identical to one the run already carried does not retire against it", () => {
    // The kickoff is a user narration too, so "has this text appeared" would retire the
    // echo the instant it was sent.
    const settled = [event("user", "ev-kickoff", "Most")];
    const pending = sendAll(settled, "Most");
    expect(reconcile(pending, settled)).toHaveLength(1);
    expect(
      reconcile(pending, [...settled, event("user", "ev1", "Most")]),
    ).toEqual([]);
  });

  test("two identical turns retire one per matching event, in the order sent", () => {
    const pending = sendAll([], "Most", "Most");
    const one = reconcile(pending, [event("user", "ev1", "Most")]);
    expect(one.map((turn) => turn.localId)).toEqual(["p1"]);
    expect(
      reconcile(pending, [
        event("user", "ev1", "Most"),
        event("user", "ev2", "Most"),
      ]),
    ).toEqual([]);
  });

  test("turns with different text retire independently", () => {
    const pending = sendAll([], "Most", "Few");
    const left = reconcile(pending, [event("user", "ev1", "Few")]);
    expect(left.map((turn) => turn.text)).toEqual(["Most"]);
  });

  test("a dropped turn does not strand the identical one behind it", () => {
    // Two "Most" in flight, the first fails. The run will only ever carry one, so a
    // baseline that had counted the dropped turn would leave the survivor pending
    // forever.
    const pending = sendAll([], "Most", "Most");
    const survivor = dropped(pending, "p0");
    expect(reconcile(survivor, [event("user", "ev1", "Most")])).toEqual([]);
  });

  test("a send that fails after a later one settled does not strand the turns behind it", () => {
    // The sequence the group anchor exists for: an earlier duplicate is still in flight
    // (a retried upstream call) while a later one is sent, polled back, and pruned out
    // of the stored list by the next send. Charging each turn its own baseline would
    // count that one server event twice and leave the newest bubble pending forever.
    let pending = taken([], [], "Most", "a");
    pending = taken(pending, [], "Most", "b");

    const one = [event("user", "ev1", "Most")];
    pending = taken(pending, one, "Most", "c");
    pending = dropped(pending, "a");

    const two = [...one, event("user", "ev2", "Most")];
    expect(reconcile(pending, two)).toEqual([]);
  });

  test("only user narrations settle a turn", () => {
    // An assistant turn quoting the curator back, or a tool row labelled with their
    // words, must not be mistaken for the turn itself.
    const pending = sendAll([], "Most");
    const decoys = [
      event("assistant", "ev1", "Most"),
      toolEvent("ev2", "Most"),
    ];
    expect(reconcile(pending, decoys)).toHaveLength(1);
  });
});
