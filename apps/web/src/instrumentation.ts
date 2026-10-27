// Next.js calls `register` once, before the server handles a request, in every runtime it starts. The trace
// pipeline runs on Node's APIs, so only the Node.js runtime loads it.
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { registerTracing } = await import("./server/tracing/register");
    await registerTracing(process.env);
  }
}
