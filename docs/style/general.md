# Code style — language-agnostic

Principles independent of language. The language layers build on this doc ([`python.md`](python.md), with
`typescript.md` to come); on conflict, the language doc wins for its language. Before writing or reviewing tests, read
[`docs/style/writing-tests.md`](writing-tests.md) for what a test asserts, what it may depend on, how it reads, and what
its data may contain. Code properties only — behavioral directives (push back, resist the minimal fix) live in
[`../../CLAUDE.md`](../../CLAUDE.md).

## Fail loud; never silently degrade

Raise on missing or malformed data; don't paper over it with `x or []`, `x or {}`, or a bare `if x:` that skips the real
case — that turns a missing input into a silent wrong answer. Validate and fail early.

"Unknown" is a valid analysis *result* ([`../PRODUCT.md`](../PRODUCT.md) §6), not a license for code to swallow missing
inputs: the agent may conclude "unknown"; the code feeding it must not manufacture that from absent data.

## Make the invalid state unrepresentable

Before writing a validation rule, ask why the value it rejects can be expressed at all. A field derived from another
cannot disagree with it. A type that admits only valid values needs no check, and a record nested inside its owner
cannot dangle. Reshaping the type removes the validation rule, the failure it guarded against, and the test that rule
would have needed, for every caller at once rather than only the paths a test reaches. Take a child record that repeats
its parent's identifier and joins to it by reference. That layout admits three errors (the pair disagreeing, the
reference naming nothing, and the repeated name drifting), so it needs three rules and three tests. Nested under the
parent and declaring only what it adds, it admits none.

Fail loud ([above](#fail-loud-never-silently-degrade)) governs the invalid input that can still arrive from a file, a
wire, or a caller outside the type system. Prefer the shape that cannot hold the error; validate what remains, and test
that validation ([`writing-tests.md`](writing-tests.md)).

## Comments

Default to *no* comment. Add one only for a non-obvious *mechanism* or *constraint* a reader cannot recover from the
code — tersely, one line where possible. The *why* (why this shape was chosen) belongs in a design doc or the docstring,
not inline; never duplicate what a doc already states. A design-doc citation (`§N`, `see infrastructure.md §3`) *inside*
a comment that also explains the design is the tell you are restating the doc — cut the explanation; a bare one-line
pointer is fine. No history narration ("removed X", "switched from Y"), no reference to a transient project artifact
("this slice", "this spike", "this PR", "as X lands") — name the durable behavior, not the moment it arrived — no
commented-out code, no persuasion: write as if the current shape always existed. Self-check: a comment that stays true
after the code beneath it is rewritten is describing intent, not mechanism.

```
# Bad — rationale, persuasion, and the design doc already states this
conn = connect(dsn)  # one connection not a pool: pooling adds reconnect
# complexity we don't need yet, only pays off above N writers — the
# whole point of staying simple ...

# Good — one non-obvious fact; the why stays in the doc
conn = connect(dsn)  # single connection: the writer is single-threaded
```

```
# Bad — paraphrases the design doc and cites the section; intent, not mechanism
ipv4_enabled=True,  # Public IP, no authorized networks: reachable only through
# the connector (IAM-gated, TLS, ephemeral certs) — direct connections rejected.
# Private IP would need a VPC + serverless connector (infrastructure.md §7).

# Good — the one non-obvious mechanism, terse
ipv4_enabled=True,  # empty authorizedNetworks ⇒ Cloud SQL refuses direct
# connections; the connector reaches it via an Admin-API ephemeral cert
```
