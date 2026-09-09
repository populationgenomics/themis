import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle (server.js + a traced minimal node_modules) so the
  // Cloud Run image stays small. Built and served on Bun; see ./Dockerfile.
  output: "standalone",
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
