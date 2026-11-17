import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { create } from "@bufbuild/protobuf";
import { Code, ConnectError } from "@connectrpc/connect";
import { workbench } from "@/lib/rpc";
import {
  type ConversationEvent,
  ConversationEventSchema,
} from "@/models/workbench";
import {
  click,
  findAll,
  input,
  install,
  MiniElement,
} from "../minidom.test-support";

let dom: ReturnType<typeof install>;
let React: typeof import("react");
let createRoot: typeof import("react-dom/client").createRoot;
let QueryClient: typeof import("@tanstack/react-query").QueryClient;
let QueryClientProvider: typeof import("@tanstack/react-query").QueryClientProvider;
let SteerComposer: typeof import("./steer-composer").SteerComposer;
let useSteering: typeof import("./use-steering").useSteering;
const realSteer = workbench.steer;
const realInterrupt = workbench.interrupt;
const unmounts: (() => void)[] = [];

beforeAll(async () => {
  dom = install();
  React = await import("react");
  ({ createRoot } = await import("react-dom/client"));
  ({ QueryClient, QueryClientProvider } = await import(
    "@tanstack/react-query"
  ));
  ({ SteerComposer } = await import("./steer-composer"));
  ({ useSteering } = await import("./use-steering"));
});

afterEach(() => {
  workbench.steer = realSteer;
  workbench.interrupt = realInterrupt;
  while (unmounts.length > 0) unmounts.pop()?.();
});

afterAll(() => {
  dom.uninstall();
});

function toolEvent(settled: boolean): ConversationEvent {
  return create(ConversationEventSchema, {
    id: "ev_tool",
    kind: {
      case: "tool",
      value: {
        name: "read",
        intent: "/workspace/working_document.md",
        result: settled ? { output: "12 lines" } : undefined,
      },
    },
  });
}

function SteerSurface({ events }: { events: ConversationEvent[] | undefined }) {
  const steering = useSteering("an_1", events);
  return <SteerComposer steering={steering} />;
}

async function mountComposer(initialEvents: ConversationEvent[] | undefined) {
  const client = new QueryClient();
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  unmounts.push(() => {
    React.act(() => root.unmount());
    dom.document.body.removeChild(container);
  });

  const rerender = async (events: ConversationEvent[] | undefined) => {
    await React.act(async () => {
      root.render(
        <QueryClientProvider client={client}>
          <SteerSurface events={events} />
        </QueryClientProvider>,
      );
    });
  };
  await rerender(initialEvents);

  const byLabel = (ariaLabel: string): MiniElement => {
    const [found] = findAll(
      container,
      (node) =>
        node instanceof MiniElement &&
        node.getAttribute("aria-label") === ariaLabel,
    );
    if (!(found instanceof MiniElement)) {
      throw new Error(`no element labelled "${ariaLabel}"`);
    }
    return found;
  };

  return {
    container,
    rerender,
    textarea: () => byLabel("Steer this analysis"),
    sendButton: () => byLabel("Send this turn"),
    stopButton: () => byLabel("Stop the agent's current step"),
    alertText: () => {
      const [alert] = findAll(
        container,
        (node) =>
          node instanceof MiniElement && node.getAttribute("role") === "alert",
      );
      return alert?.textContent ?? null;
    },
    type: async (value: string) => {
      await React.act(async () =>
        input(container, byLabel("Steer this analysis"), value),
      );
    },
  };
}

const BUSY_ALERT =
  "The agent is still working on its current step — wait for it to finish, or stop it first.";

describe("SteerComposer", () => {
  test("disables sending and stopping until the first poll resolves", async () => {
    const surface = await mountComposer(undefined);
    expect(surface.textarea().getAttribute("placeholder")).toBe(
      "Loading the run…",
    );
    expect(surface.sendButton().disabled).toBe(true);
    expect(surface.stopButton().disabled).toBe(true);
  });

  test("keeps Send disabled until the draft holds non-whitespace text", async () => {
    const surface = await mountComposer([]);
    expect(surface.textarea().getAttribute("placeholder")).toBe(
      "Write a message…",
    );
    expect(surface.stopButton().disabled).toBe(false);
    expect(surface.sendButton().disabled).toBe(true);

    await surface.type("   ");
    expect(surface.sendButton().disabled).toBe(true);

    await surface.type("Check PM2");
    expect(surface.sendButton().disabled).toBe(false);
  });

  test("explains that the agent is busy on a refused turn and restores the draft", async () => {
    workbench.steer = (async () => {
      throw new ConnectError("mid-step", Code.FailedPrecondition);
    }) as typeof workbench.steer;

    const surface = await mountComposer([]);
    await surface.type("Check PM2");
    expect(surface.sendButton().disabled).toBe(false);
    await React.act(async () => click(surface.container, surface.sendButton()));

    expect(surface.alertText()).toBe(BUSY_ALERT);
    expect(surface.textarea().value).toBe("Check PM2");
  });

  test("keeps the busy alert while a tool call stays in flight and retires it once settled", async () => {
    workbench.steer = (async () => {
      throw new ConnectError("mid-step", Code.FailedPrecondition);
    }) as typeof workbench.steer;

    const surface = await mountComposer([]);
    await surface.type("Check PM2");
    await React.act(async () => click(surface.container, surface.sendButton()));
    expect(surface.alertText()).toBe(BUSY_ALERT);

    await surface.rerender([toolEvent(false)]);
    expect(surface.alertText()).toBe(BUSY_ALERT);

    await surface.rerender([toolEvent(true)]);
    expect(surface.alertText()).toBeNull();
  });

  test("does not report a FailedPrecondition from stopping as busy, and keeps the alert across polls", async () => {
    workbench.interrupt = (async () => {
      throw new ConnectError("branch moved", Code.FailedPrecondition);
    }) as typeof workbench.interrupt;

    const surface = await mountComposer([]);
    expect(surface.stopButton().disabled).toBe(false);
    await React.act(async () => click(surface.container, surface.stopButton()));

    expect(surface.alertText()).toBe("Could not stop the agent: branch moved");
    await surface.rerender([]);
    expect(surface.alertText()).toBe("Could not stop the agent: branch moved");
  });

  test.each([
    [
      "a ConnectError without its code prefix",
      new ConnectError("masked", Code.Internal),
      "Could not send that turn: masked",
    ],
    [
      "a transport Error",
      new Error("Failed to fetch"),
      "Could not send that turn: Failed to fetch",
    ],
  ])(
    "reports a failed turn from %s and keeps the alert across polls",
    async (_label, failure, expected) => {
      workbench.steer = (async () => {
        throw failure;
      }) as typeof workbench.steer;

      const surface = await mountComposer([]);
      await surface.type("Check PM2");
      await React.act(async () =>
        click(surface.container, surface.sendButton()),
      );
      expect(surface.alertText()).toBe(expected);

      await surface.rerender([]);
      expect(surface.alertText()).toBe(expected);
    },
  );
});
