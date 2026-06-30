### Concern: security

Adapted from claude-code-action's
[security-focused review template](https://github.com/anthropics/claude-code-action/blob/main/docs/solutions.md#security-focused-pr-reviews),
extended with LLM-specific risks and supply-chain pinning.

Rate each finding's severity inline as **CRITICAL**, **HIGH**, **MEDIUM**, or **LOW**. Name the concern, quote the
offending content briefly, and suggest a concrete remediation.

**OWASP-class issues**

- **Injection.** SQL injection (string-built queries, missing parameterisation), command injection (shelling out to user
  input), path traversal, SSRF, server-side template injection, XSS in any HTML/JSX path that interpolates untrusted
  data.
- **Broken authentication / authorisation.** New endpoints, RPCs, or cloud functions that don't authenticate their
  caller, or authenticate but don't authorise (anyone-authenticated reading a resource that should be project-scoped).
  Session/token handling mistakes.
- **Tenant / user / group isolation.** This is a multi-tenant system; per-user (and per-group) data isolation is
  load-bearing. Flag: endpoints that scope to "authenticated user" but don't further scope to "*this user's* resources"
  (e.g. a report-fetch endpoint that returns any report given a known ID); SQL/queries that cross tenant boundaries
  without an explicit owner filter; cache keys not scoped by user/tenant (a shared cache is a side channel); error
  responses or logs that include another user's data; tools/agents given permissions broad enough to read across
  tenants.
- **Sensitive data exposure.** Logging that includes credentials, tokens, or full request/response bodies. Error
  responses that echo back internal state (stack traces, query text). Secrets in configuration files committed to the
  repo, unless encrypted to the bar set under "Hardcoded secrets" — masking doesn't count.
- **Security misconfiguration.** Permissive CORS, debug flags enabled by default in non-debug paths, dev/test endpoints
  reachable in production builds.
- **CSRF / state-changing GETs.** Mutating endpoints that don't verify CSRF tokens or assume same-origin without
  enforcing it.
- **XXE / unsafe XML / deserialisation.** `pickle.loads`, `yaml.load` (without `SafeLoader`), `eval`/`exec` on untrusted
  input, dynamic `import` of attacker-controlled module names.
- **Cryptography.** Use of MD5/SHA-1 for security purposes, ECB mode, hand-rolled crypto, missing IV/nonce uniqueness,
  hardcoded keys, comparing secrets with `==` rather than constant-time compare.
- **Race conditions / TOCTOU.** Check-then-act patterns on files, database rows, or filesystem state without proper
  locking or transactions.

**LLM-specific risks**

- **Prompt injection vectors.** User input flowing unsanitised into a system prompt, tool descriptions, or anywhere the
  model treats the content as instructions.
- **Token consumption.** Patterns that let an external caller consume LLM tokens for tasks unrelated to the intended
  product surface: open-ended chat endpoints, missing rate limits, missing auth on LLM-backed routes.
- **Tool-use scope.** Tools given to a model that have broader permissions than the model needs (e.g. a read-only
  summarisation agent given write access to a database).
- **Rule of Two / lethal trifecta.** Watch for agents or services where *all three* of these intersect: (a) access to
  private or sensitive data; (b) exposure to untrusted content (user input, third-party tool responses, file contents
  the agent reads); (c) external communication (network calls, sending messages, writing to public surfaces). Any *two*
  is usually defensible; all *three* is a lethal trifecta and the output needs aggressive sanitisation and policy
  enforcement. Background: https://ai.meta.com/blog/practical-ai-agent-security/

**Supply chain**

New dependencies (including transitive bumps in lockfiles) should clear all of the following — flag if any are missing:

- **Pinned to a specific version.** No floating ranges.
- **Explicit human review.** The PR introducing the dep is the vetting point; a maintainer is signing off on this
  package becoming part of the project's attack surface. Surface any new dep prominently in the PR description.
- **From the project's canonical registry.** PyPI, npm — not from arbitrary VCS URLs or unofficial mirrors.
- **Manifest entry matches the lockfile.** A package showing up in `uv.lock` (or equivalent) but not in
  `pyproject.toml`'s declared deps is a smell — direct deps should always be declared.

Cooldown gates ([cooldowns.dev](https://cooldowns.dev/) — delaying adoption of releases by a few hours/days so known-bad
versions get caught before they land) are a useful project-wide default but are a follow-up rather than per-PR
enforcement.

New GitHub Actions referenced without a specific commit SHA or semver tag are also supply-chain risks — flag them.

**Hardcoded secrets**

- Credential-shaped strings (API keys, tokens, private keys, cloud credentials, database URLs with embedded passwords).
- Even if a string looks like a placeholder (`sk-xxx...`, `password123`), flag it — placeholders mistakenly real have
  been the source of every credential leak that ever happened.
- Committed ciphertext can be fine — the bar is that the secret must not be recoverable from repo contents alone. That
  means encryption under a key held outside the repo and independently access-controlled (e.g. envelope encryption with
  an IAM-gated Cloud KMS key, which is how Pulumi `secure:` stack-config values work — see
  [`docs/design/deployment.md`](../../docs/design/deployment.md)). Anything weaker fails the bar: base64 or other
  encodings, hardcoded or repo-derivable keys, passphrase-based encryption that can be brute-forced offline. A raw
  credential sitting where ciphertext belongs is still a leak — flag it.

**Anything else that seems risky.** If a change makes you uncomfortable from a security standpoint and doesn't fit
cleanly above, say so. Better to surface a half-formed concern than to miss a real one.
