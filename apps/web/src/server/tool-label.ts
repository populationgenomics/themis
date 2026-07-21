// Server-side tool-call presentation: the one-line label and the full command a
// tool-call row shows, derived from a tool_use input. A custom tool (the sandbox
// `shell` tool) carries a model-stated `intent`; every prebuilt tool has none, so
// its label is one well-known structured target field read straight off the input
// (the tool name is shown separately as the row's tag). A plain field read — never
// command parsing. The full command/input stays on the row's expand.

// The prebuilt tools carry their subject under one of these input keys, tried in
// order. A plain read, no parsing.
const TARGET_KEYS = ["file_path", "path", "pattern", "url"] as const;

/** A one-line label for a tool call. The custom `shell` tool uses its model-stated
 *  `intent`; every prebuilt tool uses its well-known target field, falling back to
 *  the tool name so the label is never empty. */
export function deriveToolLabel(
  name: string,
  input: Record<string, unknown>,
): string {
  const intent = input.intent;
  if (typeof intent === "string" && intent.trim() !== "") return intent.trim();
  return wellKnownTarget(input) ?? name;
}

/** The full, untruncated invocation the row reveals on expand: the shell command,
 *  another tool's target, else the input JSON so nothing is hidden. Never empty. */
export function toolCommand(input: Record<string, unknown>): string {
  const command = input.command;
  if (typeof command === "string" && command.trim() !== "") return command;
  return wellKnownTarget(input) ?? JSON.stringify(input);
}

function wellKnownTarget(input: Record<string, unknown>): string | null {
  for (const key of TARGET_KEYS) {
    const value = input[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}
