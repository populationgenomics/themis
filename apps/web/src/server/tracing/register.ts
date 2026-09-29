import { DiagConsoleLogger, DiagLogLevel, diag } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";
import { NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { type AuthClient, GoogleAuth } from "google-auth-library";
import { selectedBackend } from "../backend";
import { TraceIdRatioSampler } from "./sampler";

// The web server's trace pipeline: OTLP over HTTP to Google Cloud's Telemetry API, read in Cloud Trace, the
// same endpoint the backend services export to (`themis/telemetry/tracing.py`). Next.js opens a server span
// for each request, continuing the `traceparent` Cloud Run populates, so the BFF's spans join the trace Cloud
// Run's request log names. Each outgoing call carries the trace on (`connect.ts`), for a backend service that
// traces to continue.
//
// The live backend is traced at the ratio THEMIS_TRACE_SAMPLE_RATIO names; the fixture backend never reads
// it and reaches no cloud.

type EnvLike = Record<string, string | undefined>;

export const SAMPLE_RATIO_VAR = "THEMIS_TRACE_SAMPLE_RATIO";
/** The Cloud Run service's name, the one Cloud Trace lists the spans under. */
const SERVICE_NAME = "themis-web";
const TRACES_URL = "https://telemetry.googleapis.com/v1/traces";
const SCOPES = ["https://www.googleapis.com/auth/cloud-platform"];
const DECIMAL = /^\s*[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?\s*$/i;

/** The fraction of traces the server records. Unset, empty, or not a number from 0 to 1 is a
 *  misconfiguration. */
export function sampleRatio(env: EnvLike): number {
  const raw = env[SAMPLE_RATIO_VAR];
  if (raw === undefined || raw.trim() === "") {
    throw new Error(
      `${SAMPLE_RATIO_VAR} is required: the fraction of traces this service records, 0 to 1`,
    );
  }
  // A decimal number, as Python's `float` reads it on the backend services; `Number` alone also takes hex.
  const ratio = DECIMAL.test(raw) ? Number(raw) : Number.NaN;
  if (!(Number.isFinite(ratio) && ratio >= 0 && ratio <= 1)) {
    throw new Error(
      `${SAMPLE_RATIO_VAR} must be a number from 0 to 1, got ${JSON.stringify(raw)}`,
    );
  }
  return ratio;
}

/** The Application Default Credentials' headers for one export. The exporter requires a headers factory
 *  that never throws, so a failure is logged and the export goes out unauthenticated, to be refused and
 *  logged in turn. */
async function authorization(
  client: AuthClient,
): Promise<Record<string, string>> {
  try {
    return Object.fromEntries((await client.getRequestHeaders()).entries());
  } catch (error) {
    diag.error("no credentials for the trace export", error);
    return {};
  }
}

/** Install the process's tracer provider for a live deployment, or nothing: at a ratio of 0, and for the
 *  fixture backend, every span is a no-op. Throws on a malformed ratio and when the Application Default
 *  Credentials name no project, which fails the server's start. */
export async function registerTracing(env: EnvLike): Promise<void> {
  if (selectedBackend(env) !== "live") return;
  const ratio = sampleRatio(env);
  if (ratio === 0) return;
  // The SDK reports a failed export through its diagnostic logger, which is silent until one is set.
  diag.setLogger(new DiagConsoleLogger(), DiagLogLevel.WARN);
  const auth = new GoogleAuth({ scopes: SCOPES });
  const project = await auth.getProjectId();
  const client = await auth.getClient();
  const provider = new NodeTracerProvider({
    resource: resourceFromAttributes({
      "service.name": SERVICE_NAME,
      "gcp.project_id": project,
    }),
    sampler: new TraceIdRatioSampler(ratio),
    spanProcessors: [
      new BatchSpanProcessor(
        new OTLPTraceExporter({
          url: TRACES_URL,
          headers: () => authorization(client),
        }),
      ),
    ],
  });
  provider.register();
}
