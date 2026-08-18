import type { Components } from "hast-util-to-jsx-runtime";
import { toJsxRuntime } from "hast-util-to-jsx-runtime";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import { createLowlight } from "lowlight";
import { Fragment, type ReactNode, useMemo } from "react";
import { jsx, jsxs } from "react/jsx-runtime";
import { ToolLanguage } from "@/models/workbench";

// A tool call's text — its body or its result — in the language the projection named
// (docs/design/conversation-view.md).

const REGISTERED_GRAMMARS = { bash, json, markdown, python, typescript };

export const lowlight = createLowlight(REGISTERED_GRAMMARS);

// `null` renders the text as it stands.
export const GRAMMAR_BY_LANGUAGE: Readonly<
  Record<ToolLanguage, keyof typeof REGISTERED_GRAMMARS | null>
> = {
  [ToolLanguage.UNSPECIFIED]: null,
  [ToolLanguage.PYTHON]: "python",
  [ToolLanguage.SHELL]: "bash",
  [ToolLanguage.MARKDOWN]: "markdown",
  [ToolLanguage.JSON]: "json",
  [ToolLanguage.TYPESCRIPT]: "typescript",
};

// An empty ink leaves the span uncoloured: the scope wraps a construct rather than
// marking a token, so colouring it would wash the tokens inside it.
export const TOKEN_INK: Readonly<Record<string, string>> = {
  "hljs-function": "",
  "hljs-comment": "text-ink-faint",
  "hljs-quote": "text-ink-faint",
  "hljs-keyword": "text-agent-fg",
  "hljs-literal": "text-agent-fg",
  "hljs-string": "text-teal-fg",
  "hljs-regexp": "text-teal-fg",
  "hljs-number": "text-path-pill-fg",
  "hljs-meta": "text-subagent-fg",
  "hljs-built_in": "text-ink-primary",
  "hljs-title": "text-ink-primary",
  "hljs-type": "text-ink-primary",
  "hljs-variable": "text-ink-primary",
  "hljs-subst": "text-ink-primary",
  "hljs-params": "text-ink-body",
  "hljs-punctuation": "text-ink-faint",
  "hljs-attr": "text-agent-fg",
  "hljs-property": "text-agent-fg",
  "hljs-section": "font-semibold text-ink-primary",
  "hljs-strong": "font-semibold text-ink-primary",
  "hljs-emphasis": "italic",
  "hljs-bullet": "text-ink-faintest",
  "hljs-code": "text-teal-fg",
  "hljs-link": "text-agent-fg",
  "hljs-symbol": "text-path-pill-fg",
};

/** The ink for a token span. highlight.js states a scope as a class list —
 *  `hljs-title function_` — of which one entry is the scope proper. */
function tokenInk(className: string | undefined): string | undefined {
  for (const name of className?.split(" ") ?? []) {
    const ink = TOKEN_INK[name];
    if (ink) return ink;
  }
  return undefined;
}

const components: Components = {
  span: ({ className, children }) => (
    <span className={tokenInk(className)}>{children}</span>
  ),
};

export function ToolText({
  text,
  language,
}: {
  text: string;
  language: ToolLanguage;
}): ReactNode {
  // Lexing is linear in the body, which is untruncated, and the poll re-renders every
  // expanded row on every tick.
  return useMemo(() => {
    // A language number this build predates indexes nothing, and renders unlit.
    const grammar = GRAMMAR_BY_LANGUAGE[language] ?? null;
    if (grammar === null) return text;
    return toJsxRuntime(lowlight.highlight(grammar, text), {
      Fragment,
      jsx,
      jsxs,
      components,
    });
  }, [text, language]);
}
