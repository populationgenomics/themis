import { describe, expect, test } from "bun:test";
import { create } from "@bufbuild/protobuf";
import {
  Code,
  ConnectError,
  createClient,
  createRouterTransport,
} from "@connectrpc/connect";
import { SpanKind, SpanStatusCode, trace } from "@opentelemetry/api";
import type { ReadableSpan } from "@opentelemetry/sdk-trace-base";
import { Literature, PaperInfoSchema } from "@/gen/themis/rpc/literature_pb";
import { tracingInterceptor } from "./connect";
import { recordedSpans } from "./recorded-spans";

const PATH = "/themis.rpc.literature.Literature/DescribePaper";

/** A literature client over an in-memory server answering DescribePaper with `answer`; the headers each
 *  call arrived with are collected. */
function client(answer: () => void) {
  const received: Headers[] = [];
  const transport = createRouterTransport(
    ({ service }) => {
      service(Literature, {
        describePaper(_request, context) {
          received.push(context.requestHeader);
          answer();
          return create(PaperInfoSchema, {});
        },
      });
    },
    { transport: { interceptors: [tracingInterceptor] } },
  );
  return { client: createClient(Literature, transport), received };
}

/** Call DescribePaper inside a `request` span, as the BFF does inside Next.js's. */
async function describeInsideARequest(
  literature: ReturnType<typeof client>["client"],
) {
  await trace.getTracer("test").startActiveSpan("request", async (span) => {
    try {
      await literature.describePaper({ docId: "d" });
    } finally {
      span.end();
    }
  });
}

function named(spans: ReadableSpan[], name: string): ReadableSpan {
  const matching = spans.filter((s) => s.name === name);
  expect(matching).toHaveLength(1);
  return matching[0];
}

describe("an outgoing Connect call", () => {
  test("is a client span of the request, and carries it in traceparent", async () => {
    const spans = recordedSpans();
    const { client: literature, received } = client(() => {});

    await describeInsideARequest(literature);

    const finished = spans.getFinishedSpans();
    const request = named(finished, "request");
    const call = named(finished, PATH);
    expect(call.kind).toBe(SpanKind.CLIENT);
    expect(call.parentSpanContext?.spanId).toBe(request.spanContext().spanId);
    expect(call.attributes["rpc.grpc.status_code"]).toBe(0);
    const { traceId, spanId } = call.spanContext();
    expect(received.map((h) => h.get("traceparent"))).toEqual([
      `00-${traceId}-${spanId}-01`,
    ]);
  });

  test("that fails records the status it failed with", async () => {
    const spans = recordedSpans();
    const { client: literature } = client(() => {
      throw new ConnectError("no such paper", Code.NotFound);
    });

    await expect(describeInsideARequest(literature)).rejects.toThrow(
      "no such paper",
    );

    const call = named(spans.getFinishedSpans(), PATH);
    expect(call.status.code).toBe(SpanStatusCode.ERROR);
    expect(call.attributes["rpc.grpc.status_code"]).toBe(Code.NotFound);
  });
});
