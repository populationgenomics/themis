import { ToolLanguage } from "@/models/workbench";
import { type DiffLineInit, replacementDiff } from "./tool-diff";

// Server-side tool-call presentation: the one-line label a tool-call row shows, and the
// body it reveals on expand, both derived from a tool_use input
// (docs/design/conversation-view.md).

// The prebuilt tools carry their subject under one of these input keys, tried in order.
const TARGET_KEYS = ["file_path", "pattern", "path", "url", "query"] as const;

const WRITTEN_LANGUAGES: ReadonlyMap<string, ToolLanguage> = new Map([
  [".py", ToolLanguage.PYTHON],
  [".sh", ToolLanguage.SHELL],
  [".bash", ToolLanguage.SHELL],
  [".md", ToolLanguage.MARKDOWN],
  [".markdown", ToolLanguage.MARKDOWN],
  [".json", ToolLanguage.JSON],
  [".ts", ToolLanguage.TYPESCRIPT],
  [".tsx", ToolLanguage.TYPESCRIPT],
]);

// A heredoc opener (`<<EOF`, `<<-EOF`, `<<'EOF'`, `<<\EOF`) — the shell's own `<<<`
// here-string is not one, hence the leading `[^<]` and the lookahead.
const HEREDOC_OPENER = /(^|[^<])<<-?(?!<)[ \t]*\\?['"]?\w/;

/** The body a tool-call row reveals on expand: either text in a language, or a
 *  replacement line by line. */
type ToolBody =
  | { kind: "text"; text: string; language: ToolLanguage }
  | { kind: "diff"; lines: DiffLineInit[] };

/** A one-line label for a tool call. The custom `shell` tool uses its model-stated
 *  `intent`; every prebuilt tool uses its well-known target field, falling back to the
 *  tool name so the label is never empty. */
function deriveToolLabel(name: string, input: Record<string, unknown>): string {
  const intent = input.intent;
  if (typeof intent === "string" && intent.trim() !== "") return intent.trim();
  return wellKnownTarget(input)?.trim() ?? name;
}

/** The body, untruncated: the shell command, the content a write wrote, the two sides of
 *  an edit, else the whole input. */
function toolBody(input: Record<string, unknown>): ToolBody {
  const command = nonBlankString(input, "command");
  if (command !== null) {
    return {
      kind: "text",
      text: command,
      language: HEREDOC_OPENER.test(command)
        ? ToolLanguage.UNSPECIFIED
        : ToolLanguage.SHELL,
    };
  }
  // Presence, not content: a write that empties a file wrote an empty body.
  const content = input.content;
  if (typeof content === "string") {
    return { kind: "text", text: content, language: writtenLanguage(input) };
  }
  const before = input.old_string;
  const after = input.new_string;
  if (typeof before === "string" && typeof after === "string") {
    // A replacement of nothing by nothing draws no line, leaving the input the only body.
    const lines = replacementDiff(before, after);
    if (lines.length > 0) return { kind: "diff", lines };
  }
  return {
    kind: "text",
    text: JSON.stringify(input, null, 2),
    language: ToolLanguage.JSON,
  };
}

/** The `ToolCall` fields for one tool_use. The single place a body is chosen, so
 *  `command` and `diff` are never both set. */
export function projectToolCall(
  name: string,
  input: Record<string, unknown>,
): {
  name: string;
  intent: string;
  command: string;
  language: ToolLanguage;
  diff: DiffLineInit[];
} {
  const body = toolBody(input);
  return {
    name,
    intent: deriveToolLabel(name, input),
    command: body.kind === "text" ? body.text : "",
    language: body.kind === "text" ? body.language : ToolLanguage.UNSPECIFIED,
    diff: body.kind === "diff" ? body.lines : [],
  };
}

/** The language of written content, from the extension of the file it was written to. */
function writtenLanguage(input: Record<string, unknown>): ToolLanguage {
  const path =
    nonBlankString(input, "file_path") ?? nonBlankString(input, "path");
  if (path === null) return ToolLanguage.UNSPECIFIED;
  const dot = path.lastIndexOf(".");
  if (dot === -1) return ToolLanguage.UNSPECIFIED;
  return (
    WRITTEN_LANGUAGES.get(path.slice(dot).toLowerCase()) ??
    ToolLanguage.UNSPECIFIED
  );
}

function nonBlankString(
  input: Record<string, unknown>,
  key: string,
): string | null {
  const value = input[key];
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function wellKnownTarget(input: Record<string, unknown>): string | null {
  for (const key of TARGET_KEYS) {
    const value = nonBlankString(input, key);
    if (value !== null) return value;
  }
  return null;
}
