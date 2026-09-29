import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkDirective from "remark-directive";
import remarkGfm from "remark-gfm";
import grammar from "@/widgets/embed-grammar.test-support.json";
import { remarkDirectives } from "./directives";

// Which `::embed` directives the browser draws out of a document, and each one's path. The cases are
// shared with the guest's linter (themis/document_linter/tests/test_embeds.py), which parses the same
// documents with markdown-it-py: the two have to draw the same embeds and read the same paths, or the
// linter passes a path the browser resolves differently. An `::embed` that does not draw carries
// the problem its placeholder names; the linter reports each such line.

interface Read {
  path: string;
  problem: string;
}

function embedsOf(markdown: string): Read[] {
  const found: Read[] = [];
  const components = {
    "widget-embed": ({
      node,
    }: {
      node?: { properties?: Record<string, unknown> };
    }) => {
      found.push({
        path: String(node?.properties?.path),
        problem: String(node?.properties?.problem),
      });
      return null;
    },
  } as Components;
  renderToStaticMarkup(
    <ReactMarkdown
      remarkPlugins={[
        remarkGfm,
        remarkDirective,
        [remarkDirectives, { citations: false, embeds: true }],
      ]}
      components={components}
    >
      {markdown}
    </ReactMarkdown>,
  );
  return found;
}

function drawn(markdown: string): string[] {
  return embedsOf(markdown)
    .filter((embed) => embed.problem === "")
    .map((embed) => embed.path);
}

/** A deterministic stream of pseudo-random numbers in [0, 1), so a failing label reproduces. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1_103_515_245 + 12_345) % 2_147_483_648;
    return state / 2_147_483_648;
  };
}

// Characters a label may carry that markdown or the directive grammar gives a meaning to.
const ALPHABET = [
  ..."aZ09./-_*`[]\\{}\"'<>:@!#~|()& ",
  "www.",
  "http://",
  "\\]",
  "\\[",
];

describe("the ::embed directive", () => {
  for (const { name, markdown, embeds } of grammar.cases) {
    test(`is read ${name}`, () => {
      expect(drawn(markdown)).toEqual(embeds);
    });
  }

  test("never fails the render, whatever its label, and draws only the label it scanned", () => {
    const next = random(20_260_925);
    for (let run = 0; run < 3_000; run += 1) {
      const length = 1 + Math.floor(next() * 12);
      let label = "";
      for (let i = 0; i < length; i += 1) {
        label += ALPHABET[Math.floor(next() * ALPHABET.length)];
      }
      const markdown = `::embed[${label}]`;
      const read = embedsOf(markdown);
      expect(read.length).toBeLessThanOrEqual(1);
      for (const embed of read.filter((each) => each.problem === "")) {
        expect(markdown).toBe(`::embed[${embed.path}]`);
      }
    }
  });

  test("on a surface that does not draw widgets is its own text", () => {
    const html = renderToStaticMarkup(
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          remarkDirective,
          [remarkDirectives, { citations: true, embeds: false }],
        ]}
      >
        {"before\n\n::embed[assets/a.binpb]\n\nafter"}
      </ReactMarkdown>,
    );
    expect(html).toContain("::embed[assets/a.binpb]");
    expect(html).toContain("after");
  });
});
