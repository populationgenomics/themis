"""An agent as the repo declares it, applied to the Managed Agents control plane.

An agent — its model, system prompt, tools, skills and roster — is a control-plane resource, and
`agents/<name>.agent.yaml` is its declaration: `apply` sends every field the declaration can carry,
so what the yaml does not carry is not on the agent. A custom skill the repo holds is declared by its
directory under `agents/skills/`. `apply` reads the directory, publishes it as a new skill version
when its content is not what the agent runs, and pins the agent to that version; the agent's own
`metadata` records the version, the digest of the content behind it, and the commit holding that
content, so the next apply can tell. `apply` publishes only a directory `HEAD` holds as it stands, so
it changes the skill and the agent only when a commit did.

    uv run --group agents python -m tools.agents apply agents/svcv4-classifier.agent.yaml --agent-id agent_…

Credentials are the SDK's own resolution: the `ant` CLI's login profile on a laptop, the workload
identity federation variables in CI (`docs/runbooks/claude-api-wif.md`).
"""
