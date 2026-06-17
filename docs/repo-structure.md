# Repository structure

A polyglot monorepo, grouped by role — each top-level directory has one job. The
layout anticipates growth (a Next.js frontend, a backend/orchestrator, agent
tool + MCP servers, a sandbox worker) without standing up tooling for parts that
do not exist yet.

| Path | Holds |
| --- | --- |
| `apps/` | Deployable application surfaces, one directory per service, each owning its Dockerfile. `apps/web` is the IAP-fronted web surface — a static placeholder today, the Next.js frontend later. Future: `apps/api` (backend/orchestrator on Cloud Run, talks to Postgres), `apps/sandbox-worker`, `apps/<name>-mcp` (tool / MCP servers). |
| `packages/` | Shared libraries imported by more than one app (shared types, API clients, config schemas), TypeScript and Python alike. Added when the first shared module appears. |
| `infra/` | Cloud infrastructure only (Pulumi, Python) — no application code. One stack per environment; images are built in `apps/*` by CI and deployed here. |
| `docs/` | Design docs, runbooks, style guides. Primary audience is a model. |
| `tools/` | Repo tooling (CI screens, scripts) — *not* the agent's tools, which are apps/services. |
| `tests/` | Cross-cutting tests; app-local tests live with their app. |

As the workspaces land: JS apps/packages under a pnpm workspace; Python under uv
(dependency groups / workspace). Nothing here commits to that tooling yet.
