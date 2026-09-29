import { describe, expect, test } from "bun:test";
import { SpanKind, SpanStatusCode, trace } from "@opentelemetry/api";
import { recordedSpans } from "./recorded-spans";
import { clientSpan } from "./spans";

describe("a backend round trip", () => {
  test("is a client span, current while it runs, and a child of the span it ran in", async () => {
    const spans = recordedSpans();
    let current: string | undefined;

    await trace.getTracer("test").startActiveSpan("request", async (span) => {
      await clientSpan("kms.macSign", { "kms.key_version": "v1" }, async () => {
        current = trace.getActiveSpan()?.spanContext().spanId;
      });
      span.end();
    });

    const [roundTrip, request] = spans.getFinishedSpans();
    expect(roundTrip.name).toBe("kms.macSign");
    expect(roundTrip.kind).toBe(SpanKind.CLIENT);
    expect(roundTrip.attributes["kms.key_version"]).toBe("v1");
    expect(current).toBe(roundTrip.spanContext().spanId);
    expect(roundTrip.parentSpanContext?.spanId).toBe(
      request.spanContext().spanId,
    );
  });

  test("that fails is recorded as the failure, and the error still reaches the caller", async () => {
    const spans = recordedSpans();

    await expect(
      clientSpan("gcs.download", {}, async () => {
        throw new Error("object gone");
      }),
    ).rejects.toThrow("object gone");

    const [roundTrip] = spans.getFinishedSpans();
    expect(roundTrip.status).toEqual({
      code: SpanStatusCode.ERROR,
      message: "object gone",
    });
    expect(roundTrip.events.map((e) => e.name)).toEqual(["exception"]);
  });
});
