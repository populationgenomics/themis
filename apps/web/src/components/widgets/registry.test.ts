import { describe, expect, test } from "bun:test";
import path from "node:path";
import { type DescFile, type DescMessage, getOption } from "@bufbuild/protobuf";
import { nestedTypes } from "@bufbuild/protobuf/reflect";
import { widget } from "@/models/widgets";
import { WIDGETS } from "./registry";

// The registry is hand-written and the marked set is the contract's, so the two are held to each
// other here: a payload type the `widget` option marks with no component would draw as a placeholder
// in every document that embeds it. The marked set is read off every descriptor the build generates,
// not off a list, so a newly marked type fails this until its component is registered.

const GENERATED = path.join(import.meta.dir, "..", "..", "gen");

async function generatedFiles(): Promise<DescFile[]> {
  const files: DescFile[] = [];
  for await (const relative of new Bun.Glob("**/*_pb.ts").scan({
    cwd: GENERATED,
  })) {
    const module: Record<string, unknown> = await import(
      path.join(GENERATED, relative)
    );
    for (const value of Object.values(module)) {
      if (
        typeof value === "object" &&
        value !== null &&
        (value as { kind?: unknown }).kind === "file"
      ) {
        files.push(value as DescFile);
      }
    }
  }
  return files;
}

async function markedTypes(): Promise<DescMessage[]> {
  const marked: DescMessage[] = [];
  for (const file of await generatedFiles()) {
    for (const type of nestedTypes(file)) {
      if (type.kind === "message" && getOption(type, widget)) marked.push(type);
    }
  }
  return marked;
}

describe("the widget registry", () => {
  test("draws every payload type the widget option marks", async () => {
    const marked = await markedTypes();
    expect(marked.length).toBeGreaterThan(0);
    const unregistered = marked
      .map((type) => type.typeName)
      .filter((name) => !WIDGETS.has(name));
    expect(unregistered).toEqual([]);
  });

  test("maps only marked types, each under its own name", async () => {
    const marked = new Set((await markedTypes()).map((type) => type.typeName));
    for (const [name, entry] of WIDGETS) {
      expect(entry.schema.typeName).toBe(name);
      expect(marked.has(name)).toBe(true);
    }
  });
});
