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
  // Loaded from node_modules at run time instead of bundled. Turbopack copies a bundled package into
  // each route group's server chunk and the proxy's, so every group would read and compile it again;
  // an external one a process loads once. Lists the cloud SDKs and the protobuf and Connect runtime
  // the server imports directly; their own dependencies load with them.
  serverExternalPackages: [
    "@anthropic-ai/sdk",
    "@bufbuild/protobuf",
    "@bufbuild/protovalidate",
    "@connectrpc/connect",
    "@connectrpc/connect-node",
    "@connectrpc/validate",
    "@google-cloud/cloud-sql-connector",
    "@google-cloud/kms",
    "@google-cloud/storage",
    "google-auth-library",
  ],
  experimental: {
    proxyClientMaxBodySize: PROXY_BODY_MAX_BYTES,
    // Off: the preload requires every route's module in sequence without yielding to I/O, so the
    // server reads no request, the startup probe's included, until all have loaded. A route's module
    // loads on its first request instead.
    preloadEntriesOnStart: false,
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
