# Code style — language-agnostic

Principles independent of language; the language layers build on it ([`python.md`](python.md), and `typescript.md` to
come). On conflict, the language doc wins for its language. Code properties only — behavioral directives (push back,
resist the minimal fix) live in [`../../CLAUDE.md`](../../CLAUDE.md).

## Fail loud; never silently degrade

Raise on missing or malformed data; don't paper over it with `x or []`, `x or {}`, or a bare `if x:` that skips the real
case — that turns a missing input into a silent wrong answer. Validate and fail early.

"Unknown" is a valid analysis *result* ([`../PRODUCT.md`](../PRODUCT.md) §6), not a license for code to swallow missing
inputs: the agent may conclude "unknown"; the code feeding it must not manufacture that from absent data.

## Comments

Comment the non-obvious *mechanism* or *constraint*, tersely. The *why* (why this shape was chosen) belongs in a design
doc or the docstring, not inline — and never duplicate what a doc already states. No history narration ("removed X",
"switched from Y"), commented-out code, or persuasion: write as if the current shape always existed.

```
# Bad — rationale, persuasion, and the design doc already states this
conn = connect(dsn)  # one connection not a pool: pooling adds reconnect
# complexity we don't need yet, only pays off above N writers — the
# whole point of staying simple ...

# Good — one non-obvious fact; the why stays in the doc
conn = connect(dsn)  # single connection: the writer is single-threaded
```
