import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ToolLanguage, ToolLanguageSchema } from "@/models/workbench";
import {
  GRAMMAR_BY_LANGUAGE,
  lowlight,
  TOKEN_INK,
  ToolText,
} from "./tool-text";

// The highlighter's two standing obligations: every language the wire can carry has a
// grammar and an ink for the scopes it emits, and nothing it renders can become markup.

const SAMPLES: Readonly<Record<string, string>> = {
  bash: "cd /workspace && grep -c '^### ' doc.md # count",
  python: "import sys\n\ndef main() -> int:\n    print('hi')\n    return 0",
  markdown: "# Title\n\n- a **bold** item\n\n`code` and [a link](http://x)",
  json: '{\n  "file_path": "/a.md",\n  "view_range": [1, 40]\n}',
  typescript: `export interface X { a: string }\nconst f = (n: number): string => \`\${n}\`;`,
};

/** Every `hljs-*` scope class a highlighted sample emits. */
function scopesIn(grammar: string, source: string): Set<string> {
  const found = new Set<string>();
  const walk = (node: {
    type: string;
    properties?: { className?: unknown };
    children?: unknown[];
  }) => {
    const classes = node.properties?.className;
    if (Array.isArray(classes)) {
      for (const name of classes) {
        if (typeof name === "string" && name.startsWith("hljs-"))
          found.add(name);
      }
    }
    for (const child of node.children ?? []) {
      walk(child as Parameters<typeof walk>[0]);
    }
  };
  walk(lowlight.highlight(grammar, source) as Parameters<typeof walk>[0]);
  return found;
}

describe("the highlighter", () => {
  test("every language the wire can carry has a registered grammar", () => {
    // Read off the generated descriptor, not a hand-written list: a language added to the
    // proto and mapped to `null` to satisfy the compiler would otherwise render unlit.
    const named = ToolLanguageSchema.values.filter(
      (value) => value.number !== ToolLanguage.UNSPECIFIED,
    );
    expect(named.length).toBeGreaterThan(0);
    for (const value of named) {
      const grammar = GRAMMAR_BY_LANGUAGE[value.number as ToolLanguage];
      expect(grammar).not.toBeNull();
      expect(lowlight.registered(grammar as string)).toBe(true);
    }
  });

  test("every scope a registered grammar emits has an ink", () => {
    // A grammar added without extending the palette renders most of its tokens in the
    // block's own ink, which reads as "highlighting is broken" rather than as a choice.
    for (const [grammar, source] of Object.entries(SAMPLES)) {
      for (const scope of scopesIn(grammar, source)) {
        expect(TOKEN_INK).toHaveProperty(scope);
      }
    }
  });

  test("every registered grammar has a sample here", () => {
    // Otherwise the check above passes by not looking.
    for (const grammar of Object.values(GRAMMAR_BY_LANGUAGE)) {
      if (grammar !== null) expect(SAMPLES).toHaveProperty(grammar);
    }
  });

  test("agent-authored text never becomes markup", () => {
    // The text is arbitrary output from an untrusted source; it reaches React as
    // elements, never as a string interpolated into HTML.
    const hostile = "<img src=x onerror=alert(1)> </pre><script>x()</script>";
    for (const language of [ToolLanguage.UNSPECIFIED, ToolLanguage.SHELL]) {
      const markup = renderToStaticMarkup(
        <ToolText text={hostile} language={language} />,
      );
      expect(markup).not.toContain("<img");
      expect(markup).not.toContain("<script");
    }
  });

  test("an unspecified language renders the text as it stands", () => {
    const markup = renderToStaticMarkup(
      <ToolText text="echo hello" language={ToolLanguage.UNSPECIFIED} />,
    );
    expect(markup).toBe("echo hello");
  });

  test("a language this build predates renders unlit rather than throwing", () => {
    // A tab polling on its old bundle is handed whatever the deployed BFF projects, so an
    // enum value added upstream reaches this component before the mapping does.
    const ahead = (Math.max(
      ...ToolLanguageSchema.values.map((value) => value.number),
    ) + 1) as ToolLanguage;
    const markup = renderToStaticMarkup(
      <ToolText text="echo hello" language={ahead} />,
    );
    expect(markup).toBe("echo hello");
  });

  test("a specified language marks the text up in the palette's own ink", () => {
    // The class, not just the span: a scope that resolved to no ink would still draw a
    // span, and every token would reach the screen in the block's own colour.
    const markup = renderToStaticMarkup(
      <ToolText text="echo 'hello'" language={ToolLanguage.SHELL} />,
    );
    expect(markup).toContain(`class="${TOKEN_INK["hljs-built_in"]}"`);
    expect(markup).toContain(`class="${TOKEN_INK["hljs-string"]}"`);
    expect(markup).toContain("hello");
  });
});
