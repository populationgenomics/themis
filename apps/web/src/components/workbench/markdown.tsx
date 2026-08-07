import type { Root } from "mdast";
import { type ReactNode, useMemo } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkDirective from "remark-directive";
import remarkGfm from "remark-gfm";

// GFM markdown rendered into the design tokens. Both panes route prose through
// this: the agent emits real markdown (fenced code, tables, mixed heading
// levels), so the renderer must cover the full grammar, not a bold-only subset.
// react-markdown returns React elements, so there is no dangerouslySetInnerHTML.

// By default nothing rendered here reaches the network. Agent prose carries text
// from untrusted sources, and `img` (fetched on render) and `a` (fetched on click)
// are the only elements that can egress — both render as inert text, with the
// destination dropped so it cannot be copied out either. Paper rendering opts into
// figures via `resolveImage`, which maps a same-origin corpus figure name to our
// files route and returns null for anything else (a scheme, a path) — so an image
// can still only reach the corpus, never an arbitrary URL. Links stay inert always.

// Fenced blocks carry a `language-*` class or span multiple lines; a bare inline
// `code` span carries neither. That split drives block vs. inline styling.
function isBlockCode(
  className: string | undefined,
  children: ReactNode,
): boolean {
  return /language-/.test(className ?? "") || String(children).includes("\n");
}

const components: Components = {
  h1: ({ children }) => (
    <h1 className="mt-[18px] mb-[10px] text-[17px] font-bold tracking-[-0.01em] text-ink-primary first:mt-0">
      {children}
    </h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-[18px] mb-[8px] text-[15px] font-semibold text-ink-primary first:mt-0">
      {children}
    </h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-[14px] mb-[6px] text-[13.5px] font-semibold text-ink-primary first:mt-0">
      {children}
    </h3>
  ),
  h4: ({ children }) => (
    <h4 className="mt-[12px] mb-[6px] text-[13px] font-semibold text-ink-faint first:mt-0">
      {children}
    </h4>
  ),
  p: ({ children }) => (
    <p className="mb-[12px] text-[14px] leading-[1.65] text-ink-body last:mb-0">
      {children}
    </p>
  ),
  ul: ({ children }) => (
    <ul className="mb-[12px] list-disc space-y-[4px] pl-[22px] last:mb-0">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="mb-[12px] list-decimal space-y-[4px] pl-[22px] last:mb-0">
      {children}
    </ol>
  ),
  li: ({ children }) => (
    <li className="text-[14px] leading-[1.6] text-ink-body marker:text-ink-faintest">
      {children}
    </li>
  ),
  a: ({ children }) => children,
  img: ({ alt }) => <InertImage alt={alt} />,
  strong: ({ children }) => (
    <strong className="font-semibold">{children}</strong>
  ),
  em: ({ children }) => <em className="italic">{children}</em>,
  hr: () => <hr className="my-[16px] border-line-soft" />,
  blockquote: ({ children }) => (
    <blockquote className="mb-[12px] border-l-2 border-line-soft pl-[12px] text-[14px] italic text-ink-faint last:mb-0">
      {children}
    </blockquote>
  ),
  pre: ({ children }) => (
    <pre className="tscroll mb-[12px] overflow-x-auto rounded-[8px] border border-line-soft bg-surface-inset px-[13px] py-[11px] last:mb-0">
      {children}
    </pre>
  ),
  code: ({ className, children }) =>
    isBlockCode(className, children) ? (
      <code className="font-mono text-[12px] leading-[1.55] text-ink-body">
        {children}
      </code>
    ) : (
      <code className="rounded-[4px] border border-line-soft bg-surface-inset px-[4px] py-[1px] font-mono text-[12px] text-ink-primary">
        {children}
      </code>
    ),
  table: ({ children }) => (
    <div className="tscroll mb-[12px] overflow-x-auto last:mb-0">
      <table className="w-full border-collapse text-[13px]">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-line-soft bg-surface-inset px-[8px] py-[4px] text-left font-semibold text-ink-primary">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-line-soft px-[8px] py-[4px] text-ink-body">
      {children}
    </td>
  ),
};

function InertImage({ alt }: { alt?: string }) {
  return (
    <span className="font-mono text-[11.5px] text-ink-faintest">
      [image{alt ? `: ${alt}` : ""}]
    </span>
  );
}

// A bare corpus figure name — no scheme, no path separators, no traversal. Anything
// else (an absolute or protocol-relative URL, a nested path) is refused so a figure
// can only ever resolve to a same-origin corpus file.
function isCorpusFigureName(src: string): boolean {
  return src !== "" && !/[:/\\]/.test(src) && !src.includes("..");
}

/** Map a bare figure name to the paper's files route; null for anything that is not a plain
 *  corpus figure name, which keeps the image inert. */
export function corpusFigureResolver(
  fileUrl: (name: string) => string,
): (src: string) => string | null {
  return (src) => (isCorpusFigureName(src) ? fileUrl(src) : null);
}

// A citation the agent embeds in narration or the working document: `:paper[id]` points at a
// paper; `:quote[id, text]` points at a locatable quote within one. Clicking reveals it in the
// document pane (opening the paper tab, then — for a quote — highlighting it).
export type Citation =
  | { kind: "paper"; docId: string }
  | { kind: "quote"; docId: string; quote: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// A minimal structural view of the mdast tree — enough to find directive nodes and read their
// label text without pulling the full mdast/directive type graph in.
interface MdastNode {
  type: string;
  name?: string;
  value?: string;
  children?: MdastNode[];
  data?: { hName?: string; hProperties?: Record<string, string> };
}

function labelText(node: MdastNode): string {
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(labelText).join("");
}

const DIRECTIVE_TYPES = new Set([
  "textDirective",
  "leafDirective",
  "containerDirective",
]);

// remark plugin: turn `:paper` / `:quote` directives into `cite-paper` / `cite-quote` hast elements
// carrying the parsed doc_id (and quote), which the components below render. `:quote[id, text]`
// splits on the first comma — the doc_id is a UUID (comma-free), so the remainder is the quote.
//
// `remark-directive` tokenizes `:name` only when a letter follows the colon (micromark requires the
// name to start with an ASCII alpha), so a colon-then-digit like `chr1:12345`, `3:1`, or `10:30` is
// never a directive and never at risk. A colon-then-letter is: `BRCA1:c.68delAG` tokenizes as name
// `c`, `note:foo` as name `foo` — ordinary prose the parser mistook for a directive. Only paper/quote
// are ours; any other is round-tripped back to literal text. Leaving it as an unhandled directive is
// not benign: `remark-rehype` drops the node *and the token after it*, silently corrupting the agent's
// narration and the working document.
function remarkCitations() {
  return (tree: Root): void => {
    walkDirectives(tree as unknown as MdastNode);
  };
}

function walkDirectives(node: MdastNode): void {
  for (const child of node.children ?? []) {
    if (DIRECTIVE_TYPES.has(child.type) && child.name) {
      const label = labelText(child).trim();
      if (child.name === "paper") {
        child.data = { hName: "cite-paper", hProperties: { docId: label } };
      } else if (child.name === "quote") {
        const comma = label.indexOf(",");
        const docId = (comma === -1 ? label : label.slice(0, comma)).trim();
        const quote = comma === -1 ? "" : label.slice(comma + 1).trim();
        child.data = { hName: "cite-quote", hProperties: { docId, quote } };
      } else {
        literalizeDirective(child);
      }
    }
    walkDirectives(child);
  }
}

// Convert an unrecognized directive node back to the source text the parser consumed, in place: the
// marker (`:`/`::`/`:::`), the name, and any `[label]`. So `chr1:12345` survives as itself instead of
// `chr1` + a dropped `12345`. (Rare `{attrs}` are not reconstructed — a `:name{…}` in prose is far-
// fetched, and a real one would be paper/quote and never reach here.)
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

// react-markdown passes the hast node; the parsed values live on its properties.
type CitationNodeProps = { node?: { properties?: Record<string, unknown> } };

function prop(node: CitationNodeProps["node"], key: string): string {
  const value = node?.properties?.[key];
  return typeof value === "string" ? value : "";
}

function citationComponents(
  onCitation: (citation: Citation) => void,
): Record<string, Components[keyof Components]> {
  return {
    "cite-paper": ({ node }: CitationNodeProps) => (
      <CitationMark
        citation={{ kind: "paper", docId: prop(node, "docId") }}
        onCitation={onCitation}
      >
        source
      </CitationMark>
    ),
    "cite-quote": ({ node }: CitationNodeProps) => {
      const quote = prop(node, "quote");
      return (
        <CitationMark
          citation={{ kind: "quote", docId: prop(node, "docId"), quote }}
          onCitation={onCitation}
        >
          {quote || "quote"}
        </CitationMark>
      );
    },
  };
}

function CitationMark({
  citation,
  onCitation,
  children,
}: {
  citation: Citation;
  onCitation: (citation: Citation) => void;
  children: ReactNode;
}) {
  if (!UUID.test(citation.docId)) {
    return (
      <span
        title={`Unresolved citation: ${citation.docId || "missing id"}`}
        className="rounded-[3px] bg-error-bg px-[3px] text-[13px] text-error-text line-through"
      >
        {children}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onCitation(citation)}
      className="rounded-[3px] px-[2px] text-left text-[14px] text-primary underline decoration-dotted underline-offset-2 hover:bg-surface-inset"
    >
      {children}
    </button>
  );
}

// One root element, not a bare fragment: the rendered blocks must stay grouped
// as a single child of whatever lays the prose out.
export function Markdown({
  text,
  resolveImage,
  onCitation,
}: {
  text: string;
  /** Opt in to rendering figures; see the module comment. Absent ⇒ images stay inert. */
  resolveImage?: (src: string) => string | null;
  /** Opt in to `:paper`/`:quote` citation directives (conversation + working document). Absent ⇒
   *  directives render as plain text. */
  onCitation?: (citation: Citation) => void;
}) {
  // Memoized on the two opt-in callbacks: the `img` / `cite-*` overrides are fresh closures each call,
  // and React reconciles components by identity — a changed identity unmounts and remounts the whole
  // subtree. The workbench re-renders every poll (2.5 s), so without this the prose flickers continually.
  const merged = useMemo<Components>(() => {
    let m: Components = components;
    if (resolveImage) {
      m = {
        ...m,
        img: ({ src, alt }) => {
          const url = typeof src === "string" ? resolveImage(src) : null;
          return url ? (
            // biome-ignore lint/performance/noImgElement: corpus figures, not Next-optimized assets
            <img
              src={url}
              alt={alt ?? ""}
              className="my-[12px] max-w-full rounded-[6px] border border-line-soft"
            />
          ) : (
            <InertImage alt={alt} />
          );
        },
      };
    }
    if (onCitation) {
      m = { ...m, ...citationComponents(onCitation) } as Components;
    }
    return m;
  }, [resolveImage, onCitation]);
  const remarkPlugins = useMemo(
    () =>
      onCitation ? [remarkGfm, remarkDirective, remarkCitations] : [remarkGfm],
    [onCitation],
  );
  return (
    <div>
      <ReactMarkdown remarkPlugins={remarkPlugins} components={merged}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
