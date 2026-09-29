import { describe, expect, test } from "bun:test";
import { ROOT_CONTEXT, TraceFlags, trace } from "@opentelemetry/api";
import { SamplingDecision } from "@opentelemetry/sdk-trace-base";
import { TraceIdRatioSampler } from "./sampler";

// The backend services sample with the Python SDK's `TraceIdRatioBased`; this sampler has to agree with it
// about every trace, or a trace is recorded in one service and dropped in the next. These are the cases
// `themis/telemetry/tests/test_tracing.py` holds the Python side to.
const DECISIONS: [number, string, boolean][] = [
  [0.5, "ffffffffffffffff7fffffffffffffff", true],
  [0.5, "00000000000000008000000000000000", false],
  [0.25, "4bf92f3577b34da63fffffffffffffff", true],
  [0.25, "4bf92f3577b34da64000000000000000", false],
  [1.0, "ffffffffffffffffffffffffffffffff", true],
  [0.0, "00000000000000000000000000000001", false],
];

function records(ratio: number, traceId: string, parentSampled: boolean) {
  const parent = trace.setSpanContext(ROOT_CONTEXT, {
    traceId,
    spanId: "00f067aa0ba902b7",
    isRemote: true,
    traceFlags: parentSampled ? TraceFlags.SAMPLED : TraceFlags.NONE,
  });
  const { decision } = new TraceIdRatioSampler(ratio).shouldSample(
    parent,
    traceId,
  );
  return decision === SamplingDecision.RECORD_AND_SAMPLED;
}

describe("the sampling decision", () => {
  test.each(DECISIONS)(
    "at %p, trace %s is recorded: %p, whichever way the parent's flag points",
    (ratio, traceId, recorded) => {
      expect(records(ratio, traceId, true)).toBe(recorded);
      expect(records(ratio, traceId, false)).toBe(recorded);
    },
  );

  test.each([-0.1, 1.5, Number.NaN, Number.POSITIVE_INFINITY])(
    "a ratio of %p is refused",
    (ratio) => {
      expect(() => new TraceIdRatioSampler(ratio)).toThrow("from 0 to 1");
    },
  );
});
