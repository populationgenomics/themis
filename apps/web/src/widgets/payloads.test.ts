import { expect, test } from "bun:test";
import path from "node:path";
import { type DescFile, getOption } from "@bufbuild/protobuf";
import { nestedTypes } from "@bufbuild/protobuf/reflect";
import { widget } from "@/models/widgets";
import { PAYLOAD_FILES } from "./payloads";

const GENERATED = path.join(import.meta.dir, "..", "gen");
const MODELS = path.join(GENERATED, "themis", "widgets", "models");
// The package every widget payload is declared in, as themis/widgets/asset.py's WIDGET_PACKAGE: the
// guest refuses an asset naming a type in it that its build does not hold, and reads any other
// type it does not hold as no asset at all.
const WIDGET_PACKAGE = "themis.widgets.";
// The file declaring the options, which holds no payload.
const OPTIONS_FILE = "themis/widgets/models/widget.proto";

test("every generated payload file is one the worker reads assets through", async () => {
  const generated: string[] = [];
  for await (const relative of new Bun.Glob("*_pb.ts").scan({ cwd: MODELS })) {
    generated.push(
      `themis/widgets/models/${relative.replace(/_pb\.ts$/, ".proto")}`,
    );
  }
  const listed = new Set(PAYLOAD_FILES.map((file) => file.proto.name));
  const missing = generated.filter(
    (name) => name !== OPTIONS_FILE && !listed.has(name),
  );
  expect(generated.length).toBeGreaterThan(1);
  expect(missing).toEqual([]);
});

function isFile(value: unknown): value is DescFile {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { kind?: unknown }).kind === "file"
  );
}

test("every generated type the widget option marks is in the widgets package", async () => {
  const marked: string[] = [];
  for await (const relative of new Bun.Glob("**/*_pb.ts").scan({
    cwd: GENERATED,
  })) {
    const generated: Record<string, unknown> = await import(
      path.join(GENERATED, relative)
    );
    for (const file of Object.values(generated).filter(isFile)) {
      for (const type of nestedTypes(file)) {
        if (type.kind === "message" && getOption(type, widget)) {
          marked.push(type.typeName);
        }
      }
    }
  }
  expect(marked.length).toBeGreaterThan(0);
  expect(marked.filter((name) => !name.startsWith(WIDGET_PACKAGE))).toEqual([]);
});
