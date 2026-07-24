---
paths:
  - "agents/**/*"
  - "apps/web/src/server/adapters/real/**/*"
  - "themis/clients/work_queue/**/*"
  - "themis/services/dispatcher/**/*"
  - "themis/services/sandbox_worker/**/*"
---

These directories integrate with the Claude API / Managed Agents: the Python `anthropic` SDK
(`themis/clients/work_queue`, `themis/services/dispatcher`, and `themis/services/sandbox_worker` — the
`anthropic.lib.environments.EnvironmentWorker` loop), the TypeScript `@anthropic-ai/sdk`
(`apps/web/src/server/adapters/real`), and the agent/environment YAML (`agents`).

The SDK surface and the Managed Agents beta diverge from training data and deprecate quickly — model IDs, `thinking`
config, and the `beta.sessions` / `beta.environments` shapes all change. Before writing or modifying code here, load the
`claude-api` skill and verify against the installed SDK source rather than recalled patterns.
