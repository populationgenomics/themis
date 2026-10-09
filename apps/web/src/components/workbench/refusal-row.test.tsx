import { describe, expect, test } from "bun:test";
import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import { RefusalSchema } from "@/models/workbench";
import { RefusalRow } from "./refusal-row";

const row = (init: MessageInitShape<typeof RefusalSchema>) =>
  renderToStaticMarkup(<RefusalRow refusal={create(RefusalSchema, init)} />);

const POLICY_URL =
  "https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback";

describe("a refused turn", () => {
  test("states that it was refused, under which category, and why", () => {
    const markup = row({
      category: "cyber",
      explanation: `Blocked under Anthropic's Usage Policy. See ${POLICY_URL}.`,
    });
    expect(markup).toContain(">refused<");
    expect(markup).toContain(">cyber<");
    // Plain text, so the policy link survives where the markdown surface drops it.
    expect(markup).toContain(POLICY_URL);
    expect(markup).not.toContain("<a");
  });

  test("represents a category and an explanation upstream left out", () => {
    const markup = row({});
    expect(markup).toContain(">refused<");
    expect(markup).toContain("no policy category given");
    expect(markup).toContain("no explanation given");
  });
});
