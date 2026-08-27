# Themis development notes

## Product

See [`docs/PRODUCT.md`](docs/PRODUCT.md) for the product north star — what we're building and why, the load-bearing
principles, and what's out of scope. Read it before proposing designs or plans. Shared terminology lives in
[`GLOSSARY.md`](GLOSSARY.md).

## Working norms

Operating directives for Claude (and any agent) in this repo; they counteract default model dispositions.

- **Resist the minimal-diff reflex.** Don't reach for the smallest change that hides the symptom (special-casing,
  papering over root causes). Aim for the correct fix at the right complexity level — not the smallest, not gold-plated.
- **Fail loudly and early.** Raise on a missing expected input or precondition; never fall back to a default/placeholder
  to limp along. A placeholder is an explicit caller input, never a code default.
- **Never instruct around a defect — fix the defect.** Don't write prose telling readers to work around broken code —
  "pass it as a string, the converter loses precision". Prose is untested, and callers who didn't read it stay broken.
- **Push back; don't just comply.** When a design, name, or approach seems worse — including a shortcut you're asked to
  take — say so with reasoning, unprompted. The author owns the final call.
- **Offer better alternatives with trade-offs.** When a materially better approach than the proposed one exists, present
  it and the trade-offs — don't just execute the ask.
- **Investigate before producing.** Read the code and verify constraints first. Don't treat a training-pattern
  convention as load-bearing unchecked; don't speculate about what you can read.
- **Look up Anthropic/Claude API facts; don't recall them.** Model ids, pricing, limits, SDK and API behavior come from
  the `claude-api` skill — use it freely, but only inside a subagent: loading it costs ~300k tokens of context.
- **Explain non-obvious changes first.** For a change whose rationale isn't self-evident, give the why before showing or
  applying the diff.
- **Ask when unsure** rather than assume intent.
- **No intensifiers or emphasis filler.** Drop words and phrases that add emphasis but no information — "that's the
  key", "crucially", "importantly", "the key insight", "it's worth noting". State the point plainly. Applies to all
  prose: chat replies, PR/review comments, commit messages, and docs.

## Code style

@docs/style/general.md

## Services

Adding a backend service under `themis/services/` (the data plane) → follow
[`docs/design/services.md`](docs/design/services.md): the established pattern (hand-authored proto → generated stubs;
the server subclasses the generated servicer base on `grpc.aio`; one port `abc.ABC` per interface, its fail-loud,
env-seeded fixture backend in a module of its own; deploy stacked separately). Reuse it; don't reinvent per service.

## Docs

Two audiences, two registers:

- **Instruction files** are prompts and rules — `CLAUDE.md`, `.claude/rules/`, `.github/review/`, `.github/doc-garden/`:
  model-only, only what changes behavior, no maintainer notes, no harness mechanics (which rules load when, where files
  live). A token there is paid on every run that loads it; human-facing explanation belongs in `docs/` or code.
- **Everything under `docs/`** is written for a human first — a maintainer who has read
  [`docs/PRODUCT.md`](docs/PRODUCT.md) and [`GLOSSARY.md`](GLOSSARY.md) but not this area, and has to get the take-aways
  from one read on GitHub; under the review policy a design-doc PR is normally reviewed by a second maintainer. Explain
  with the clarity and style of Martin Kleppmann — motivation before mechanism, specifics out of the argument's way.
  Detail that restates code — field lists, paths, env vars, test names — stays in the code and is linked, never
  transcribed. A model reads what a human reads. Design docs are the durable design record: one living doc per area
  under `docs/design/`, rewritten in place; no ADRs — rationale lives in the doc, chronology in git. The guide is
  [`docs/style/design-docs.md`](docs/style/design-docs.md); to write or rewrite one, load the `writing-design-docs`
  skill.

## Committing

- **Stage explicit paths**, not `git add -A` / `.`. Every tracked commit is mirrored 1:1 to the public `themis` repo;
  explicit staging avoids sweeping in an untracked file the screen doesn't catch.
- **Pre-commit runs lint/format/hygiene** (`.pre-commit-config.yaml`); pyright runs in CI. Ensure hooks are installed
  (`pre-commit install`) — if not, install or ask the author; never bypass with `--no-verify`.
- **Correct a pushed branch with a new commit on top**, not amend + force-push. PRs squash-merge, so `main` history
  stays linear regardless and intermediate fixups vanish on merge. Reserve force-push for rebasing a branch onto `main`.

## Worktrees

Worktrees go in `.claude/worktrees/` (gitignored), never `../` siblings.

- **New branch** → the Claude Code worktree command.
- **Existing branch** → `git worktree add .claude/worktrees/<name> <branch>` (the command only cuts fresh branches).

## CI and review

See [`docs/plans/screen-and-mirror-workflow.md`](docs/plans/screen-and-mirror-workflow.md) for the screen-and-mirror
design.

- **Design docs, interfaces, and database schemas get their own PR and a second maintainer's review; a maintainer's
  implementation PRs don't need one.** Split design docs, interface changes (proto contracts, data-contract docs), and
  database schema changes (migrations) into their own PR and request that review after the author's read, before the
  draft goes ready (a request survives the draft phase); a maintainer's implementation PRs are normally merged by their
  author after the adversarial review passes, the PR screen, and the author's own read of the diff — the human review of
  agent-written code, so open PRs as drafts and leave going ready to the author. Requesting a review for any PR is fine.
  Any other contributor's PR — implementation included — needs a maintainer's approving review. The split is
  additive-first — a retirement, and an addition an exhaustiveness test binds, land with the code; the doc names each
  bound and the check that enforces it. Policy and encoding:
  [`docs/design/review-policy.md`](docs/design/review-policy.md).
- **Adversarially review before opening a PR.** For any change with non-trivial code or logic, run adversarial review
  passes in subagents with fresh context — the reviewer sees only the diff, not the authoring conversation — and fix the
  findings autonomously; repeat until a pass surfaces only diminishing findings, then open the PR. Exempt: trivial
  changes, doc-only changes, resource/asset changes.
- **A PR description is written for the human reviewer**: what the change is and why, the take-aways, and where to look
  — the altitude of a design doc's Overview, shorter. The diff carries the detail; don't narrate it. Same style:
  [`docs/style/design-docs.md` § Style](docs/style/design-docs.md#style).
- **Pin third-party GitHub Actions to the latest stable release**: the moving major tag (`@v3`) where the action
  publishes one, else the exact latest version (`@v8.2.0`). Verify against the action's releases when adding or bumping
  one.
- **A change to a rendered surface ships with screenshots** in the PR description — the surface before and after, or
  after alone when it is new. A reviewer cannot see layout, spacing, or state in a diff, and prose describing them is
  unfalsifiable. `apps/web` runs offline against the fixture backend (`THEMIS_BACKEND=fixture`), so capturing one needs
  no cloud access: build, start, drive the surface to the state under review, and screenshot it. GitHub has no
  image-attach API, so link the image instead: `uv run --group screenshot python -m tools.screenshot.upload after.png`
  uploads a capture to a world-readable bucket and prints the `![…](…)` line to paste into the body. Say so explicitly
  when a change is not capturable — a state reachable only against real data, say.
