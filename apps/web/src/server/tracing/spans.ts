import {
  type Attributes,
  type Span,
  SpanKind,
  SpanStatusCode,
  trace,
} from "@opentelemetry/api";

// The BFF's own spans: one around each round trip it makes to a backend, a child of the request's span.
// Every span goes to the process's tracer provider, which `register.ts` installs for a live deployment;
// with none installed a span is a no-op.

export const tracer = trace.getTracer("themis-web");

/** Mark `span` as the failure `error` ended it with. */
export function recordFailure(span: Span, error: unknown): void {
  span.recordException(error instanceof Error ? error : String(error));
  span.setStatus({
    code: SpanStatusCode.ERROR,
    message: error instanceof Error ? error.message : String(error),
  });
}

/** Run `operation` inside a client span named `name`, the current span while it runs. */
export async function clientSpan<T>(
  name: string,
  attributes: Attributes,
  operation: () => Promise<T>,
): Promise<T> {
  return tracer.startActiveSpan(
    name,
    { kind: SpanKind.CLIENT, attributes },
    async (span) => {
      try {
        return await operation();
      } catch (error) {
        recordFailure(span, error);
        throw error;
      } finally {
        span.end();
      }
    },
  );
}
