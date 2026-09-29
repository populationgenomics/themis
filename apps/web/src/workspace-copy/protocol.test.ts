import { describe, expect, test } from "bun:test";
import { parseRequest } from "./protocol";

// A port carries whatever a window posted, so the worker checks every request before acting on it.

const COMMIT = "c41e".padEnd(40, "0");
const FILE = { path: "assets/a.binpb", bytes: new Uint8Array([8, 1]) };

/** A publish envelope that is well-formed except for `overrides`. */
function publish(overrides: Record<string, unknown>) {
  return {
    id: 1,
    request: {
      method: "publish",
      analysisId: "an_1",
      base: COMMIT,
      curatorEmail: "c@example.org",
      message: "m",
      files: [FILE],
      ...overrides,
    },
  };
}

describe("a request from a window", () => {
  test.each([
    { method: "ping" },
    { method: "sync", analysisId: "an_1", tip: COMMIT },
    { method: "readDocument", analysisId: "an_1", commit: COMMIT },
    {
      method: "readFile",
      analysisId: "an_1",
      commit: COMMIT,
      path: "assets/a.binpb",
    },
    { method: "history", analysisId: "an_1", tip: COMMIT },
    { method: "reset", analysisId: "an_1" },
    {
      method: "isAncestor",
      analysisId: "an_1",
      ancestor: COMMIT,
      descendant: COMMIT,
    },
    {
      method: "publish",
      analysisId: "an_1",
      base: COMMIT,
      curatorEmail: "curator@example.org",
      message: "Check PS3",
      files: [{ path: "assets/a.binpb", bytes: new Uint8Array([8, 1]) }],
    },
  ])("$method is read as sent", (request) => {
    expect(parseRequest({ id: 7, request })).toEqual({ id: 7, request });
  });

  test.each([
    ["no envelope", null, /not an object/],
    [
      "an id that is not an integer",
      { id: "7", request: { method: "ping" } },
      /id is not an integer/,
    ],
    ["no request", { id: 1 }, /a request is not an object/],
    [
      "an unknown method",
      { id: 1, request: { method: "delete", analysisId: "an_1" } },
      /not a request the copy answers/,
    ],
    [
      "an Analysis id with a slash",
      {
        id: 1,
        request: { method: "history", analysisId: "an/1", tip: COMMIT },
      },
      /not an Analysis id/,
    ],
    [
      "an abbreviated tip",
      { id: 1, request: { method: "sync", analysisId: "an_1", tip: "c41e" } },
      /tip is not an object id/,
    ],
    [
      "a history request with no tip",
      { id: 1, request: { method: "history", analysisId: "an_1" } },
      /tip is not a commit id/,
    ],
    [
      "no path",
      {
        id: 1,
        request: { method: "readFile", analysisId: "an_1", commit: COMMIT },
      },
      /names no path/,
    ],
    [
      "no curator",
      {
        id: 1,
        request: {
          method: "publish",
          analysisId: "an_1",
          base: COMMIT,
          message: "m",
          files: [FILE],
        },
      },
      /names no curator/,
    ],
    ["no commit message", publish({ message: 7 }), /carries no commit message/],
    [
      "a hole in its files",
      // biome-ignore lint/suspicious/noSparseArray: the hole is the case under test
      publish({ files: [FILE, , FILE] }),
      /an edited file is not an object/,
    ],
    ["no files", publish({ files: [] }), /replaces no file/],
    [
      "a file with no path",
      publish({ files: [{ bytes: FILE.bytes }] }),
      /names no path/,
    ],
    [
      "a file with bytes that are not a Uint8Array",
      publish({ files: [{ path: FILE.path, bytes: [1] }] }),
      /carries no bytes/,
    ],
    [
      "a file twice",
      publish({ files: [FILE, FILE] }),
      /replaces assets\/a.binpb twice/,
    ],
  ])("with %s is refused", (_case, message, reason) => {
    expect(() => parseRequest(message)).toThrow(reason);
  });
});
