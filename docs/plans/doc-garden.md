# Plan: scheduled doc-gardening agent for `themis-internal`

## Context

The repo's documentation is written primarily for a model reading it as context (`CLAUDE.md` "Docs"); drift between a
doc and the code it describes silently degrades every future agent run. The doc-gardening agent is the countermeasure: a
scheduled job that audits the tracked docs against the real tree, fixes the drift it can confidently fix, and opens a
fix-up PR. The pattern follows OpenAI's "harness engineering" note — a recurring agent that scans for documentation that
no longer reflects real code behaviour and opens fix-up pull requests.

It is a sibling of the PR-time screen ([`screen-and-mirror-workflow.md`](screen-and-mirror-workflow.md)) and is built on
the same machinery: its PR is screened (regex + LLM), reviewed by CODEOWNERS, and mirrored 1:1 like any other. The agent
has no merge authority and cannot push to `main`, so a wrong edit is bounded to a PR a human rejects — and the
leak/regex screens still stand between its edits and the public mirror.

## Schedule

`cron: "0 14 * * 0-4"` plus `workflow_dispatch` for manual runs. The intent is midnight Sydney/Melbourne on workdays.
GitHub cron is UTC with no DST, so 00:00 Mon–Fri AEST (UTC+10) = 14:00 the previous day UTC, which is the UTC days
Sun–Thu. During AEDT (UTC+11, ~Oct–Apr) the run fires at 01:00 local; midnight is not load-bearing for a gardener, so
the seasonal hour-slip is accepted rather than worked around.

## Behaviour

- **Fix-up PR with edits**, not a report. The agent edits docs in place; the diff is the deliverable. Edits are
  confidence-gated: mechanical fixes (links, paths, stale markers) are applied freely; a behavioural claim the agent
  cannot verify against the code is left untouched and reported, never guessed.
- **One rolling PR.** A single branch `doc-garden/rolling` is reset from `main` and force-pushed each run; a PR is
  opened only when none is open, otherwise the force-push updates the existing one. The PR always reflects today's tree
  and never conflicts with `main`; branch protection dismisses the stale approval on each update, correctly re-gating
  the changed diff. A clean tree produces no branch, commit, or PR.
- **Scope** is all tracked Markdown — it is all documentation (`docs/`, every `README.md`, `GLOSSARY.md`, `CLAUDE.md`,
  `.claude/rules/`, `.github/**/*.md`). The drift classes and fix discipline live in the agent's instructions,
  [`.github/doc-garden/instructions.md`](../../.github/doc-garden/instructions.md).
- **Plans and design proposals are forward-looking.** `docs/plans/`, and any section describing a not-yet-built design,
  state a target; the agent does not read their divergence from current code as drift, gardening them only for
  build-independent drift (links, cross-references, terminology). This keeps plans in scope without a hard
  gardened/ungardened directory split or a manual archive-on-implementation step: the agent tells "unbuilt" from "wrong"
  by the doc's nature, not its location. The cost, accepted over the archival alternative, is that an implemented plan
  that later drifts from the code is not re-gardened — a design that must track code long-term belongs in a
  `docs/design/` doc, which *is* code-gardened when it describes a built system.

## Workflow

[`internal-doc-garden.yml`](../../.github/workflows/internal-doc-garden.yml), guarded by
`if: github.repository == 'populationgenomics/themis-internal'` so the mirrored copy is inert. It reuses the structure
of `internal-review.yml` (checkout → load-instructions → `claude-code-action` → upload the execution log), with two
additions: a GitHub App token step and a deterministic publish step.

- **The agent only reads and edits.** `claude-code-action@v1` does not open a PR itself (it pushes commits and prints a
  pre-filled link), and its branch/PR features target `pull_request`/`issue` events, not `schedule`. So the agent's
  allowed tools are `Read,Grep,Glob,Edit,Write` plus read-only `rg` / `git ls-files` / `git diff` / `git status` /
  `git log`. The Bash allowlist — no network, no mutating git — is the real security boundary; `Edit` vs `Write` is not
  (both only mutate doc content that reaches the world via the reviewed PR). Renames are out of scope: they need
  `git mv`, which the allowlist omits.
- **The publish step** detects the agent's edits, commits them to `doc-garden/rolling`, force-pushes, and opens the PR
  when none is open. Any untracked file the agent left is a scope violation (authoring is out of scope) and fails the
  step loudly rather than being dropped silently.

### Auth

- **Claude API via WIF**, no stored key. The scheduled run reuses the `cpg-themis-ci-review` service account through an
  added federation rule pinned to the `ref:refs/heads/main` subject (a `schedule` / `workflow_dispatch` run's OIDC `sub`
  is not `…:pull_request`, so the PR-review rule rejects it). Setup:
  [`claude-api-wif.md`](../runbooks/claude-api-wif.md) Path C.
- **GitHub App `themis-doc-garden`** (Contents + Pull-requests write, installed on `themis-internal`) opens the PR. This
  is required, not incidental: a PR opened by the default `GITHUB_TOKEN` does not emit `pull_request` events, so the
  required `regex screen` / `review + LLM screen` checks would never run and the PR could never merge. The mirror App
  cannot be reused — it is installed on the public `themis` repo only. Setup:
  [`doc-garden-app-setup.md`](../runbooks/doc-garden-app-setup.md).

## Prerequisites

The workflow cannot pass until these exist:

1. The Path-C federation rule (`cpg-themis-ci-review-main-rule`) is provisioned and its `fdrl_…` id wired into the
   workflow's `anthropic_federation_rule_id`.
1. The `themis-doc-garden` App is created and installed, with `DOC_GARDEN_APP_CLIENT_ID` (variable) and
   `DOC_GARDEN_APP_PRIVATE_KEY` (secret) stored on `themis-internal`.

## Failure modes

- A misconfigured WIF rule fails the token exchange with `400 invalid_grant`, which from the outside looks like "no
  drift" (no PR). Read the `claude-doc-garden-execution-output` artifact on early runs to tell them apart.
- `workflow_dispatch` from a feature branch carries a `ref:refs/heads/<branch>` subject that the `main`-pinned rule
  rejects; the live agent run must be dispatched from `main` (after merge). The non-WIF logic is testable on a branch.
- The doc-garden PR triggers the LLM review and screens on the gardener's own edits — intended defence-in-depth before
  they can mirror to public.

## Verification

1. Provision the prerequisites; fill the rule id into the workflow.
1. Run `doc garden` via `workflow_dispatch` from `main`. With drift present it opens/updates the `doc-garden/rolling`
   PR; with a clean tree it logs "No drift found" and opens nothing.
1. Confirm the PR shows `regex screen` and `review + LLM screen` running (proves the App token authored it).
1. Dispatch twice; confirm the second run updates the same PR rather than opening a second.
