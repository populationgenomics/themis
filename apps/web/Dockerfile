# syntax=docker/dockerfile:1

# Multi-stage build of the Next.js `standalone` server, run on the Bun runtime.
# CI builds this directory into the `themis/web` image; infra points the Cloud Run
# service at it. Cloud Run injects $PORT and expects the container to listen on it —
# Next's standalone server.js honours PORT and HOSTNAME.
FROM oven/bun:1.3 AS base
ENV NEXT_TELEMETRY_DISABLED=1

# deps: install from the committed lockfile (bunfig.toml carries the age gate).
FROM base AS deps
WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
RUN bun install --frozen-lockfile

# build: compile the standalone output.
FROM base AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN bun run build

# runtime: copy only the standalone server, static assets, and public/.
FROM base AS runtime
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build --chown=bun:bun /app/.next/standalone ./
COPY --from=build --chown=bun:bun /app/.next/static ./.next/static
COPY --from=build --chown=bun:bun /app/public ./public
USER bun
# PORT is not set here: Cloud Run injects it at runtime (and server.js defaults to
# 3000 for a local `docker run`). HOSTNAME binds all interfaces, required so the
# Cloud Run / LB front end can reach the server.
ENV HOSTNAME=0.0.0.0
CMD ["bun", "server.js"]
