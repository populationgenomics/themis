// Next.js calls `register` once, before the server handles a request, in every runtime it starts. The trace
// pipeline and the warm start run on Node's APIs, so only the Node.js runtime loads them.
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { registerTracing } = await import("./server/tracing/register");
    await registerTracing(process.env);
    const { selectedBackend } = await import("./server/backend");
    if (selectedBackend(process.env) === "live") {
      const { warmLiveBackend } = await import("./server/warm");
      // Not awaited; called before `register` returns, so the identity it builds is the one every
      // request gets.
      void warmLiveBackend(process.env);
    }
  }
}
