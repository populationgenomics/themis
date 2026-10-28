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
   Console), matching `agents/selfhosted.environment.yaml`, in the environment's own Anthropic workspace
   (`themis-<env>`, [`claude-api-wif.md`](claude-api-wif.md)): environments, agents and sessions all belong to a
   workspace, and the agent's sessions can only bind an environment in theirs. Name it `themis-self-hosted-<env>`. Note
   the `env_…` id.

1. **Generate the environment key** (Console-only) — the `sk-ant-oat01-…` worker credential. Name it after its holder
   and the month it was issued (`themis-dispatcher-2026-10`): the dispatcher reads it and hands it to every sandbox job,
   and the date tells the old key from the new during a rotation. Set it as the encrypted stack config the dispatcher
   reads with `pulumi config set --secret themis:anthropicEnvironmentKey`, which prompts for the value and so keeps it
   out of shell history. Cannot be fast-revoked, so it never reaches the agent container (§7).

1. **Generate the webhook signing key** — register a webhook endpoint (Console) in the same workspace, subscribed to
   **`session.status_run_started` only**, pointing at the `dispatcher_webhook_url` stack output
   (`https://<domain>/webhooks/anthropic`). Store its `whsec_…` key with
   `pulumi config set --secret themis:anthropicWebhookSigningKey`.

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

## Webhook source allowlist

Anthropic's deliveries reach the dispatcher through the web load balancer, whose Cloud Armor policy on the dispatcher's
backend admits Anthropic's published outbound range and denies every other source (`DispatcherService` in
`infra/themis_infra/sandbox.py`). The deny is a catch-all rule above the policy's default rule, because Cloud Armor
refuses preview on the default rule, and it has a switch, `enforce_source_range` in `infra/__main__.py`. Off, the
catch-all runs in Cloud Armor preview: the load-balancer log records each request it would deny, and the default rule
lets every request through to the dispatcher. The switch is on: dev's load-balancer log confirmed that every delivery
comes from the range, which the dispatcher's own Cloud Run request logs could not show, since they record every
delivery's `remoteIp` as `0.0.0.0`. Those logs do record the user agent, `Claude-Platform`, which the queries below
select deliveries by.

The switch is one value for every stack, since the range belongs to Anthropic rather than to an environment. The
cut-over below confirms it once, on dev. An environment brought up after the switch is on starts enforced, and its
webhook is registered at the load-balancer URL from the start (step 3 of the one-time setup).

Moving dev's webhook from the dispatcher's run.app URL onto the load balancer:

1. **Deploy with `enforce_source_range=False`.** From then on the run.app URL refuses requests from the internet, so
   deliveries to the old endpoint URL fail. Anthropic retries them and the work waits in the queue, but it disables an
   endpoint after ~20 consecutive failures, so replace the endpoint straight away.

1. **Replace the endpoint.** The Console cannot change an endpoint's URL, so create a new endpoint at the
   `dispatcher_webhook_url` output (`cd infra && pulumi stack output --stack dev dispatcher_webhook_url`), subscribed to
   `session.status_run_started` only, and delete the old one. The new endpoint has its own `whsec_…` key, which the
   dispatcher rejects until it is stored and deployed. Copy it to the clipboard and store it without it ever reaching
   the terminal, then deploy:

   ```sh
   cd infra && pbpaste | pulumi config set --secret --stack dev themis:anthropicWebhookSigningKey
   ```

1. **Check the route from outside the range.** An unsigned request to the new URL gets the dispatcher's own 401, since
   the deny rule only previews; the same request to the old run.app URL gets Cloud Run's 404 and never reaches the
   dispatcher:

   ```sh
   curl -s -o /dev/null -w '%{http_code}\n' -X POST "$(cd infra && pulumi stack output --stack dev dispatcher_webhook_url)"
   ```

   The curl's own request should then show up in the second query below, under your address. A 403 instead of the 401
   means the catch-all is enforcing, so check that the switch is still off in the deployed program.

1. **Confirm deliveries come from the range.** Create an Analysis so Anthropic sends a delivery. This query lists the
   deliveries that reached the dispatcher's backend. It has to return at least one row, or the checks after it prove
   nothing. Every row should show a `remoteIp` inside the range, status 204, and priority 1000 (the rule admitting
   Anthropic's range) in the enforced column, with the preview column empty:

   ```sh
   gcloud logging read --project cpg-themis-dev --freshness 1d \
     'resource.type="http_load_balancer" AND resource.labels.backend_service_name:"themis-dispatcher-backend" AND httpRequest.userAgent:"Claude-Platform"' \
     --format 'table(timestamp, httpRequest.remoteIp, httpRequest.status, jsonPayload.enforcedSecurityPolicy.priority, jsonPayload.previewSecurityPolicy.priority)'
   ```

   This one lists every request the deny rule would have refused. Scanners and your curl belong here; a
   `Claude-Platform` user agent never should:

   ```sh
   gcloud logging read --project cpg-themis-dev --freshness 1d \
     'resource.type="http_load_balancer" AND resource.labels.backend_service_name:"themis-dispatcher-backend" AND jsonPayload.previewSecurityPolicy.priority=2000' \
     --format 'table(timestamp, httpRequest.remoteIp, httpRequest.userAgent, httpRequest.status)'
   ```

1. **Let deliveries accumulate** over a few days of normal use, so they come from more than one Anthropic address, and
   rerun both queries with `--freshness` covering the whole window. A `Claude-Platform` row in the second query, or one
   outside the range in the first, means the published range does not cover Anthropic's webhook senders: leave the
   switch off and take it up with Anthropic.

1. **Enforce.** Set `enforce_source_range=True` and deploy. The curl from step 3 now gets Cloud Armor's 403.

If deliveries start failing once enforced, set the switch back to `False` and deploy. Anthropic retries the deliveries
Cloud Armor refused in the meantime, unless ~20 consecutive failures have disabled the endpoint, in which case re-enable
it in the Console.

## Projects and members

Which Projects exist, and who works on each, comes from the deployment's ACL, which the site writes
([`access-control.md`](access-control.md)). A person is not admitted and cannot create an Analysis until the ACL lists
them as a manager or curator of a Project, so ask the site for a Project before the smoke test below. The ACL names
people, so its contents never enter this repo.

## Operations

- **End-to-end smoke** — with a Project you are a member of (above), create one Analysis (BFF
  `POST /api/rpc/themis.workbench.rpc.Workbench/CreateAnalysis`) whose first `user.message` is the user prompt that
  drives both data-plane legs: a `hello` code-mode call plus a `/workspace/working_document.md` write embedding the
  returned `greeting`, `analysis_id`, and `project_id`. A well-formed document (one `#` title, non-empty) carrying the
  binding ids the session token resolved to is the proof signal that the forward leg and working-document persistence
  both work.

- **Queue-depth / liveness alert** — off `work.stats` (org API key, run from ops tooling, never a worker). `depth`
  growing while `workers_polling == 0` is the signature of a silently auto-disabled webhook endpoint (~20 consecutive
  failed deliveries) — re-enable it in the Console.

- **Env-key rotation** — regenerate in Console + `pulumi config set`; Anthropic cannot fast-revoke a leaked key.

- **Updating the agent** — edit its yaml and deploy: `deploy.yml` applies the declaration after the stack, so pushing
  `deployed/dev` is how a change reaches the agent, and the guest image the skill relies on lands first. The declaration
  is sent whole, so what the yaml does not carry is not on the agent; the API creates a new agent version only when a
  field changed, and `apply` prints the version before and after. The same command runs by hand when a change should not
  wait for a deploy:

  ```sh
  export THEMIS_AGENT_MODEL_ID=$(cd infra && pulumi config get --stack dev themis:anthropicAgentModelId)
  AGENT_ID=$(cd infra && pulumi config get --stack dev themis:anthropicAgentId)
  uv run --group agents python -m tools.agents diff agents/svcv4-classifier.agent.yaml --agent-id "$AGENT_ID"   # read-only
  uv run --group agents python -m tools.agents apply agents/svcv4-classifier.agent.yaml --agent-id "$AGENT_ID"
  ```

  A session pins the agent version at creation, so a running Analysis keeps the old prompt: create a fresh one to
  exercise the change.

- **Updating a custom skill** — edit its files under `agents/skills/<directory>`, commit them, and deploy, or apply by
  hand as above. `apply` publishes the directory as a new skill version when its content is not what the agent runs, and
  pins the agent to that version. It refuses a directory with uncommitted or untracked changes, because the agent's
  record names the commit the content came from. The record sits in the agent's `metadata` (`skill:<directory>` → the
  version, a digest of the content, and the commit), so a deploy that changes no skill publishes nothing. The next
  session's run record is the proof: its `ran_skills` names the version each skill resolved to
  (`custom:<skill_id>@<version>`).

- **Adding a custom skill** — write its files under a new `agents/skills/<directory>`, then
  `uv run --group agents python -m tools.agents create-skill agents/skills/<directory>`: it creates the skill with the
  directory as its first version and prints the `skill_…` id, which goes into the agent's yaml as a `custom` entry with
  `directory: <directory>`. Then apply the agent.

- **Replacing the agent** — only when the superseded agent must stay separately addressable: create it from the config,
  `pulumi config set themis:anthropicAgentId` to the new id, then archive the old one last (live sessions hold it).
