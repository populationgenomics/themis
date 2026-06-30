# web

The IAP-fronted web surface — the Next.js app (UI + BFF) served on Cloud Run as
`themis-web`. See
[`docs/design/frontend-framework.md`](../../docs/design/frontend-framework.md) for the
framework and web-tier design, and [`docs/design/agent-runtime.md`](../../docs/design/agent-runtime.md)
for the session/trace data flow it surfaces.

## Stack

- **Next.js** (App Router) on the **Bun** runtime
- **Biome** for lint + format (`biome check` / `biome format`)
- **Tailwind CSS + shadcn/ui** — copy-in components, added via `shadcn add` into `src/components/ui`
- **TanStack Query** for client data fetching (provider in `src/app/providers.tsx`)

## Layout

| Path | Holds |
| --- | --- |
| `src/app` | App Router routes, root layout, page tree |
| `src/app/api` | BFF route handlers (data API, webhook receiver, session relay — see the design doc) |
| `src/components/ui` | shadcn/ui copy-in components |
| `src/lib` | Shared helpers (`cn`, …) |

## Develop

```bash
bun install
bun dev          # http://localhost:3000
bun run lint     # biome check
bun run typecheck
bun run build    # standalone output
```

## Deploy

CI builds this directory into the `themis/web` image (tagged with the commit SHA) and
`infra`'s Pulumi program points the Cloud Run service at it. The image runs the Next.js
`standalone` server on Bun, listening on Cloud Run's `$PORT`.
