import type { Root } from "mdast";

// The directive grammar the shared markdown renderer reads: the citations `:paper[id]` and
// `:quote[id, text]`, and the widget `::embed[<path>]`, each only on a surface that opts in; every
// other directive, and an opted-out one, as the literal text it was written as.

// A minimal structural view of the mdast tree — enough to find directive nodes and read their
// label text without pulling the full mdast/directive type graph in.
interface MdastNode {
  type: string;
  name?: string;
  value?: string;
  children?: MdastNode[];
  data?: {
    hName?: string;
    hProperties?: Record<string, string>;
    directiveLabel?: boolean;
  };
  position?: { start: { offset?: number }; end: { offset?: number } };
}

// mdast keeps a node's own characters in `value` on these types and in `children` on every other.
// `image` and `footnoteReference` are childless and carry none, so a missing `children` does not
// imply an empty label.
const VALUE_TYPES = new Set(["text", "inlineCode", "html", "code"]);

function labelText(node: MdastNode): string {
  if (VALUE_TYPES.has(node.type)) return node.value ?? "";
  return (node.children ?? []).map(labelText).join("");
}

const DIRECTIVE_TYPES = new Set([
  "textDirective",
  "leafDirective",
  "containerDirective",
]);

// remark plugin: turn `:paper[id]` / `:quote[id, text]` directives into `cite-paper` / `cite-quote`
// hast elements carrying the parsed doc_id (and quote), which the components below render. `:quote`
// splits on the first comma — the doc_id is a UUID (comma-free), so the remainder is the quote.
//
// `remark-directive` tokenizes `:name` only when a letter follows the colon (micromark requires the
// name to start with an ASCII alpha), so a colon-then-digit like `chr1:12345`, `3:1`, or `10:30` is
// never a directive and never at risk. A colon-then-letter is: `BRCA1:c.68delAG` tokenizes as name
// `c`, `note:foo` as name `foo`, and the bare word in `the :quote directive` as name `quote` —
// ordinary prose the parser mistook for a directive. So the citation trigger is a paper/quote name
// carrying a `[…]` label; the bare word is prose, and every other directive is round-tripped back to
// literal text. Leaving one as an unhandled directive is not benign: `remark-rehype` drops the node
// *and the token after it*, silently corrupting the agent's narration and the working document.
//
// On a surface that draws widgets, the leaf directive `::embed[<path>]` becomes a `widget-embed`
// element carrying the path (docs/design/document-widgets.md). The path is the label's raw source,
// never its parsed text: markdown reads `_a_` as emphasis and `www.a.b` as a link, so the parsed
// text of a label can differ from what the guest's linter checked. The label's end is found by
// scanning the source with micromark's label rule, the same scan the linter makes
// (themis/document_linter/embeds.py). An embed draws only written exactly as `::embed[<path>]` on
// a line of its own at the top level of the document; any other `::embed` directive carries the
// problem the placeholder names, as the linter reports every other line opening with `::embed`.
export interface DirectiveSurface {
  citations: boolean;
  embeds: boolean;
}

export function remarkDirectives(surface: DirectiveSurface) {
  return (tree: Root, file: { value: unknown }): void => {
    walkDirectives(tree as unknown as MdastNode, surface, String(file.value));
  };
}

function walkDirectives(
  node: MdastNode,
  surface: DirectiveSurface,
  source: string,
): void {
  node.children = node.children?.flatMap((child) =>
    child.type === "containerDirective" ? unwrapContainer(child) : [child],
  );
  for (const child of node.children ?? []) {
    if (DIRECTIVE_TYPES.has(child.type) && child.name) {
      const data =
        surface.embeds &&
        child.type === "leafDirective" &&
        child.name === "embed"
          ? embedData(child, source, node.type === "root")
          : surface.citations && labelled(child)
            ? citationData(child.name, labelText(child).trim())
            : null;
      if (data) child.data = data;
      else literalizeDirective(child);
    }
    walkDirectives(child, surface, source);
  }
}

/** A container directive as the prose it was written as: its opening line as text, its block
 *  content in its parent's place, and the closing fence. Its content keeps its own level, which is
 *  how the linter, knowing no containers, reads it. */
function unwrapContainer(node: MdastNode): MdastNode[] {
  const children = node.children ?? [];
  const label = children[0]?.data?.directiveLabel ? labelText(children[0]) : "";
  const content = label ? children.slice(1) : children;
  const line = (value: string): MdastNode => ({
    type: "paragraph",
    children: [{ type: "text", value }],
  });
  return [
    line(`:::${node.name ?? ""}${label ? `[${label}]` : ""}`),
    ...content,
    line(":::"),
  ];
}

const NOT_EXACT =
  "the directive has to be written exactly as ::embed[<path>] on a line of its own";
const NESTED =
  "the directive has to stand at the top level of the document, outside any list, quote or note";

/** The `widget-embed` element for a leaf `::embed`, its label read out of `source` as written.
 *  `problem` is empty unless the directive itself is not one that draws. Drops the parsed label,
 *  which is not what is drawn. */
function embedData(
  node: MdastNode,
  source: string,
  topLevel: boolean,
): MdastNode["data"] {
  node.children = [];
  const start = node.position?.start.offset;
  const end = node.position?.end.offset;
  const element = (path: string, problem: string): MdastNode["data"] => ({
    hName: "widget-embed",
    hProperties: { path, problem },
  });
  if (start === undefined || end === undefined) {
    return element("", "the directive's source could not be read");
  }
  const open = start + "::embed".length;
  const close = source[open] === "[" ? labelEnd(source, open + 1) : undefined;
  const path = close === undefined ? "" : source.slice(open + 1, close);
  if (
    close === undefined ||
    source.slice(start, end).trimEnd() !== `::embed[${path}]`
  ) {
    return element(path, NOT_EXACT);
  }
  return element(path, topLevel ? "" : NESTED);
}

const MAX_LABEL_DEPTH = 32;
const ESCAPABLE = new Set(["[", "\\", "]"]);

/** The index of the `]` closing a label whose text starts at `start`, by micromark's label rule
 *  (a backslash escapes `[`, `\` and `]`; brackets nest, at most 32 deep; no line ending), or
 *  undefined when the label does not close. */
export function labelEnd(source: string, start: number): number | undefined {
  let depth = 0;
  let position = start;
  while (position < source.length) {
    const character = source[position];
    if (character === "\n" || character === "\r") return undefined;
    if (character === "\\" && ESCAPABLE.has(source[position + 1] ?? "")) {
      position += 2;
      continue;
    }
    if (character === "[") {
      depth += 1;
      if (depth > MAX_LABEL_DEPTH) return undefined;
    } else if (character === "]") {
      if (depth === 0) return position;
      depth -= 1;
    }
    position += 1;
  }
  return undefined;
}

// For a text or leaf directive the children are exactly the label, and `:paper` and `:paper[]` both
// tokenize to none — so a label's presence is structural, not a count of the characters in it. A
// container's children are its block content, which this reads as a label.
function labelled(node: MdastNode): boolean {
  return (node.children ?? []).length > 0;
}

function citationData(name: string, label: string): MdastNode["data"] | null {
  if (name === "paper") {
    return { hName: "cite-paper", hProperties: { docId: label } };
  }
  if (name === "quote") {
    const comma = label.indexOf(",");
    const docId = (comma === -1 ? label : label.slice(0, comma)).trim();
    const quote = comma === -1 ? "" : label.slice(comma + 1).trim();
    return { hName: "cite-quote", hProperties: { docId, quote } };
  }
  return null;
}

// Convert a non-citation directive node back to the source text the parser consumed, in place: the
// marker (`:`/`::`/`:::`), the name, and any `[label]`. So `chr1:12345` survives as itself instead of
// `chr1` + a dropped `12345`. The text is synthesised from the node rather than sliced out of the
// source the node points at, so `{attrs}`, an empty `[]`, markup or escapes inside the label, and a
// directive nested inside the label do not come back.
function literalizeDirective(node: MdastNode): void {
  const marker =
    node.type === "containerDirective"
      ? ":::"
      : node.type === "leafDirective"
        ? "::"
        : ":";
  const label = labelText(node);
  node.type = "text";
  node.value = `${marker}${node.name ?? ""}${label ? `[${label}]` : ""}`;
  node.name = undefined;
  node.children = undefined;
  node.data = undefined;
}
