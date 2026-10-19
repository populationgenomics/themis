import type { NextConfig } from "next";
import { PROXY_BODY_MAX_BYTES } from "./src/server/rpc/limits";

const nextConfig: NextConfig = {
  // Self-contained server bundle (server.js + a traced minimal node_modules) so the
  // Cloud Run image stays small. Built and served on Bun; see ./Dockerfile.
  output: "standalone",
  // Off: `next dev` would otherwise write an AGENTS.md block for a detected coding agent; the same
  // instruction lives in .claude/rules/nextjs.md.
  agentRules: false,
  // The widget browser reads its examples through a runtime path the tracer cannot follow
  // (src/server/widget-examples.ts), so the route names them.
  outputFileTracingIncludes: {
    "/dev/widgets": ["src/widgets/examples/*/*.txtpb"],
  },
  experimental: {
    proxyClientMaxBodySize: PROXY_BODY_MAX_BYTES,
  },
  // X-Frame-Options only, for browsers that read it and not `frame-ancestors`. The policy itself is
  // minted per request (src/lib/csp.ts), and a second Content-Security-Policy header here would be
  // intersected with that one.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [{ key: "X-Frame-Options", value: "DENY" }],
      },
    ];
  },
};

export default nextConfig;
