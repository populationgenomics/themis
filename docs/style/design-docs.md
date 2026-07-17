# Design docs

The design doc is themis's durable design record. There is **no separate ADR type**: a decision's rationale — what was
chosen, and what was rejected and why — lives in the design doc for the area it governs, kept current. Chronology (how
the design got here) lives in git and the merged PR discussion, not a hand-maintained trail.

## Rules

- **One living doc per area**, under `docs/design/`. It states the current decision, mechanism, rationale, and the
  still-relevant rejected alternatives — each once (the terseness rule, [`../../CLAUDE.md`](../../CLAUDE.md) "Docs").
- **Rewrite in place.** When a design changes, edit the doc; never append a supersession layer or spawn a "v2". A
  superseded doc is *deleted*, its live content folded into the successor, **in the same PR** as the change that
  supersedes it. A stale design doc doesn't just cost tokens — it misleads the next reader into building the wrong
  thing.
- **`Status` is `draft` or `current`.** `draft` = proposed, under review; `current` = authoritative. A decision in
  flight is a `draft` design-doc PR (or just the PR description + review), flipped to `current` on merge.
- **Chronology is git.** "When did we decide X / what did we once do" = `git log` / blame / the PR. Don't restate it in
  prose.
- **Rationale lives in the doc.** `Alternatives considered` is where the value the retired ADRs carried now goes;
  dropping it silently loses the "we rejected X because Y" that stops re-litigation.

## Template

A skeleton, not a fill-every-box mandate — a small design keeps a small doc; the optional section appears only when it
carries signal.

```markdown
# Design: <area>

**Status:** draft | current   **Related:** [`<adjacent>.md`](<adjacent>.md) (<what it covers>), …

## Overview

<1–2 sentences: what this is and what it's for.>

## Background

<The durable problem + constraints that motivate the design. Not narrative history.>

## Non-goals

<What this deliberately does NOT do or cover. Distinct from deferred work below.>

## Design

<The decision and its mechanisms — the core. State each once. Non-obvious consequences and
gotchas live inline, next to the mechanism they qualify.>

## Alternatives considered

<Options weighed and why rejected — the decision framing.>

## Implementation state

<What's built vs planned. Slices with status where staged; "shipped (#NNN)" when complete.>

## Open questions   (optional — omit when the design is closed)

<Unresolved points needing a decision or input.>
```

Section notes:

- **Status / Related** — one line. Links are how a reader (often a model) navigates the doc graph: the motivating
  models, what this depends on, what depends on it.
- **Overview** — the relevance hook a reader triages from before reading on.
- **Non-goals** — the cheapest scope-creep and misread preventer; answers "why doesn't this handle X?" with "by design,
  it doesn't." *Never*, as opposed to *not yet* (which is Implementation state).
- **Design** — keep consequences/gotchas inline, next to the mechanism they qualify; a standalone "Consequences" section
  drifts from what it's about and is re-paid on every read.
- **Alternatives considered** — keep the rejections still worth knowing; drop moot ones on the next rewrite.
- **Open questions** — omit entirely when there are none; never leave an empty "None".
