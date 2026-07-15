# Runbook: self-hosted sandbox

Manual steps around the Pulumi-managed sandbox (self-hosted-sandbox.md). The infra (`infra/themis_infra/sandbox.py`,
`hello.py`) and images (`deploy.yml`) deploy on merge to `main`; the steps below are the Console-only and validation
actions Pulumi cannot do.

## One-time setup (per environment)

1. **Create the self-hosted environment** — `ant beta:environments create --config '{"type": "self_hosted"}'` (or
   Console), matching `agents/selfhosted.environment.yaml`. Note the `env_…` id.
1. **Generate the environment key** (Console-only) — the `sk-ant-oat01-…` worker credential. Set it as the encrypted
   stack config the dispatcher reads: `pulumi config set --secret themis:anthropicEnvironmentKey "$KEY"`. Cannot be
   fast-revoked, so it never reaches the agent container (§7).
1. **Generate the webhook signing key** — register a webhook endpoint (Console) subscribed to
   **`session.status_run_started` only**, pointing at the dispatcher URL (`dispatcher_url` output)
   `/webhooks/anthropic`. Store its `whsec_…` key:
   `pulumi config set --secret themis:anthropicWebhookSigningKey "$WHSEC"`.
1. **Set the environment id** — `pulumi config set themis:anthropicEnvironmentId "$ENV_ID"`.
1. **Configure the agent** — drop `mcp_toolset`/`mcp_servers`; enable `bash`, the file tools, and
   `web_search`/`web_fetch` (default `always_allow`, no custom tools); the system prompt teaches the working-document
   contract path (`/workspace/document.md`) and includes the `themis/agent/prompt.md` fragment (available services + the
   working-document linter).

## Operations

- **Queue-depth / liveness alert** — off `work.stats` (org API key, run from ops tooling, never a worker). `depth`
  growing while `workers_polling == 0` is the signature of a silently auto-disabled webhook endpoint (~20 consecutive
  failed deliveries) — re-enable it in the Console.
- **Env-key rotation** — regenerate in Console + `pulumi config set`; Anthropic cannot fast-revoke a leaked key.
