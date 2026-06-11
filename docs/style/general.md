# Code style — language-agnostic

Principles independent of language; the language layers build on it
([`python.md`](python.md), and `typescript.md` to come). On conflict,
the language doc wins for its language. Code properties only —
behavioral directives (push back, resist the minimal fix) live in
[`../../CLAUDE.md`](../../CLAUDE.md).

## Fail loud; never silently degrade

Raise on missing or malformed data; don't paper over it with `x or []`,
`x or {}`, or a bare `if x:` that skips the real case — that turns a
missing input into a silent wrong answer. Validate and fail early.

"Unknown" is a valid analysis *result* ([`../PRODUCT.md`](../PRODUCT.md)
§6), not a license for code to swallow missing inputs: the agent may
conclude "unknown"; the code feeding it must not manufacture that from
absent data.

## No code archaeology

Write code and comments as if the current shape always existed. No
history-narrating comments ("removed X", "no longer polls", "switched
from Y") or commented-out former implementations. The rationale for
*why* the current shape was chosen should be documented in design docs
rather than in comments.
