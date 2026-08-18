import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { absoluteTime, timeAgo } from "@/lib/format";
import { ReaderTime } from "./reader-time";

// The reader-clock render is effect-driven and DOM-bound (`format.test.ts` covers what it renders).
// What is testable here is the property hydration depends on: the markup the server produces is the
// component's own first render, so there is nothing to reconcile.

const ISO = "2026-08-06T04:49:00Z";

describe("the instant a card carries before it mounts", () => {
  test("the markup is the pinned render, not a reader-clock one", () => {
    const html = renderToStaticMarkup(
      <ReaderTime
        iso={ISO}
        pinnedLabel={timeAgo(ISO, Date.parse("2026-08-06T07:49:00Z"))}
        pinnedTitle={absoluteTime(ISO)}
      />,
    );
    expect(html).toContain("3 h ago");
    expect(html).toContain("UTC");
  });

  test("the machine-readable instant is in the markup, whatever is shown", () => {
    // What a screen reader gets rather than a rounded label. React serializes the attribute name as
    // `dateTime`, which HTML parses case-insensitively, so match it that way.
    const html = renderToStaticMarkup(
      <ReaderTime iso={ISO} pinnedLabel="just now" pinnedTitle="whenever" />,
    );
    expect(html.toLowerCase()).toContain(`datetime="${ISO.toLowerCase()}"`);
  });
});
