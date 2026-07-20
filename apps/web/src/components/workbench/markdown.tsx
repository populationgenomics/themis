import type { ReactNode } from "react";
import type { Components } from "react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// GFM markdown rendered into the design tokens. Both panes route prose through
// this: the agent emits real markdown (fenced code, tables, mixed heading
// levels), so the renderer must cover the full grammar, not a bold-only subset.
// react-markdown returns React elements, so there is no dangerouslySetInnerHTML.

// Nothing rendered here reaches the network. Agent prose carries text from
// untrusted sources, and `img` (fetched on render) and `a` (fetched on click)
// are the only elements that can egress — both render as inert text, with the
// destination dropped so it cannot be copied out either.

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
  img: ({ alt }) => (
    <span className="font-mono text-[11.5px] text-ink-faintest">
      [image{alt ? `: ${alt}` : ""}]
    </span>
  ),
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

// One root element, not a bare fragment: the rendered blocks must stay grouped
// as a single child of whatever lays the prose out.
export function Markdown({ text }: { text: string }) {
  return (
    <div>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
