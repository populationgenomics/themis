### Tiers: which findings surface where, and when

The concern files say what counts as a finding. This file says where a finding goes. Tier is chosen by **what the fix
costs if it is deferred** — not by how confident you are, how subtle the finding is, or how much work it took to see.

("Tier" here always means T1/T2/T3. The context-budget concern uses the same word for its own always-on /
path-conditional / instruction-conditional split; those are not these.)

**T1 — wrong now.** The merged code does the wrong thing, or discloses something it must not: a runtime bug, a guard
that doesn't guard, silent degradation where the code should fail loudly, any security finding, any leak-screening hit.

Security and leak screening are never T3, at any severity or confidence. Both concerns instruct you to surface
half-formed suspicions, and a suspicion has no nameable failure by construction — so the default below would swallow
exactly the findings those files most want raised. Leak screening additionally cannot wait: the public mirror pushes on
merge, so a leak the merge-time sweep files as an issue is a leak that has already shipped.

**T2 — the fix gets more expensive with time.** Correct today, but the cost of changing it climbs once it lands and
something depends on it. Examples, not a closed list: stored-artifact and database schemas; proto and wire surface; a
new public API, port signature, or return shape while it still has no callers; migrations and deploy ordering; persisted
formats; env-var, CLI-flag, route and metric names once they are configured against; a new *direct* dependency; growth
in loaded-instruction context at any of the context-budget concern's three tiers. Deferring these is the expensive
choice even when the change itself is one line.

Match the principle, not the list. A comment typo in a `.proto` is T3 — the file is on the list, the finding doesn't
compound. An awkward signature on a new Python port is T2 even though "naming" reads as T3, because callers accrue.

**T3 — costs the same to fix in six months as it does today.** Comment and docstring accuracy, stale or inconsistent
prose in design docs, naming on surfaces nothing has bound to yet, decomposition, type-annotation specificity, test
shape and coverage gaps, style-guide citations, design taste, dead code, duplication. These are real findings; they are
simply not worth interrupting a review for, because nothing compounds while they sit.

T3 is the default for the concerns that permit one — general code quality and context budget. A finding under those is
T1 or T2 only if you can name the failure it causes now, or the thing that gets harder later. "This could confuse a
future reader" is T3. "This is inconsistent with the surrounding code" is T3.

### Routing

| tier | at review time | at merge time                                           |
| ---- | -------------- | ------------------------------------------------------- |
| T1   | inline comment | issue (`untriaged` + `bug`) if it reached `main` anyway |
| T2   | inline comment | issue (`untriaged` + `bug`) if it reached `main` anyway |
| T3   | silent         | one per-PR digest issue, labelled `untriaged`           |

A T3 finding is not posted inline, not summarised, and not mentioned in passing. Merge time is a fresh pass over what
actually landed, so a T3 the author fixed along the way is never filed at all — which is why holding them costs nothing
and reporting them costs a review.

T3s are filed as a single digest per PR rather than one issue each: at roughly 11 per PR and ~100 PRs a month, one issue
per finding buries the human backlog it shares a tracker with.

### Deferring a finding

Any review thread — the bot's or a human's — is deferred by **replying with `FOLLOWUP` alone on the first line**, then
what should happen. The merge-time pass turns each such thread into its own `followup` issue, so the thread can be
resolved and the PR merged without losing the finding:

```
FOLLOWUP
The clamp belongs on the servicer, not the backend, and moving it needs the batch-size cap first.
```

The marker is detected on the first non-blank line of a reply, optionally bolded and optionally followed by a colon.
`FOLLOWUP` further down the comment, or with text after it on the same line, does not count — that keeps prose *about*
the convention from tripping it.

A thread that is resolved without a `FOLLOWUP` is taken as dealt with or declined, and the merge-time sweep is told not
to re-file it. If you want a finding tracked, the marker is the only thing that does it — "let's do this later" in prose
reads as declined. A thread left *unresolved* is not suppressed either way; the sweep may re-derive it.
