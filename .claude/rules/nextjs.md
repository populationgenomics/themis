---
paths: apps/web/**/*
---

`apps/web` is the Next.js (App Router) web surface. It runs on the **Bun** runtime with **Biome** for lint/format and
**Tailwind + shadcn/ui** for UI. Use `bun` / `bunx` (not npm/pnpm/yarn) and `biome check` / `biome format` (not
ESLint/Prettier) when working there.

It is pinned to a Next.js major that diverges from training data — APIs, conventions, and file layout may differ, and
features deprecate quickly. Before writing or modifying Next.js/React code, read the bundled guide under
`apps/web/node_modules/next/dist/docs/` and heed deprecation notices rather than relying on recalled patterns.

## Browser storage

Client-side persistence goes in **IndexedDB**, through `apps/web/src/lib/browser-store.ts`, not Web Storage
(`localStorage` / `sessionStorage`). Web Storage blocks the main thread on every read and write, is unreachable from
workers and service workers, holds strings only — so every record needs a hand-written serializer and parser — and
shares one ~5 MB origin cap across all its keys, at which point unrelated writes start failing. IndexedDB is
asynchronous, reachable from workers, structured-clones a record as it is, and its per-origin quota is a share of the
disk rather than a fixed few megabytes. Reach for it even where today's value is small: the store that outgrows Web
Storage has to be migrated with its data already in it.

The module owns the one connection and the request/event API; persist through it rather than opening IndexedDB from a
component. Its contract is best-effort: a blocked or denied store — private mode, storage disabled, another tab holding
an older version — reads as absent and drops the write, resolving rather than rejecting into the render path. So it
holds UI preferences a default can stand in for, never load-bearing state.

Because reads are async they resolve after mount: render the default, then replace it when the read lands. Validate what
comes back — a read returns `unknown`, and a persisted record is untrusted input whatever wrote it — and let a malformed
record leave the default standing.

The exception is a value the first paint needs before it can render — a theme, say. No main-thread API can wait for an
IndexedDB read: `await` yields to the event loop rather than blocking, so the value lands a task later, after the paint.
That value goes in a **cookie**: it is scoped to a domain and rides every request to it, so the server has it
synchronously when it renders the markup, and either side can set it. The same properties bound what belongs there — a
cookie costs bytes on every request and is capped near 4 KB, so it carries only what the server needs at render time and
is never a general store.
