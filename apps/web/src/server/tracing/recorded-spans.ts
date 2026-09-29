import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
} from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";

// For tests: the process's tracer provider, recording every span in memory. OpenTelemetry's provider,
// context manager and propagator are process-global and registered once, so every test file shares
// this one recorder and clears it before it reads.

let exporter: InMemorySpanExporter | undefined;

/** The shared recorder, registered as the process's provider on first use and emptied on every call. */
export function recordedSpans(): InMemorySpanExporter {
  if (exporter === undefined) {
    exporter = new InMemorySpanExporter();
    new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    }).register();
  }
  exporter.reset();
  return exporter;
}
