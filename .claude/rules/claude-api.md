---
paths:
  - "agents/**/*"
  - "apps/web/src/server/adapters/real/**/*"
  - "themis/agent/**/*"
  - "themis/clients/work_queue/**/*"
  - "themis/services/dispatcher/**/*"
  - "themis/services/proxy/**/*"
---

These directories integrate with the Claude API / Managed Agents: the Python `anthropic` SDK
(`themis/clients/work_queue`, `themis/services/dispatcher`, `themis/services/proxy`), the TypeScript `@anthropic-ai/sdk`
(`apps/web/src/server/adapters/real`), and the agent/environment YAML plus self-hosted worker driven by the `ant` CLI
(`agents`, `themis/agent`).

The SDK surface and the Managed Agents beta diverge from training data and deprecate quickly — model IDs, `thinking`
config, the `beta.sessions` / `beta.environments` shapes, and the `ant` worker flow all change. Before writing or
modifying code here, load the `claude-api` skill and follow its docs rather than recalled patterns.
