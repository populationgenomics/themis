### Concern: general code quality

Adapted from claude-code-action's
[automatic PR code review template](https://github.com/anthropics/claude-code-action/blob/main/docs/solutions.md#automatic-pr-code-review).
Security is out of scope here — it is handled separately above.

When flagging an entry below that ends with a `Cite:` line, include that line verbatim in the finding so the reader can
jump straight to the policy.

Focus on:

**Correctness and likely bugs.** Off-by-one errors, null/None handling, error cases that aren't covered, race conditions
in non-security-critical code paths, misuse of APIs, mistaken assumptions about iteration order, mutation of arguments,
swallowed exceptions, resource leaks (files, sockets, transactions not closed).

**Design and API choices.** New public APIs with confusing names or signatures; arguments that should be keyword-only;
return shapes that will be awkward to consume; abstractions introduced for a single use site; abstractions that paper
over real differences between callers. Flag designs that will plausibly be harder to evolve than they look.

**Code quality and readability.** Functions that have grown beyond what their name suggests; control flow that's harder
to follow than the underlying problem warrants; names that mislead about behaviour; inconsistency with surrounding
patterns when there's no reason for the deviation. Don't nitpick style that the linter already enforces.

**Performance.** Algorithmic issues (accidental O(n²), repeated work that could be cached, queries inside loops);
excessive allocations on hot paths; loading more data than is used. Flag only where the cost is plausibly material — not
micro-optimisations.

**Tests.** Held to [`docs/style/writing-tests.md`](docs/style/writing-tests.md): a test that catches no regression
(logic without a test, a test that does not exercise the new behaviour, an assertion too weak to fail, coverage that
duplicates an existing test); a change detector pinning the code's current shape where an invariant is what matters —
suggest the invariant; a test that reaches cloud or network; a fixture derived from the code under test; a suite of
helper tests in which nothing invokes the tool, or a test of a single-caller helper; a sleep standing in for
synchronisation, or a test that assumes state another test left; a mock scripting a collaborator the repo has a fake
for, or verifying a query call; a test computing its own expectation, or asserting on a value set in a fixture far from
the assertion. Cite the section:
`[Style: a test earns its place](docs/style/writing-tests.md#a-test-earns-its-place-by-the-regression-it-catches)`,
`[Style: assert invariants, not the current shape](docs/style/writing-tests.md#assert-invariants-not-the-current-shape)`,
`[Style: tests run with no cloud](docs/style/writing-tests.md#tests-run-with-no-cloud-and-no-network)`,
`[Style: a fixture is an independent oracle](docs/style/writing-tests.md#a-fixture-is-an-independent-oracle)`,
`[Style: test at the surface a caller uses](docs/style/writing-tests.md#test-at-the-surface-a-caller-uses)`,
`[Style: deterministic by construction](docs/style/writing-tests.md#deterministic-by-construction)`,
`[Style: test doubles, in order of fidelity](docs/style/writing-tests.md#test-doubles-in-order-of-fidelity)`,
`[Style: a test reads without its reader leaving it](docs/style/writing-tests.md#a-test-reads-without-its-reader-leaving-it)`

**Documentation.** Public APIs or behaviour changed without a docstring update; comments that describe what code did
before, not what it does now; misleading or stale comments in the surrounding context that the PR touches.

**Schema evolution.** The authored proto contracts evolve in place — breaking changes are ruled out (see
`docs/design/proto.md`). Look for:

- Breaking changes to an authored schema: a field deleted without reserving **both** its number and its name; a renamed
  or repurposed field; a narrowed value set (dropped enum member, tightened pattern or range); or a field newly made
  required. These break existing data or older readers and are not allowed. Retiring a field *is* allowed — delete it
  and reserve both its number and its name, rather than leaving it in place as one that must never be set. A PR that
  trips the CI compat gate is not "add an override", it's "model the change compatibly".
- A browser-facing field retired in a single change. Reserving governs reuse, not the transition window: the browser
  seam rejects a JSON key the schema no longer declares, so a field the browser *sends* is retired in two changes — stop
  sending it and deploy, then delete it.
- Database schema changes (column adds / drops / renames, type changes, new tables) without a migration file
  (Alembic-style or whatever the project uses). Relational schemas should never drift between environments.

**Debugging leftovers.** Stray `print()` calls that look like they were added for debugging and forgotten (loud markers
like `print('!!!')`, `print(f'got here: {x}')`, variable-dump prints inside otherwise quiet code); log calls with the
same shape (`logger.info('XXX got to here')`, `logger.debug(f'value = {x}')` left in after troubleshooting);
commented-out blocks that look like work-in-progress rather than deliberate alternatives. `breakpoint()`,
`pdb.set_trace()`, and `import pdb`-class debug entry points are caught by lint, so you shouldn't see those here — but
flag them if the author has worked around the lint somehow.

**Python idiom and style.** This repo follows [`docs/style/python.md`](docs/style/python.md) for the high-level
human-judgement layer of Python style; ruff handles the mechanical parts.

- **Import modules, not symbols.** `from pathlib import Path`, `from dataclasses import dataclass`,
  `from foo.bar import baz_func` — flag all of these. The carved-out exceptions are `typing.*`, `collections.abc.*`, and
  `from __future__ import ...`. Anything else should be imported via its module. Cite:
  `[Style: import modules, not symbols](docs/style/python.md#imports)`

- **Exception handling.** Bare `except:`, overly broad `except Exception:`, silently swallowed exceptions, `raise`
  without `from` when re-raising with added context. Cite:
  `[Style: exception handling](docs/style/python.md#exception-handling)`

- **Resource management.** Files, sockets, locks, connections, or transactions opened without a `with` statement. Cite:
  `[Style: resource management](docs/style/python.md#resource-management)`

- **Mutable global state.** Module-level dicts/lists/sets that get mutated at runtime; caches or registries that should
  be parameter- or instance-scoped. Cite: `[Style: mutable global state](docs/style/python.md#mutable-global-state)`

- **`TYPE_CHECKING` blocks.** Any use of `if TYPE_CHECKING:` — almost always wrong in this repo. The exception is heavy
  type-only imports (`hail`, `torch`, etc.) and those need an inline comment justifying the block. Cite:
  `[Style: no TYPE_CHECKING blocks](docs/style/python.md#imports)`

- **Power features.** `eval`, `exec`, metaclasses, monkey-patching, custom `__getattr__` / `__setattr__` magic, dynamic
  class creation, reflective hacks. Distinct from the security review's RCE concerns — this is the "code is hard to
  reason about" angle. Cite: `[Style: power features](docs/style/python.md#power-features)`

- **Docstring content.** Existing docstrings whose content paraphrases the implementation rather than telling the caller
  what they need to know; missing docstrings on new public APIs (soft — flag as a suggestion, not a blocker). Cite:
  `[Style: docstrings](docs/style/python.md#docstrings)`

- **Type annotation specificity.** Bare `list` / `dict` where the element type carries meaning; over-broad parameter
  types where a `Protocol` or specific `Mapping` would be sharper. Note: `typing.Any` is banned outright by ruff and
  won't reach this prompt. Cite: `[Style: type annotations](docs/style/python.md#type-annotations)`

- **Function decomposition.** Long functions doing multiple distinct things along branch boundaries; not "long because
  of sequential setup" but "long because there are three modes here that want to be three functions". Cite:
  `[Style: function decomposition](docs/style/python.md#function-decomposition)`

Find as widely as this list directs — the tier rule, not this file, decides which findings surface and where. Don't
suppress a finding here because it feels small; tier it honestly and let the routing place it. If the diff is small and
the work is clean, reporting nothing is fine.
