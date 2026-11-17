import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, createElement, useEffect } from "react";
import { install } from "@/components/minidom.test-support";
import { workspaceCopy } from "@/workspace-copy/client";
import { WORKSPACE_DAMAGED } from "@/workspace-copy/protocol";
import {
  type DocumentRead,
  resetWorkspaceReads,
  useWorkspaceDocument,
  useWorkspaceFile,
  useWorkspaceHistory,
} from "./queries";

// Workspace-copy reads driven through the exported hooks under `QueryClientProvider`: retryable
// transport/worker failures retry up to three times, definitive repository damage stops immediately,
// and `resetWorkspaceReads` drops every workspace query hook's cache entry for the named Analysis
// without touching another Analysis's.

const COMMIT = "9e27".padEnd(40, "0");
const realSync = workspaceCopy.sync;
const realReadDocument = workspaceCopy.readDocument;
const realHistory = workspaceCopy.history;
const realReadFile = workspaceCopy.readFile;
let dom: ReturnType<typeof install>;
let createRoot: typeof import("react-dom/client").createRoot;

beforeAll(async () => {
  dom = install();
  ({ createRoot } = await import("react-dom/client"));
});

afterEach(() => {
  workspaceCopy.sync = realSync;
  workspaceCopy.readDocument = realReadDocument;
  workspaceCopy.history = realHistory;
  workspaceCopy.readFile = realReadFile;
});

afterAll(() => {
  dom.uninstall();
});

const named = (name: string) => Object.assign(new Error(name), { name });

interface DocumentProbeProps {
  docKey: {
    analysisId: string;
    tip: string;
    commit: string;
    pinned: boolean;
  };
  onSettle: (
    outcome: { ok: true; data: DocumentRead } | { ok: false; error: unknown },
  ) => void;
}

function DocumentProbe({ docKey, onSettle }: DocumentProbeProps) {
  const query = useWorkspaceDocument(docKey);
  useEffect(() => {
    if (query.isSuccess) {
      onSettle({ ok: true, data: query.data });
    } else if (query.isError) {
      onSettle({ ok: false, error: query.error });
    }
  }, [query.isSuccess, query.isError, query.data, query.error, onSettle]);
  return null;
}

async function readWorkspaceDocument(docKey: {
  analysisId: string;
  tip: string;
  commit: string;
  pinned: boolean;
}): Promise<DocumentRead> {
  const client = new QueryClient({
    defaultOptions: { queries: { retryDelay: 0 } },
  });
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);

  try {
    return await new Promise<DocumentRead>((resolve, reject) => {
      let settled = false;
      const onSettle: DocumentProbeProps["onSettle"] = (outcome) => {
        if (settled) return;
        settled = true;
        if (outcome.ok) resolve(outcome.data);
        else reject(outcome.error);
      };
      root.render(
        createElement(
          QueryClientProvider,
          { client },
          createElement(DocumentProbe, { docKey, onSettle }),
        ),
      );
    });
  } finally {
    await act(async () => {
      root.unmount();
    });
    client.clear();
  }
}

function AllWorkspaceReadsProbe({
  analysisId,
  onReady,
}: {
  analysisId: string;
  onReady: () => void;
}) {
  const doc = useWorkspaceDocument({
    analysisId,
    tip: COMMIT,
    commit: COMMIT,
    pinned: false,
  });
  const history = useWorkspaceHistory({ analysisId, tip: COMMIT });
  const file = useWorkspaceFile({
    analysisId,
    tip: COMMIT,
    commit: COMMIT,
    path: "assets/c.binpb",
  });
  useEffect(() => {
    if (doc.isSuccess && history.isSuccess && file.isSuccess) {
      onReady();
    }
  }, [doc.isSuccess, history.isSuccess, file.isSuccess, onReady]);
  return null;
}

async function hydrateAllReads(
  client: QueryClient,
  analysisId: string,
): Promise<void> {
  const container = dom.document.createElement("div");
  dom.document.body.appendChild(container);
  const root = createRoot(container as unknown as Element);
  try {
    await new Promise<void>((resolve) => {
      let done = false;
      root.render(
        createElement(
          QueryClientProvider,
          { client },
          createElement(AllWorkspaceReadsProbe, {
            analysisId,
            onReady: () => {
              if (!done) {
                done = true;
                resolve();
              }
            },
          }),
        ),
      );
    });
  } finally {
    await act(async () => {
      root.unmount();
    });
  }
}

describe("useWorkspaceDocument", () => {
  test("retries after a transient relay failure and resolves when the next read succeeds", async () => {
    let attempts = 0;
    workspaceCopy.sync = async () => {
      attempts += 1;
      if (attempts === 1) throw named("ConnectError");
    };
    workspaceCopy.readDocument = async () => "# Findings\n";

    const read = await readWorkspaceDocument({
      analysisId: "an_1",
      tip: COMMIT,
      commit: COMMIT,
      pinned: false,
    });

    expect(attempts).toBe(2);
    expect(read).toEqual({
      commit: COMMIT,
      value: "# Findings\n",
      pinned: false,
    });
  });

  test("stops retrying a transient failure after three retries", async () => {
    let attempts = 0;
    workspaceCopy.sync = async () => {
      attempts += 1;
      throw named("ConnectError");
    };

    await expect(
      readWorkspaceDocument({
        analysisId: "an_1",
        tip: COMMIT,
        commit: COMMIT,
        pinned: false,
      }),
    ).rejects.toThrow("ConnectError");
    expect(attempts).toBe(4);
  });

  test("never retries a damaged repository", async () => {
    let attempts = 0;
    workspaceCopy.sync = async () => {
      attempts += 1;
      throw named(WORKSPACE_DAMAGED);
    };

    await expect(
      readWorkspaceDocument({
        analysisId: "an_1",
        tip: COMMIT,
        commit: COMMIT,
        pinned: false,
      }),
    ).rejects.toThrow(WORKSPACE_DAMAGED);
    expect(attempts).toBe(1);
  });
});

describe("resetWorkspaceReads", () => {
  test("clears every workspace query hook's cached data for the named Analysis only", async () => {
    workspaceCopy.sync = async () => {};
    workspaceCopy.readDocument = async () => "# Doc\n";
    workspaceCopy.history = async () => [];
    workspaceCopy.readFile = async () => null;

    const client = new QueryClient();
    await hydrateAllReads(client, "an_1");
    await hydrateAllReads(client, "an_2");

    await resetWorkspaceReads(client, "an_1");

    const queriesFor = (analysisId: string) =>
      client
        .getQueryCache()
        .getAll()
        .filter((query) => query.queryKey[1] === analysisId);

    expect(queriesFor("an_1")).toHaveLength(3);
    expect(
      queriesFor("an_1").every((query) => query.state.data === undefined),
    ).toBe(true);
    expect(queriesFor("an_2")).toHaveLength(3);
    expect(
      queriesFor("an_2").every((query) => query.state.data !== undefined),
    ).toBe(true);
  });
});
