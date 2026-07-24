# Runbook: self-hosted sandbox

> **Superseded (pending rewrite).** This runbook describes the pre-postern `ant`-worker + credential-proxy architecture,
> which [`postern-sandbox-swap.md`](../plans/postern-sandbox-swap.md) replaces (single `EnvironmentWorker` container;
> the agent container and its `prompt.md` no longer exist). Do not follow the container/proxy steps below until this is
> rewritten for the worker model.

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

1. **Create the probe agent** — the config is `agents/sandbox-probe.agent.yaml`: `agent_toolset_20260401` with the
   prebuilt bash disabled (file tools + `web_search`/`web_fetch` enabled, default `always_allow`) plus a custom `shell`
   tool carrying a model-stated `intent`, no `mcp_toolset`/`mcp_servers`, and a system prompt that teaches the
   `/workspace/document.md` contract path, the code-mode service calls, and the working-document linter. Create it in
   the workspace that owns the environment — the agent carries no environment; a session binds the two. `create` takes
   flags, not YAML, so generate them from the config rather than retyping it (the beta CLI's flags drift — check
   `--help` if it rejects one):

   ```sh
   eval "ant beta:agents create $(uv run --with pyyaml python - agents/sandbox-probe.agent.yaml <<'PY'
   import json, shlex, sys, yaml

   config = yaml.safe_load(open(sys.argv[1]))
   args = ['--name', config['name'], '--model', config['model'], '--system', config['system']]
   for tool in config['tools']:
       args += ['--tool', json.dumps(tool)]
   print(' '.join(shlex.quote(arg) for arg in args))
   PY
   )"
   ```

   Record the returned `agent_…` id as `pulumi config set themis:anthropicAgentId "$AGENT_ID"`; the BFF reads it for
   `sessions.create({agent, environment_id})`.

## Project registry and membership

`projects` and `project_members` have no writer: the web SA holds `SELECT` only, and both are owned by the migrator DB
role, so rows go in by hand. Authorization is default-deny — without a `project_members` row a user who cleared the IAP
gate sees no Projects and cannot create an Analysis, so seed before the smoke. Member emails are PII: database only,
never this repo.

The instance refuses direct connections (empty `authorizedNetworks`) and a personal identity has no DB login, so reach
it through the connector as the deploy SA (`gcloud components install cloud-sql-proxy` — the v2 binary, which carries
`--auto-iam-authn`):

```sh
cloud-sql-proxy --auto-iam-authn \
  --impersonate-service-account=themis-deploy@cpg-themis-dev.iam.gserviceaccount.com \
  cpg-themis-dev:australia-southeast1:themis-sql &   # the sql_connection_name output

psql "host=127.0.0.1 dbname=themis user=themis-deploy@cpg-themis-dev.iam" <<'SQL'
INSERT INTO projects (id, name) VALUES ('demo', 'Demo project')
    ON CONFLICT (id) DO NOTHING;
INSERT INTO project_members (project_id, user_email) VALUES ('demo', 'someone@populationgenomics.org.au')
    ON CONFLICT DO NOTHING;
SQL
```

`user_email` must match the email in the IAP assertion (the signed-in Google account), and the Project must be
registered before a membership references it (FK).

## Operations

- **End-to-end smoke** — with a seeded Project and membership (above), create one Analysis (BFF `POST /api/analyses`)
  whose first `user.message` is the user prompt that drives both data-plane legs: a `hello` code-mode call plus a
  `/workspace/document.md` write embedding the returned `greeting`, `analysis_id`, and `project_id`. A well-formed
  document (one `#` title, non-empty) carrying the binding ids the session token resolved to is the proof signal that
  the forward leg and working-document persistence both work.

- **Queue-depth / liveness alert** — off `work.stats` (org API key, run from ops tooling, never a worker). `depth`
  growing while `workers_polling == 0` is the signature of a silently auto-disabled webhook endpoint (~20 consecutive
  failed deliveries) — re-enable it in the Console.

- **Env-key rotation** — regenerate in Console + `pulumi config set`; Anthropic cannot fast-revoke a leaked key.

- **Updating the agent** — edit `agents/sandbox-probe.agent.yaml`, then push it as a new agent version. The id is
  stable, so no stack config changes:

  ```sh
  AGENT_ID=$(cd infra && pulumi config get themis:anthropicAgentId)
  VERSION=$(ant --format json --transform version --raw-output beta:agents retrieve --agent-id "$AGENT_ID")
  SYSTEM=$(uv run --with pyyaml python -c \
    "import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))['system'], end='')" \
    agents/sandbox-probe.agent.yaml)

  ant beta:agents update --agent-id "$AGENT_ID" --version "$VERSION" --system "$SYSTEM"
  ```

  `update` is a partial patch — an omitted field is preserved, while `--tool`/`--skill`/`--mcp-server` replace wholesale
  when passed, so a prompt-only change sends `--system` alone. `--version` must equal the server's current (it returns
  the new one). A session pins the agent version at creation, so a running Analysis keeps the old prompt: create a fresh
  one to exercise the change.

- **Replacing the agent** — only when the superseded agent must stay separately addressable: create it from the config,
  `pulumi config set themis:anthropicAgentId` to the new id, then archive the old one last (live sessions hold it).
