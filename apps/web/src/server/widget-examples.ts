import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { toBinary } from "@bufbuild/protobuf";
import { AnySchema, anyPack } from "@bufbuild/protobuf/wkt";
import { assetPathProblem } from "@/widgets/asset";
import { payloadSchema } from "@/widgets/payloads";
import { parseTextproto, TextprotoError } from "@/widgets/textproto";
import { WORKING_DOCUMENT_PATH } from "@/workspace-copy/service";
import type { DocumentFiles } from "./ports";

// The widget browser's examples, read from `src/widgets/examples/<group>/<name>.txtpb` on every
// call, so an edited example shows the next time it is opened; a file at any other depth, or with
// another extension, is not read. Each file names its payload type in the text format's
// `# proto-message:` header, and the comment lines after the header describe it. An example that
// does not read as a payload is reported with why. One that does is packed without being checked
// against its rules, so whether it draws is decided by the embed's own path, `drawAsset`.

/** Where the examples are, from the web app's root, which `bun run dev` runs in. */
export const EXAMPLES_DIR = path.join("src", "widgets", "examples");

/** One example as the browser lists it. */
export interface WidgetExample {
  group: string;
  name: string;
  /** The payload type the header names; empty where it names none. */
  typeName: string;
  /** The comment lines after the header. */
  description: string;
  /** The asset an embed of it reads, the payload packed in an `Any` as `writePayload` packs it;
   *  undefined where the text could not be read as a payload. */
  asset: Uint8Array | undefined;
  /** Why the file does not read as a payload; undefined for one that does, which may still fail
   *  its rules when drawn. */
  error: string | undefined;
}

const MESSAGE_HEADER = /^# proto-message: (\S+)$/m;

/** Every example, by group and then name. Raises where the examples directory is missing: the
 *  browser is run from the web app's root. Never raises for one example's file: its failure is
 *  that example's error. */
export function listExamples(root: string = EXAMPLES_DIR): WidgetExample[] {
  const examples: WidgetExample[] = [];
  for (const group of readdirSync(root).sort()) {
    const dir = path.join(root, group);
    if (!statSync(dir).isDirectory()) continue;
    for (const file of readdirSync(dir).sort()) {
      if (!file.endsWith(".txtpb")) continue;
      const name = file.slice(0, -".txtpb".length);
      try {
        examples.push(
          readExample(group, name, readFileSync(path.join(dir, file), "utf8")),
        );
      } catch (error) {
        // A reader's defect, or a file that cannot be opened: shown on its card, logged in full.
        console.error(`widget example ${group}/${name}:`, error);
        examples.push({
          group,
          name,
          typeName: "",
          description: "",
          asset: undefined,
          error: `the browser failed to read it: ${String(error)}`,
        });
      }
    }
  }
  return examples;
}

/** The example in `text`, filed as `group`/`name`. */
export function readExample(
  group: string,
  name: string,
  text: string,
): WidgetExample {
  const typeName = MESSAGE_HEADER.exec(text)?.[1] ?? "";
  const described = { group, name, typeName, description: description(text) };
  const pathProblem = assetPathProblem(examplePath(group, name));
  if (pathProblem !== null) {
    return {
      ...described,
      asset: undefined,
      error: `its file name makes no asset path: ${pathProblem}`,
    };
  }
  if (typeName === "") {
    return {
      ...described,
      asset: undefined,
      error: 'the file names no "# proto-message:" type',
    };
  }
  const schema = payloadSchema(typeName);
  if (schema === undefined) {
    return {
      ...described,
      asset: undefined,
      error: `${typeName} is not a widget payload type this build knows`,
    };
  }
  let payload: ReturnType<typeof parseTextproto>;
  try {
    payload = parseTextproto(schema, text);
  } catch (error) {
    if (!(error instanceof TextprotoError)) throw error;
    return { ...described, asset: undefined, error: `line ${error.message}` };
  }
  return {
    ...described,
    asset: toBinary(AnySchema, anyPack(schema, payload)),
    error: undefined,
  };
}

/** The comment lines after the `# proto-file:` and `# proto-message:` header, up to the first line
 *  that is not a comment, joined into one paragraph per blank comment line. */
function description(text: string): string {
  const lines: string[] = [];
  for (const line of text.split("\n")) {
    if (!line.startsWith("#")) break;
    const body = line.replace(/^#\s?/, "");
    if (/^proto-(file|message):/.test(body)) continue;
    lines.push(body);
  }
  return lines
    .join("\n")
    .trim()
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.replace(/\s*\n\s*/g, " "))
    .join("\n\n");
}

/** Where the example filed as `group`/`name` sits in the seeded repository, and what its embed
 *  names. */
export function examplePath(group: string, name: string): string {
  return `assets/examples/${group}/${name}.binpb`;
}

/** The repository a set of examples opens in: a working document with a section per example,
 *  each embedding its asset, or saying why it could not. */
export function exampleDocument(
  title: string,
  examples: readonly WidgetExample[],
): DocumentFiles {
  const files: Record<string, string | Uint8Array> = {};
  const sections = examples.map((example) => {
    const heading = `## ${example.group}/${example.name}`;
    const about = example.description === "" ? [] : [example.description];
    if (example.asset === undefined) {
      return [
        heading,
        ...about,
        `> This example does not read as a payload: ${example.error}`,
      ].join("\n\n");
    }
    const at = examplePath(example.group, example.name);
    files[at] = example.asset;
    return [heading, ...about, `::embed[${at}]`].join("\n\n");
  });
  files[WORKING_DOCUMENT_PATH] = [
    `# ${title}`,
    "Opened from `apps/web/src/widgets/examples`. Ticks and notes made here are kept in this local server's memory and go when it stops; open the examples again from the widget browser for a fresh copy.",
    ...sections,
    "",
  ].join("\n\n");
  return files;
}
