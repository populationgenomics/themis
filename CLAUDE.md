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
- **Push back; don't just comply.** When a design, name, or approach seems worse — including a shortcut you're asked to
  take — say so with reasoning, unprompted. The author owns the final call.
- **Offer better alternatives with trade-offs.** When a materially better approach than the proposed one exists, present
  it and the trade-offs — don't just execute the ask.
- **Investigate before producing.** Read the code and verify constraints first. Don't treat a training-pattern
  convention as load-bearing unchecked; don't speculate about what you can read.
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
the server subclasses the generated servicer base on `grpc.aio`; a port `abc.ABC` with a fail-loud, env-seeded fixture
backend; deploy stacked separately). Reuse it; don't reinvent per service.

## Docs

The primary audience for docs is a model reading them as context; humans second. Be terse: state each decision,
mechanism, and rationale once — no rhetorical emphasis, no persuasion, no recaps. Every token written is re-paid on
every future read.

Design docs are the durable design record — one living doc per area under `docs/design/`, rewritten in place. There are
no ADRs: rationale lives in the doc's `Alternatives considered`, chronology in git. See
[`docs/style/design-docs.md`](docs/style/design-docs.md) for the policy and template.

`CLAUDE.md` and `.claude/rules/` go further: model-only context, not human docs. Include only what changes behavior — no
maintainer notes, no describing harness mechanics (which rules load when, where files live). Such content costs tokens
every session and changes nothing; human-facing explanation belongs in `docs/` or code.

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

- **Adversarially review before opening a PR.** For any change with non-trivial code or logic, run adversarial review
  passes in subagents with fresh context — the reviewer sees only the diff, not the authoring conversation — and fix the
  findings autonomously; repeat until a pass surfaces only diminishing findings, then open the PR. Exempt: trivial
  changes, doc-only changes, resource/asset changes.
- **Pin third-party GitHub Actions to the latest stable release**: the moving major tag (`@v3`) where the action
  publishes one, else the exact latest version (`@v8.2.0`). Verify against the action's releases when adding or bumping
  one.
