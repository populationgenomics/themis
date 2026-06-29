# Plan: screen-and-mirror workflow for `themis-internal`

## Context

`themis-internal` holds the working tree for the Themis platform; a 1:1 mirror lives in the public `populationgenomics/themis` repo (same git objects, same SHAs).

**Threat model:** honest oversight. Contributors are competent and acting in good faith; the screen's job is to remind a careful person of something they didn't notice (e.g. a participant ID copy-pasted into a test fixture), not to defend against an adversarial merger. This framing drives the design toward soft, model-judgement-based tooling rather than structural enforcement, and it also lowers the bar for accepting contributions from people who aren't already CODEOWNERS: their PRs go through the same screen, and the reviewer sees both their changes and the screen's findings before approving.

Two pieces:

1. **PR-time screen** (the gate): every non-draft PR is scanned by (a) a tiny regex script for known-format identifiers and (b) `anthropics/claude-code-action` for everything that needs judgement. Both must pass before the merge button unlocks.
2. **Mirror** (the action): on every push to `main`, push the same commit 1:1 to public `themis`. No checks at this step. With branch protection in place, every commit on `main` has already passed the PR-time screen.

The screening machinery itself ships to the public repo as part of the 1:1 mirror. This is deliberate — the priority is catching internal issues before they're made public, and the screening logic is not sensitive. Workflows are guarded by `if: github.repository == 'populationgenomics/themis-internal'` so the mirrored copies are inert; Actions on the public repo is also disabled at the settings level as a backstop.

A future iteration may insert Copybara to rewrite content before mirroring (path excludes, scrubbing). Out of scope here — added only if there's a concrete need that justifies losing 1:1 SHAs.

## Branch protection (applied)

`main` is branch-protected with:

- PRs required, ≥1 approving review, CODEOWNERS review required
- Stale reviews dismissed on new pushes
- Conversation resolution required
- Linear history required
- No force-pushes, no deletions
- Admin bypass disabled (`enforce_admins: true`)

Repo settings restrict merge methods so squash-merge is the only way to land a PR. Combined with linear history, every commit on `main` has exactly one parent and corresponds to one merged PR.

Required status checks:

- `regex screen` — from the regex script.
- `review + LLM screen` — the claude-code-action workflow run (required so an API outage or runner failure can't fail-open).
- `strict: true` ("Require branches to be up to date before merging") so the merge base equals the latest main when squash happens.

A PR that modifies `internal-review.yml` itself fails `review + LLM screen` by design: claude-code-action refuses to run a workflow file that differs from the default branch (anti-tamper). To land such a PR, an admin temporarily removes that check from the required list, merges, and restores it.

## PR-time screen

### Regex script

A small Python script (`tools/screen/regex.py`, ~50 lines) that:

1. Resolves the merge base of the PR head against `main`.
2. Diffs `merge-base..head` with `--unified=0` and extracts the added lines (`+` lines, excluding headers).
3. Loads a pattern list from `.github/screen/patterns.yaml` (a YAML file listing `{name, regex}` entries — e.g. `CPG[A-Z0-9]+`).
4. Reports any matches with file + line, ignoring lines that carry an inline suppression marker.
5. Exits non-zero if any unsuppressed match remains.

**Suppression syntax**: trailing same-line comment in the file's native comment syntax, with a required reason:

- Python / YAML / shell: `participant_id = "CPGxxx"  # screen-ignore: deliberate test fixture`
- JS / TS / Go / Rust: `// screen-ignore: deliberate test fixture`
- C / C++: `/* screen-ignore: ... */`
- HTML / XML: `<!-- screen-ignore: ... -->`

The script recognises the marker on the added line itself; no block form. Files without comment syntax (JSON, plain text) have no suppression mechanism — restructure or accept the hit. The reason is required (non-empty after the colon) to keep the override audit trail self-documenting.

Suppression markers are effective in the same PR they're introduced. Under honest-oversight, adding a marker is a deliberate act by a CODEOWNER-reviewed contributor.

Workflow:

```yaml
# .github/workflows/internal-screen-regex.yml
on:
  pull_request:
    types: [opened, ready_for_review, synchronize]

jobs:
  regex:
    if: |
      github.repository == 'populationgenomics/themis-internal'
      && !github.event.pull_request.draft
    runs-on: ubuntu-latest
    permissions: { contents: read, pull-requests: write, statuses: write }
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
      - run: python tools/screen/regex.py
          --head ${{ github.event.pull_request.head.sha }}
          --base ${{ github.event.pull_request.base.sha }}
          --patterns .github/screen/patterns.yaml
```

The job's success/failure becomes the `regex screen` required status check.

### Claude Code Action

`anthropics/claude-code-action@v1` runs on the same triggers, with an instructions document driving the review.

```yaml
# .github/workflows/internal-screen-llm.yml
on:
  pull_request:
    types: [opened, ready_for_review, synchronize]

jobs:
  review:
    if: |
      github.repository == 'populationgenomics/themis-internal'
      && !github.event.pull_request.draft
    runs-on: ubuntu-latest
    permissions: { contents: read, pull-requests: write, id-token: write }
    steps:
      - uses: actions/checkout@v6
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 1
      - uses: anthropics/claude-code-action@v1
        with:
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          prompt_file: .github/screen/llm-instructions.md
          claude_args: |
            --allowedTools "mcp__github_inline_comment__create_inline_comment,Bash(gh pr comment:*),Bash(gh pr diff:*),Bash(gh pr view:*)"
```

`.github/screen/llm-instructions.md` is a structured instructions document with one section per concern (participant identifiers, internal-only documentation, references to infrastructure or systems, concrete model identifiers, etc.). Each section describes what to look for and gives examples. The model-identifier concern is judgement, not regex: generic prose ("frontier models") and tooling names (`claude-code-action`) are fine; the leak is a concrete per-agent model id, which is secret-class (see [`deployment.md`](../design/deployment.md) § Confidential config). The instructions also include:

- **Scope rule**: "Focus on changes introduced by this PR. Don't re-flag content that exists in the base branch unchanged. Use `gh pr diff` to see exactly what changed."
- **Dedup rule**: "Before posting an inline comment, run `gh pr view --json comments,reviewComments` (or equivalent) and check whether the same concern has already been raised on the same line. If yes — resolved or not — skip it."
- **Output rule**: post line-anchored review comments via the inline-comment MCP tool. If any inline findings were posted in this run, also post a single top-level PR comment summarising the findings count and pointing reviewers at them — so a reviewer skimming the PR sees clearly that there's something to look at, rather than relying on them noticing the Files tab. If no findings, do not post a top-level comment.

The job's success/failure becomes the `review + LLM screen` required status check; this only fails if the action itself errors (API down, runner timeout) — review findings post as comments and the workflow exits successfully regardless. The *content* of findings gates via `required_conversation_resolution`.

### Override flow

- **Regex hit**: author either fixes the offending content or adds an inline `# screen-ignore: <reason>` marker on the line. The marker change is itself part of the PR diff and visible to the reviewer.
- **Action finding (review comment)**: reviewer reads the comment, decides, resolves the conversation. With `required_conversation_resolution: true` already on, unresolved comments block merge.
- **Action errored out** (workflow run failed): re-run the workflow once the underlying issue (API outage, etc.) is resolved. No "override the action error" path — the gate is fail-closed by design.

No `screen-override` label. No separate override workflow.

### Behaviour summary

- Author opens a draft PR → no screening runs.
- Author marks ready / pushes a commit → both regex and action run on the head SHA. Status checks update on the PR.
- Regex hits → `regex screen` fails, merge blocked. Author fixes or adds a suppression marker.
- Action posts review comments → conversations need resolution before merge.
- Reviewer approves + resolves conversations → merge button unlocks.
- Author pushes again → dismiss-stale invalidates the approval; both workflows re-run; cycle repeats.

## Mirror

A single workflow that pushes via a GitHub App token and is fully idempotent — re-running on the same commit, racing against another run, or recovering from a missed event all converge to the same correct end state.

```yaml
# .github/workflows/internal-mirror.yml
on:
  push:
    branches: [main]

jobs:
  mirror:
    if: github.repository == 'populationgenomics/themis-internal'
    runs-on: ubuntu-latest
    timeout-minutes: 5
    concurrency:
      group: mirror-public
      cancel-in-progress: false
    permissions: { contents: read, id-token: write }
    steps:
      - uses: actions/create-github-app-token@v3
        id: app-token
        with:
          client-id: ${{ vars.MIRROR_APP_CLIENT_ID }}
          private-key: ${{ secrets.MIRROR_APP_PRIVATE_KEY }}
          owner: populationgenomics
          repositories: themis

      - uses: actions/checkout@v6
        with: { fetch-depth: 0 }

      - name: Push to public mirror (idempotent)
        env:
          GH_TOKEN: ${{ steps.app-token.outputs.token }}
        run: |
          set -euo pipefail
          git remote add public "https://x-access-token:${GH_TOKEN}@github.com/populationgenomics/themis.git"
          git fetch public main || true   # may be empty on first run

          LOCAL=$(git rev-parse HEAD)
          PUBLIC=$(git rev-parse FETCH_HEAD 2>/dev/null || echo "")

          if [ -n "$PUBLIC" ] && [ "$LOCAL" = "$PUBLIC" ]; then
            echo "Public already at $LOCAL; nothing to do."
            exit 0
          fi
          if [ -n "$PUBLIC" ] && git merge-base --is-ancestor "$LOCAL" "$PUBLIC"; then
            echo "Public is ahead of internal at this SHA (a later run already pushed); nothing to do."
            exit 0
          fi
          if [ -n "$PUBLIC" ] && ! git merge-base --is-ancestor "$PUBLIC" "$LOCAL"; then
            echo "ERROR: public main has diverged from internal at $PUBLIC." >&2
            exit 1
          fi

          git push --follow-tags public HEAD:main
```

Why each piece:

- **GitHub App auth (`actions/create-github-app-token`)** instead of an SSH deploy key. Tokens are short-lived (~1h), fine-grained (`contents: write` on `themis` only), not tied to a person leaving the org, and visible in the org audit log. Deploy keys are acceptable but long-lived with no rotation story.
- **Idempotent pre-check** with four branches:
  - Public == local → no-op (re-run on same commit).
  - Local is ancestor of public → no-op (a later workflow run already pushed; this one is the laggard).
  - Public is ancestor of local → fast-forward push.
  - Otherwise → fail explicitly. Divergence means something pushed to public outside this workflow (manual override, tampering); silently overwriting would be wrong.
- **`concurrency: { group: mirror-public }`** serialises this workflow's own jobs so the check-and-push is atomic relative to other runs. Without it the check is still correct but could lose a non-fast-forward race against a parallel run.
- **`--follow-tags`** publishes annotated tags reachable from `main` (releases) along with the branch. Tag pushes are themselves idempotent — already-present tags are skipped by the protocol.
- **`timeout-minutes: 5`** prevents a stuck job from blocking the concurrency slot for hours.

Branch protection already ensures every commit on `main` has passed the PR-time screen, so there's no per-commit check at this step. Ordering note: `git push` sends all reachable objects not on the remote, so even an out-of-order dispatch where commit B's job runs before commit A's leaves the public mirror with a complete history — B's push carries A along automatically. The idempotent check then makes A's later job a clean no-op rather than a spurious non-fast-forward failure.

## Workflow naming and guards

All workflows in the initial set are internal-only and named `internal-*.yml`. The filename is documentation; the actual gate is `if: github.repository == 'populationgenomics/themis-internal'` on every job, which keeps the mirrored copies inert on the public side.

Belt-and-braces: disable Actions on the public repo via Settings → Actions, so an unguarded workflow file in the public tree cannot execute. The `if:` guards are still required for correctness; the repo-level switch is a backstop.

If we add a workflow that should run on the public mirror (none planned), we'll introduce a `public-*.yml` / unprefixed-shared convention at that point — no need to pre-define naming buckets we don't use.

## Auth and secrets

- **GitHub App for the mirror push.** A dedicated App in the `populationgenomics` org, installed on `themis` only, with `contents: write`. The App's Client ID is exposed to workflows as the `MIRROR_APP_CLIENT_ID` repository variable on `themis-internal`; the private key as the `MIRROR_APP_PRIVATE_KEY` repository secret. `actions/create-github-app-token@v3` mints a short-lived installation token at the start of the mirror job. Setup runbook: [`docs/runbooks/mirror-app-setup.md`](../runbooks/mirror-app-setup.md).
- `ANTHROPIC_API_KEY` — for claude-code-action. Repository secret on `themis-internal`.
- `GITHUB_TOKEN` — default. Permissions per workflow: regex job needs `pull-requests: write` and `statuses: write`; action job per its own docs. Mirror job needs `contents: read` (only to read the internal checkout) plus `id-token: write` for the App-token action. No workflow needs `contents: write` on the internal repo.

## Files to add

- `.github/workflows/internal-screen-regex.yml`
- `.github/workflows/internal-screen-llm.yml`
- `.github/workflows/internal-mirror.yml`
- `.github/screen/patterns.yaml` — regex patterns to detect (initially just the known-format identifier shapes).
- `.github/screen/llm-instructions.md` — instructions for claude-code-action (concern list, scope rule, dedup rule, output rule).
- `tools/screen/regex.py` — the regex script.

## Follow-up settings (after workflows land)

- Add `screen / regex` and `Claude Code` to `required_status_checks.contexts` on `main`'s protection rule, with `strict: true`.
- Disable Actions on `populationgenomics/themis`.
- Create a GitHub App in the `populationgenomics` org (e.g. "themis-mirror"), install it on `themis` with `contents: write`, store the Client ID as `MIRROR_APP_CLIENT_ID` (repo variable) and private key as `MIRROR_APP_PRIVATE_KEY` (repo secret) on `themis-internal`. Step-by-step: [`docs/runbooks/mirror-app-setup.md`](../runbooks/mirror-app-setup.md).
- Add `ANTHROPIC_API_KEY` as a repo secret on `themis-internal`.

## Verification

1. **Regex script local**: `python tools/screen/regex.py --head <sha> --base <sha> --patterns .github/screen/patterns.yaml` against a checkout; confirm it scans only added lines and respects suppression markers.
2. **PR-time regex pass**: open a benign PR; confirm `screen / regex` passes.
3. **PR-time regex fail**: open a PR introducing a known-format ID; confirm `screen / regex` fails and the merge button is disabled. Add an inline `# screen-ignore: <reason>` marker; confirm the next push passes.
4. **PR-time action pass**: open a benign PR; confirm `Claude Code` workflow succeeds and either posts no comments or only advisory ones that can be resolved.
5. **PR-time action finding**: open a PR with a deliberate concern in a non-suppressible area; confirm the action posts an inline comment; confirm merge blocks until the conversation is resolved.
6. **Action dedup**: push a second commit to the same PR without changing the offending line; confirm the action does not re-post the same comment (relying on the dedup rule in instructions + GitHub's resolved-conversation persistence).
7. **Draft skip**: open a draft PR; confirm neither workflow runs. Mark ready; confirm both run.
8. **Mirror push**: merge a passing PR; confirm `internal-mirror.yml` runs and the commit appears on public `themis` with identical SHA. Re-run the same workflow run; confirm the second run is a no-op ("Public already at ..."). Push a tagged commit; confirm the tag appears on public.
9. **Mirror divergence guard**: manually push a different commit to public `themis` `main`; confirm the next internal mirror run fails with the "diverged" error rather than silently overwriting.
10. **Action error fail-closed**: temporarily break the action workflow (e.g. invalid API key); confirm the `Claude Code` status fails and the merge button is disabled even with all conversations resolved.

## Related

The scheduled doc-gardening agent ([`doc-garden.md`](doc-garden.md)) is a sibling automation built on this machinery: its fix-up PRs pass through the same PR-time screen and branch protection before they can merge and mirror.
