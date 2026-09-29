import { ConnectError, type Interceptor } from "@connectrpc/connect";
import {
  context,
  propagation,
  type Span,
  SpanKind,
  trace,
} from "@opentelemetry/api";
import { recordFailure, tracer } from "./spans";

// A client span around each outgoing Connect call, with the call's context propagated in its headers: the
// W3C `traceparent` the backend service continues the trace from. Named like the server span that
// continues it, `/<service>/<method>`.

/** The gRPC status code a call ended with, on the span; `OK` is 0 and Connect's codes are gRPC's. */
const STATUS_CODE = "rpc.grpc.status_code";

const headersSetter = {
  set(carrier: Headers, key: string, value: string): void {
    carrier.set(key, value);
  },
};

function failed(span: Span, error: unknown): void {
  if (error instanceof ConnectError) {
    span.setAttribute(STATUS_CODE, error.code);
  }
  recordFailure(span, error);
  span.end();
}

function succeeded(span: Span): void {
  span.setAttribute(STATUS_CODE, 0);
  span.end();
}

/** `messages`, ending `span` once the stream is drained, fails, or its reader stops early. */
async function* endingWith<T>(
  messages: AsyncIterable<T>,
  span: Span,
): AsyncIterable<T> {
  let failure = false;
  try {
    yield* messages;
  } catch (error) {
    failure = true;
    failed(span, error);
    throw error;
  } finally {
    if (!failure) succeeded(span);
  }
}

export const tracingInterceptor: Interceptor = (next) => async (request) => {
  const service = request.service.typeName;
  const method = request.method.name;
  const span = tracer.startSpan(`/${service}/${method}`, {
    kind: SpanKind.CLIENT,
    attributes: {
      "rpc.system": "grpc",
      "rpc.service": service,
      "rpc.method": method,
    },
  });
  const active = trace.setSpan(context.active(), span);
  propagation.inject(active, request.header, headersSetter);
  let response: Awaited<ReturnType<typeof next>>;
  try {
    response = await context.with(active, () => next(request));
  } catch (error) {
    failed(span, error);
    throw error;
  }
  if (!response.stream) {
    succeeded(span);
    return response;
  }
  return { ...response, message: endingWith(response.message, span) };
};
