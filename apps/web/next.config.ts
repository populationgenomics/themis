import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle (server.js + a traced minimal node_modules) so the
  // Cloud Run image stays small. Built and served on Bun; see ./Dockerfile.
  output: "standalone",
  // No page is framed by another origin: every action runs on ambient IAP identity, so a framed page
  // is an overlay target. The CSP directive is the current control; X-Frame-Options for browsers
  // that read only it.
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          {
            key: "Content-Security-Policy",
            value: "frame-ancestors 'none'",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
