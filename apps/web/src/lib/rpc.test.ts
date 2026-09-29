import { describe, expect, test } from "bun:test";
import { Code, ConnectError } from "@connectrpc/connect";
import { Workbench } from "@/models/workbench";
import { isAgentBusy } from "./rpc";

describe("isAgentBusy", () => {
  const refused = new ConnectError("mid-step", Code.FailedPrecondition);

  test("a refused turn is the agent being busy", () => {
    expect(isAgentBusy(Workbench.method.steer, refused)).toBe(true);
  });

  test.each(
    Workbench.methods
      .filter((method) => method !== Workbench.method.steer)
      .map((method) => [method.name, method] as const),
  )("failed_precondition from %s is not", (_name, method) => {
    // A publish whose branch moved answers the same code, and must never read as a busy agent.
    expect(isAgentBusy(method, refused)).toBe(false);
  });

  test("another code from a turn is not", () => {
    expect(
      isAgentBusy(
        Workbench.method.steer,
        new ConnectError("masked", Code.Internal),
      ),
    ).toBe(false);
  });
});
