import { describe, expect, test } from "bun:test";
import { fromJson } from "@bufbuild/protobuf";
import { renderToStaticMarkup } from "react-dom/server";
import { ConversationEventSchema } from "@/models/workbench";
import { StreamItem } from "./stream-item";

describe("a stream line", () => {
  test("a kind this build predates draws neutrally rather than throwing", () => {
    // A tab polling on its old bundle through a deploy that added a variant: the
    // client's JSON parse drops the unknown member, leaving the kind unset.
    const event = fromJson(
      ConversationEventSchema,
      { id: "sevt_ahead", thinking: { text: "…" } },
      { ignoreUnknownFields: true },
    );
    expect(event.kind.case).toBeUndefined();
    const markup = renderToStaticMarkup(
      <StreamItem event={event} card={() => null} />,
    );
    expect(markup).toContain("reload to see it");
  });
});
