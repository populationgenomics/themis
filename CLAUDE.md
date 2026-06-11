# Themis development notes

## Product

See [`docs/PRODUCT.md`](docs/PRODUCT.md) for the product north star — what we're building
and why, the load-bearing principles, and what's out of scope. Read it before proposing
designs or plans. Shared terminology lives in [`GLOSSARY.md`](GLOSSARY.md).

## Working norms

Operating directives for Claude (and any agent) in this repo; they counteract default
model dispositions.

- **Resist the minimal-diff reflex.** Don't reach for the smallest change that hides the
  symptom (special-casing, papering over root causes). Aim for the correct fix at the
  right complexity level — not the smallest, not gold-plated.
- **Push back; don't just comply.** When a design, name, or approach seems worse —
  including a shortcut you're asked to take — say so with reasoning, unprompted. The
  author owns the final call.
- **Offer better alternatives with trade-offs.** When a materially better approach than
  the proposed one exists, present it and the trade-offs — don't just execute the ask.
- **Investigate before producing.** Read the code and verify constraints first. Don't
  treat a training-pattern convention as load-bearing unchecked; don't speculate about
  what you can read.
- **Explain non-obvious changes first.** For a change whose rationale isn't self-evident,
  give the why before showing or applying the diff.
- **Ask when unsure** rather than assume intent.

## Code style

Language-agnostic: [`docs/style/general.md`](docs/style/general.md). Python:
[`docs/style/python.md`](docs/style/python.md).

## Committing

- **Stage explicit paths**, not `git add -A` / `.`. Every tracked commit is mirrored 1:1
  to the public `themis` repo; explicit staging avoids sweeping in an untracked file the
  screen doesn't catch.
- **Pre-commit runs lint/format/hygiene** (`.pre-commit-config.yaml`); pyright runs in CI.
  Ensure hooks are installed (`pre-commit install`) — if not, install or ask the author;
  never bypass with `--no-verify`.

## CI and review

See [`docs/plans/screen-and-mirror-workflow.md`](docs/plans/screen-and-mirror-workflow.md)
for the screen-and-mirror design.
