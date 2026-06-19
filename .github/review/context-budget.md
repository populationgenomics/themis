### Concern: context budget

Instruction files auto-load into every Claude session — a cost paid in
tokens and attention on each one, invisible in a normal diff. Report when
a PR materially changes that footprint. Informational, not a problem to
fix; the value is the number.

Count prose reaches ("read `docs/PRODUCT.md` before designing"), not just
`@`-imports. Three tiers, by when the cost lands:

- **always-on**: `CLAUDE.md` + its transitive `@`-imports — every session,
  any task. The expensive tier; weight it most.
- **path-conditional**: a `.claude/rules/*` + its `@`-imports, when its
  `paths:` glob matches a touched file.
- **instruction-conditional**: files `CLAUDE.md` / a rule says to read in
  prose, keyed on task intent. Per-task, not every session.

Estimate tokens as chars/4 (order of magnitude and delta, not precision).
After-size from the head file; delta from the diff, or the whole file when
a reach is newly added/removed.

Report when: always-on moves ≳300 tokens, or any file enters/leaves the
always-on set (new/removed `@`-import or rule) at any size; or a
conditional trigger moves ≳1000 tokens, or a rule / sizable prose reach is
added or removed. Otherwise silent.

Post one inline note on the line driving the change (the `@`-import / "read
this" line, else the grown file), naming the tier, new total, and delta —
e.g. "Context budget: this `@`-import adds ~2,600 tokens to always-on
context (now ~3,700, was ~1,100), paid every session. If it's task-specific,
a path-scoped `.claude/rules/` entry would load it on demand instead."
