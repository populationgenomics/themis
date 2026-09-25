# Runbook: self-hosted sandbox

> **Superseded (pending rewrite).** This runbook describes the pre-postern `ant`-worker + credential-proxy architecture,
> which [`sandbox-worker.md`](../design/sandbox-worker.md) replaces (single `EnvironmentWorker` container; the agent
> container and its `prompt.md` no longer exist). Do not follow the container/proxy steps below until this is rewritten
> for the worker model.

Manual steps around the Pulumi-managed sandbox (self-hosted-sandbox.md). The infra (`infra/themis_infra/sandbox.py`,
`hello.py`) and images (`deploy.yml`) reach the environment on a deploy — pushing `deployed/<env>`, or dispatching on
`main`; the steps below are the Console-only and validation actions Pulumi cannot do.

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

1. **Create the agent** — `agents/svcv4-classifier.agent.yaml` is the classifier's whole declaration: model, system
   prompt, tools, skills and roster. An agent carries no environment — a session binds the two — so create it in the
   workspace that owns the environment. `tools.agents` reads the declaration and makes the calls. It authenticates
   through the SDK's credential resolution, which after `ant auth login` is the CLI's profile, and takes the model id
   from the environment, since the id is confidential stack config rather than the yaml's:

   ```sh
   export THEMIS_AGENT_MODEL_ID=$(cd infra && pulumi config get --stack dev themis:anthropicAgentModelId)
   uv run --group agents python -m tools.agents create agents/svcv4-classifier.agent.yaml   # publishes its skills; prints the agent id
   ```

   Record the printed `agent_…` id as `pulumi config set --stack dev themis:anthropicAgentId "$AGENT_ID"`; the BFF reads
   it for `sessions.create({agent, environment_id})`.

## Project registry and membership

`projects` and `project_members` are owned by the migrator DB role and the web SA holds `SELECT` only, so rows go in by
hand — as `themis-clu`, which is a member of that role. Authorization is default-deny — without a `project_members` row
a user who cleared the IAP gate sees no Projects and cannot create an Analysis, so seed before the smoke. Member emails
are PII: database only, never this repo.

The instance refuses direct connections (empty `authorizedNetworks`) and a personal identity has no DB login, so reach
it through `tools/psql.py`, which runs the connector and connects as `themis-clu` — the account a person impersonates
for this (`infra/themis_infra/clu.py`):

```sh
uv run python -m tools.psql -- -c "
INSERT INTO projects (id, name) VALUES ('demo', 'Demo project')
    ON CONFLICT (id) DO NOTHING;
INSERT INTO project_members (project_id, user_email) VALUES ('demo', 'someone@populationgenomics.org.au')
    ON CONFLICT DO NOTHING;"
```

`user_email` must match the email in the IAP assertion (the signed-in Google account), and the Project must be
registered before a membership references it (FK).

## Operations

- **End-to-end smoke** — with a seeded Project and membership (above), create one Analysis (BFF
  `POST /api/rpc/themis.workbench.rpc.Workbench/CreateAnalysis`) whose first `user.message` is the user prompt that
  drives both data-plane legs: a `hello` code-mode call plus a `/workspace/working_document.md` write embedding the
  returned `greeting`, `analysis_id`, and `project_id`. A well-formed document (one `#` title, non-empty) carrying the
  binding ids the session token resolved to is the proof signal that the forward leg and working-document persistence
  both work.

- **Queue-depth / liveness alert** — off `work.stats` (org API key, run from ops tooling, never a worker). `depth`
  growing while `workers_polling == 0` is the signature of a silently auto-disabled webhook endpoint (~20 consecutive
  failed deliveries) — re-enable it in the Console.

- **Env-key rotation** — regenerate in Console + `pulumi config set`; Anthropic cannot fast-revoke a leaked key.

- **Updating the agent** — edit its yaml, then apply it. The declaration is sent whole, so what the yaml does not carry
  is not on the agent; the API creates a new agent version only when a field changed, and `apply` prints the version
  before and after:

  ```sh
  export THEMIS_AGENT_MODEL_ID=$(cd infra && pulumi config get --stack dev themis:anthropicAgentModelId)
  AGENT_ID=$(cd infra && pulumi config get --stack dev themis:anthropicAgentId)
  uv run --group agents python -m tools.agents diff agents/svcv4-classifier.agent.yaml --agent-id "$AGENT_ID"   # read-only
  uv run --group agents python -m tools.agents apply agents/svcv4-classifier.agent.yaml --agent-id "$AGENT_ID"
  ```

  A session pins the agent version at creation, so a running Analysis keeps the old prompt: create a fresh one to
  exercise the change.

- **Updating a custom skill** — edit its files under `agents/skills/<directory>`, commit them, and apply the agent, as
  above. `apply` publishes the directory as a new skill version when its content is not what the agent runs, and pins
  the agent to that version. It refuses a directory with uncommitted or untracked changes, because the agent's record
  names the commit the content came from. The record sits in the agent's `metadata` (`skill:<directory>` → the version,
  a digest of the content, and the commit), so an apply that changes no skill publishes nothing. The next session's run
  record is the proof: its `ran_skills` names the version each skill resolved to (`custom:<skill_id>@<version>`).

- **Adding a custom skill** — write its files under a new `agents/skills/<directory>`, then
  `uv run --group agents python -m tools.agents create-skill agents/skills/<directory>`: it creates the skill with the
  directory as its first version and prints the `skill_…` id, which goes into the agent's yaml as a `custom` entry with
  `directory: <directory>`. Then apply the agent.

- **Replacing the agent** — only when the superseded agent must stay separately addressable: create it from the config,
  `pulumi config set themis:anthropicAgentId` to the new id, then archive the old one last (live sessions hold it).
