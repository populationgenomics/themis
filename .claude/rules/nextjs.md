---
paths: apps/web/**/*
---

`apps/web` is the Next.js (App Router) web surface. It runs on the **Bun** runtime with **Biome** for lint/format and
**Tailwind + shadcn/ui** for UI. Use `bun` / `bunx` (not npm/pnpm/yarn) and `biome check` / `biome format` (not
ESLint/Prettier) when working there.

It is pinned to a Next.js major that diverges from training data — APIs, conventions, and file layout may differ, and
features deprecate quickly. Before writing or modifying Next.js/React code, read the bundled guide under
`apps/web/node_modules/next/dist/docs/` and heed deprecation notices rather than relying on recalled patterns.
