# Design: Deployment — IaC, deploy auth, state, and secrets

**Parent epic:** [`issues/epic-themis-spike.md`](../../issues/epic-themis-spike.md) (PR #1) **Related:**
[`spike-infrastructure.md`](spike-infrastructure.md) — what infrastructure the Spike needs; this doc decides how it is
managed. Resolves that exploration's IaC-tool and secrets-management questions and the auth part of its CI/CD question.
Also [`migrations.md`](migrations.md) — the database schema step the deploy pipeline runs after `pulumi up`.

Constraint: every tracked file is mirrored publicly. The only hard exclusion is plaintext secret material. Identifiers
and KMS-gated ciphertext are fine ("What lives where"); the leak-screen and security review policies are refined in step
with this doc.

## Decisions

| Concern            | Decision                                                                                                                           |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| IaC tool           | Pulumi (Python SDK), program tracked in this repo                                                                                  |
| Deploy trigger     | Push `deployed/<env>`, or dispatch on `main`; merging deploys nothing (temporary — see below)                                      |
| Deploy auth, CI    | GitHub Actions OIDC → Workload Identity Federation; no service-account keys                                                        |
| Deploy auth, local | Personal `gcloud` ADC, IAM-gated                                                                                                   |
| State backend      | Versioned, private GCS bucket (Pulumi DIY backend)                                                                                 |
| Secrets encryption | `gcpkms` secrets provider (Cloud KMS key, IAM-gated)                                                                               |
| Secret values      | OIDC/IAM-first: Anthropic API via WIF, Cloud SQL via IAM auth; only no-WIF-path credentials in Secret Manager                      |
| Model selection    | Concrete per-agent model id is confidential config — gcpkms-encrypted in the Pulumi stack config, never plaintext in tracked files |
| Repo contents      | Standard Pulumi layout incl. `Pulumi.<stack>.yaml`; no plaintext secrets, ever                                                     |

## Deploy trigger

`deploy.yml` has two entry points, and merging to `main` is neither — a merge lints, tests, and builds images, but does
not deploy.

- **Ad-hoc:** push the `deployed/<env>` branch — `git push --force origin <commit-ish>:deployed/dev`, taking a branch
  name or a bare SHA. The push *is* the deploy, so the branch always names what is live, and because the deployed commit
  is the one being built, a running revision's image tag is that commit. The deploy record is the workflow's run list,
  which carries who deployed and whether it succeeded.
- **Reviewed:** dispatch the workflow on `main` (Actions → deploy → Run workflow), which needs no branch juggling for
  the routine case.

**Temporary.** Dev doubles as the demo environment and is regularly deployed from unreviewed branches. Deploy-on-merge
overwrites whatever is running there, without warning and possibly mid-demo: merging an unrelated PR should not reset
someone else's environment. Revisit once dev stops carrying ad-hoc demos of unreviewed code, and for `prod` when it
exists — a stable environment wants deploy-on-merge (`on: push: branches: [main]`), and should not carry an ad-hoc
deploy branch at all, which is why `bootstrap.sh` grants that member only when asked.

### Who can deploy

The deploy SA's WIF binding accepts exactly two refs, `refs/heads/main` and `refs/heads/deployed/<env>`, scoped by
`attribute.ref` rather than by event so it covers both entry points. Everything else follows from that.

**The WIF binding is the whole boundary, and a GitHub ruleset is what makes it mean something.** Anyone with repo write
can dispatch the workflow on any branch, and that run executes *that branch's* copy of `deploy.yml` — guard step
deleted, `pulumi up` replaced with anything. What stops it is that the dispatched ref also determines the token's `ref`
claim, so `refs/heads/<their-branch>` matches neither member and impersonation fails. Workflow-file provenance
contributes nothing; do not treat it as a second layer. (`deploy.yml` reads no `secrets.*`, so such a run gains nothing
beyond the `GITHUB_TOKEN` any PR job already has.)

The two refs the binding does accept are therefore the entire attack surface, and both are write-controlled: `main` is
PR-reviewed, and `deployed/*` is covered by a ruleset whose `Restrict creations` / `Restrict updates` /
`Restrict deletions` rules bypass only for the **repository-admin role**.

Bypassing by role, not by named actor, is what keeps membership out of this repo entirely — `bootstrap.sh` lists refs,
never logins, and the deployer set is whoever GitHub says is an admin.

**The ruleset must exist before the WIF member does, and the branch must not.** The binding is the credential; the
ruleset is the only thing standing in front of it, so between adding the member and configuring the ruleset any repo
write can push `deployed/dev` and take the deploy SA. `Restrict creations` does not rescue a late ruleset either: it
blocks a *future* creation, so a branch created beforehand is simply frozen with whatever it already points at. Hence
the ad-hoc member is opt-in (`ADHOC_DEPLOY_BRANCH`) rather than something `bootstrap.sh` grants by default, and
[`../runbooks/fresh-environment.md`](../runbooks/fresh-environment.md) orders the steps: confirm no `deployed/*` ref
exists, create the ruleset, then add the member.

This matters because the deploy SA holds `roles/resourcemanager.projectIamAdmin` on its project: whoever can make it run
can grant themselves any role there. GitHub's OIDC token carries no permission or role claim, so "admins only" cannot be
expressed in the WIF binding — a ref only admins can write is the mechanism that expresses it.

The admin role is broader than the `roles/iam.serviceAccountTokenCreator` holders who can already deploy by hand, and
broader still because organization owners inherit it. That is deliberate: an org owner can rewrite this repo regardless,
so gating them buys nothing. The population the gate actually excludes is repo-write-without-admin, which is most of the
collaborator list and has no other path to a GCP credential.

Accepted consequences:

- **`main` and the deployed stack drift by default.** Whoever needs a merged change live deploys it; nothing does it for
  them. A migration merged but not deployed stays pending, which the runner handles — it is forward-only and safe to run
  unconditionally ([`migrations.md`](migrations.md)).
- **Dev is one shared stack, so deployers overwrite each other.** `concurrency: themis-deploy-dev` serialises runs but
  does not arbitrate between them; the last deploy wins. Deliberate clobbering replaces automatic clobbering — it is not
  eliminated. A `deployed/dev` deploy also applies that branch's `infra/` program, so an unreviewed infra change reaches
  the stack and a broken one wedges it until someone deploys a good ref.
- **An ad-hoc deploy applies that branch's migrations, and those do not come back.** The migrate step runs on every
  deploy, so an unreviewed migration reaches the shared dev database. Migrations are forward-only
  ([`migrations.md`](migrations.md)), so redeploying a good ref — the remedy for every other item here — reverts the
  stack but not the schema.

## Deploy authentication

CI: the workflow exchanges a short-lived GitHub OIDC token via WIF to impersonate a deploy service account. No
long-lived credential exists anywhere. The WIF provider name and deploy-SA email sit in the workflow file; GitHub holds
no secrets.

- The WIF provider's attribute condition pins `attribute.repository == "populationgenomics/themis-internal"`; OIDC
  tokens name the requesting repository, so the public mirror (a different repo, Actions disabled) cannot satisfy it.
  Which SA a token may impersonate is then scoped per SA: deploy to deployable-ref tokens, preview to `pull_request`
  tokens.
- **Write** access (deploy) runs only from a deployable ref, as the deploy SA — its WIF binding is scoped to
  `refs/heads/main` and `refs/heads/deployed/<env>` tokens (above). A PR's ref is `refs/pull/N/merge`, which matches
  neither. PRs get a **read-only** identity (the preview SA, WIF binding scoped to `pull_request` tokens) used only to
  run `pulumi preview` and post it as a comment, informing PR review before merge. No PR job can mutate cloud state. The
  read-only token is dev-scoped (synthetic data); the residual malicious-PR risk is bounded by the private repo and
  trusted membership. Cloud-free validation (lint, type-check, unit tests) also runs on PRs.
- "Read-only" ≠ "cannot read secrets": `pulumi preview` runs the PR's own copy of the program and the preview SA can
  **decrypt** the stack's gcpkms secrets (the secrets manager loads on every op), so a malicious PR could run arbitrary
  code as that SA and exfiltrate any secret in the previewed stack's state/config plus project-wide `viewer` reads.
  Tolerable while the dev stack stores **no** secrets and membership is trusted. Before either trigger flips — the first
  `require_secret(...)` in a previewed stack, or prod adopting the same preview-on-PR posture — reconsider granting
  `cryptoKeyDecrypter` to a PR-triggered identity (e.g. drop decrypt and accept previews that can't diff secret values,
  or gate preview behind manual approval).

Local: `gcloud auth application-default login`; IAM grants to named individuals.

## State

**Per-environment** private GCS bucket (`gs://cpg-themis-<env>-pulumi-state`), each in its own project — a stack must
never read another's state. Uniform bucket-level access, object versioning, public-access prevention; IAM limited to
that environment's deploy SA (read/write), preview SA (read), and developers. State may contain secret ciphertext;
bucket privacy and the secrets provider are independent layers. `Pulumi.yaml` carries **no** `backend:` (it is shared
across stacks, which would force a single bucket); the backend is selected per environment instead — CI passes
`--cloud-url`, locally `pulumi login gs://cpg-themis-<env>-pulumi-state`.

## Secrets encryption

`gcpkms`, not `passphrase`:

```
pulumi stack init prod \
  --secrets-provider="gcpkms://projects/…/locations/…/keyRings/…/cryptoKeys/…"
```

- Decryption authority is IAM (`roles/cloudkms.cryptoKeyEncrypterDecrypter`) on the key — the same identity plane as
  deploys. No shared passphrase to distribute, rotate, or revoke.
- Every decrypt lands in Cloud Audit Logs.
- The wrapped data key is inert without KMS access; no offline brute-force path. Passphrase ciphertext is
  offline-bruteforceable.

## Secret values

OIDC/IAM-first — the workload authenticates with short-lived credentials, not stored secrets, wherever a federation path
exists:

- **Anthropic API:** Workload Identity Federation; the workload SA's GCP OIDC token is exchanged for short-lived
  Anthropic credentials. No API key stored.
- **Cloud SQL:** IAM database authentication via the workload SA. No database password.
- **GCP services:** the workload SA's IAM credentials from the metadata server.

The only Secret Manager secrets are credentials with no WIF path — currently the optional Semantic Scholar API key. Read
at runtime via the workload SA (`roles/secretmanager.secretAccessor`); Pulumi provisions the `Secret` containers, IAM
bindings, **and the versions** — each value a `gcpkms`-encrypted config secret in `Pulumi.<stack>.yaml` (inert without
KMS access; safe on the mirror), pushed as a `SecretVersion`, so a fresh `pulumi up` self-provisions the secrets with no
manual step. Rotation updates the config and re-applies; a value rotated out of band uses `ignoreChanges`.

Nothing with a WIF path is stored. Pulumi-generated provision-time values (e.g. a `RandomPassword`) use the same
gcpkms-encrypted path, currently unused while IAM auth covers Cloud SQL.

## Confidential config

The concrete **model selection** (which model id powers which agent) is not a credential but is secret-class
confidential config. It lives as gcpkms-encrypted Pulumi stack config (ciphertext in the repo, safe on the mirror), read
at runtime from a Secret Manager secret or an equivalent config blob — the choice is immaterial. Never plaintext in any
tracked file (agent config, Pulumi program, docs); generic statements ("frontier models") stay public, the concrete id
and per-agent assignment do not.

## What lives where

| Artifact                                             | Lives                                                                                                                   |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Pulumi program, `Pulumi.yaml`, `Pulumi.<stack>.yaml` | this repo; secret values only as KMS ciphertext                                                                         |
| State checkpoints                                    | state bucket                                                                                                            |
| Credentials with no WIF path                         | Secret Manager; values as `gcpkms` config in `Pulumi.<stack>.yaml`, applied by Pulumi                                   |
| Anthropic / Cloud SQL / GCP access                   | short-lived WIF/IAM credentials; nothing stored                                                                         |
| Concrete model selection                             | gcpkms-encrypted Pulumi stack config; read at runtime (Secret Manager or config blob); never plaintext in tracked files |
| KMS key                                              | Cloud KMS, IAM-gated, audit-logged                                                                                      |

Committing `Pulumi.<stack>.yaml` (secrets-provider URL, project ID, region, resource names, ciphertext) is safe on the
public mirror: identifiers are not credentials (access is IAM), ciphertext is KMS-gated, and the mirror carries only
`main`'s files — CI, PR discussion, and logs stay in this private repo.

Never in the repo or logs: plaintext secret values, service-account keys (none exist), identifiers of CPG infrastructure
beyond the Spike's deploy project. The Spike is isolated and public-endpoints-only, so its config discloses nothing
about the data estate.

Bootstrap: the state bucket, KMS key ring, and WIF pool predate the first `pulumi up`; created once by a documented
`gcloud` script (spike-infrastructure runbook deliverable).

## Why Pulumi

Candidates: Pulumi, Terraform/OpenTofu, `gcloud` scripts, manual + runbook. Manual fails reproducibility; scripts aren't
idempotent or drift-aware. Versus Terraform:

- Python program — same language and CI toolchain (ruff, pyright, pytest) as the rest of the repo; HCL is a second
  language.
- Secrets are encrypted inside state; Terraform state is plaintext, protected only at the storage layer (OpenTofu has
  client-side state encryption, younger ecosystem).
- Licensing: Terraform is BUSL since 2023; Pulumi CLI/SDKs are Apache-2.0.

Accepted trade-offs: smaller module ecosystem and community. Pulumi's GCP provider is bridged from Terraform's, so
resource coverage is equivalent. Pulumi Cloud / ESC is the upgrade path if self-managing chafes; the DIY backend keeps
the Spike self-hosted.

## Out of scope — stays with spike-infrastructure

- GCP project layout, IAP/SSO, cost observability, audit/retention, agent sandbox image.
- CI/CD pipeline shape beyond auth: Artifact Registry, image build, promotion path, preview environments.
- Per-secret inventory and rotation cadence.
- The fresh-environment runbook (absorbs the bootstrap steps).
