import { type Context, trace } from "@opentelemetry/api";
import {
  type Sampler,
  SamplingDecision,
  type SamplingResult,
} from "@opentelemetry/sdk-trace-base";

// The sampling decision every Themis service makes, so a trace is recorded whole or not at all: a trace is
// recorded when its id's low 64 bits fall under the ratio times 2^64. It is the rule of the Python SDK's
// `TraceIdRatioBased`, which the backend services sample with (`themis/telemetry/tracing.py`), and it reads
// the trace id alone. The parent's sampled flag is Cloud Run's decision, not the caller's: Cloud Run puts a
// span of its own in front of every service and samples those spans at no more than 0.1 requests per second
// per instance.

const TWO_TO_THE_64 = 2 ** 64;

/** `x` rounded to an integer with ties to even, as Python's `round` does. `x` is a non-negative double. */
function roundHalfEven(x: number): bigint {
  const floor = Math.floor(x);
  const fraction = x - floor;
  if (fraction < 0.5) return BigInt(floor);
  if (fraction > 0.5) return BigInt(floor + 1);
  return BigInt(floor % 2 === 0 ? floor : floor + 1);
}

export class TraceIdRatioSampler implements Sampler {
  private readonly bound: bigint;

  constructor(private readonly ratio: number) {
    if (!(Number.isFinite(ratio) && ratio >= 0 && ratio <= 1)) {
      throw new Error(`a sample ratio is from 0 to 1, got ${ratio}`);
    }
    // `ratio * 2^64` is exact: scaling a double by a power of two loses nothing.
    this.bound = roundHalfEven(ratio * TWO_TO_THE_64);
  }

  shouldSample(context: Context, traceId: string): SamplingResult {
    const low = BigInt(`0x${traceId.slice(16)}`);
    return {
      decision:
        low < this.bound
          ? SamplingDecision.RECORD_AND_SAMPLED
          : SamplingDecision.NOT_RECORD,
      traceState: trace.getSpanContext(context)?.traceState,
    };
  }

  toString(): string {
    return `TraceIdRatioSampler{${this.ratio}}`;
  }
}
