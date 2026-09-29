import { type ReactNode, useMemo } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown, { type Options } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkDirective from "remark-directive";
import remarkGfm from "remark-gfm";
import { Embed, WidgetSurface } from "@/components/widgets/embed";
import type { WidgetRevision } from "@/components/widgets/revision";
import { type Citation, CitationMark } from "./citation";
import { remarkDirectives } from "./directives";

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

// react-markdown passes the hast node; the parsed values live on its properties.
type CitationNodeProps = { node?: { properties?: Record<string, unknown> } };

function prop(node: CitationNodeProps["node"], key: string): string {
  const value = node?.properties?.[key];
  return typeof value === "string" ? value : "";
}

// One component for every render, reading its revision from context: a component whose identity
// changed would be remounted on each Poll.
function EmbedElement({ node }: CitationNodeProps) {
  return (
    <Embed path={prop(node, "path")} problem={prop(node, "problem") || null} />
  );
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

export type { Citation };

// One root element, not a bare fragment: the rendered blocks must stay grouped
// as a single child of whatever lays the prose out.
export function Markdown({
  text,
  resolveImage,
  onCitation,
  revision,
  breaks,
}: {
  text: string;
  /** Opt in to rendering figures; see the module comment. Absent ⇒ images stay inert. */
  resolveImage?: (src: string) => string | null;
  /** Opt in to `:paper`/`:quote` citation directives (conversation + working document). Absent ⇒
   *  directives render as plain text. */
  onCitation?: (citation: Citation) => void;
  /** Opt in to `::embed` widgets, drawn from this revision's tree (the working document); needs
   *  `onCitation`, which a widget's citations raise. Absent ⇒ the directive renders as plain text. */
  revision?: WidgetRevision;
  /** Opt in to honouring single newlines as line breaks (chat-style prose: the curator's typed
   *  turns). Absent ⇒ standard markdown, which folds them into the paragraph. */
  breaks?: boolean;
}) {
  if (revision !== undefined && onCitation === undefined) {
    throw new Error("a surface drawing widgets has to handle their citations");
  }
  const drawsWidgets = revision !== undefined;
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
    if (drawsWidgets) {
      m = { ...m, "widget-embed": EmbedElement } as Components;
    }
    return m;
  }, [resolveImage, onCitation, drawsWidgets]);
  const remarkPlugins = useMemo(() => {
    const citations = onCitation !== undefined;
    const embeds = drawsWidgets;
    const plugins: NonNullable<Options["remarkPlugins"]> =
      citations || embeds
        ? [
            remarkGfm,
            remarkDirective,
            [remarkDirectives, { citations, embeds }],
          ]
        : [remarkGfm];
    return breaks ? [...plugins, remarkBreaks] : plugins;
  }, [onCitation, drawsWidgets, breaks]);
  const body = (
    <ReactMarkdown remarkPlugins={remarkPlugins} components={merged}>
      {text}
    </ReactMarkdown>
  );
  return (
    <div>
      {revision !== undefined && onCitation !== undefined ? (
        <WidgetSurface value={{ revision, onCitation }}>{body}</WidgetSurface>
      ) : (
        body
      )}
    </div>
  );
}
