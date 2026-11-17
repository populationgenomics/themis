# Writing tests

This guide states what a test asserts, what it may depend on, how it reads, and what its data may contain, independent
of language. Runner mechanics stay with each language: pytest in [`python.md`](python.md#pytest) and `bun test` for
`apps/web`. Much of the guidance is distilled from Google's [Testing on the Toilet](https://testing.googleblog.com/)
series, and the headings track its post titles closely enough to search.

## A test earns its place by the regression it catches

Before keeping a test, name the caller-observable change in behaviour that would make it fail. If no such regression
exists, the test is decoration. If the only change that breaks it moves code around without changing what a caller sees,
the test pins internal structure ([below](#test-at-the-surface-a-caller-uses)). If an existing test already catches that
same regression, the new test duplicates coverage without adding signal. Tests that look like coverage commonly fail
this bar in one of three ways:

- Production logic lands with no test that executes it.
- A test exercises the new code path, but its assertion would also pass against the old behaviour.
- The assertion checks something too weak to catch the regression, such as asserting that the return value is not `None`
  or that a call did not raise when an unexpected exception was never the risk.

Writing the test before the change and watching it fail proves that the assertion catches the bug. When refactoring test
code, invert that loop: break the production code first, confirm that the expected assertions fail, and keep the suite
red while restructuring the tests. Any test that starts passing mid-refactor lost its check during the edit.

Line coverage shows what has no test at all, not whether covered lines are tested. Dividing by one covers a division
without testing zero, and evaluating the left side of a short-circuit condition covers the line without exercising the
right side. Read uncovered lines to find missing scenarios, and never write a test just to raise a coverage percentage
or to pin a detail no caller can observe, such as wording in a log message or a tuning constant. If coverage is gated at
all, gate the lines a change touched rather than a repository-wide threshold.

## Test at the surface a caller uses

Test a unit through the interface its callers use rather than the private functions behind it. For a library, that
surface is its public API. For a service, it is the RPC interface over a loopback server. For a command-line tool, it is
the command itself: arguments and input files in, standard output, exit status, and written files out.

Splitting a tool into helpers and writing a test per helper produces a large suite without showing that the command
works. First, a helper test has no independent ground truth: the author works out the expected value by mentally running
the helper, which records the same assumption in both the code and the test. At the caller surface, the expected output
follows from the user's task instead of the internal decomposition.

Second, helper tests freeze how the code is split up. A refactor that preserves every caller-visible behaviour should
leave assertions untouched even when test setup changes; when a refactor forces rewrites across the suite, the tests
were asserting internal structure. A helper with a single caller whose paths are all reachable through that caller needs
no tests of its own, and testing it in isolation can pass even when the caller wires it up wrongly.

```python
# Bad — tests a single-caller private helper; nothing runs the command
assert cli._parse_row('a\t1') == ('a', 1)

# Good — invokes the command as a user does and checks its observable output
exit_code = cli.main(['--input', str(input_tsv), '--output', str(out_json)])
assert exit_code == 0
assert json.loads(out_json.read_text('utf-8')) == [{'id': 'a', 'count': 1}]
```

A helper earns direct tests when driving every case through the outer surface is impractical — such as a parser with
dozens of edge cases or a numerical routine with many branches — or when several callers share it. Even then, feed it
only inputs a caller can actually produce. Warning signs that a suite has crossed into testing structure include direct
tests of single-caller private functions, tests that mock sibling helpers, and test files where nothing invokes the
public entry point.

Validation rules also multiply tests, at one test per rejected state. Before writing a rule and its test, check whether
the type can make the invalid state unrepresentable instead
([`general.md`](general.md#make-the-invalid-state-unrepresentable)).

## Assert invariants, not the current shape

A test that pins today's incidental values — such as the full list of committed migration filenames or an entire
serialised payload — is a *change detector*: it breaks on routine additions without catching defects. State the property
that every valid state must satisfy instead. When production code already enforces that property, pass a violating input
and assert that the enforcement raises, rather than re-checking its valid output with an assertion that can never fail.
Moving from a pinned value to an invariant changes what the test asserts, not whether the behaviour has a test.

```python
# Good — checks the committed directory in a form no new migration invalidates
def test_committed_migrations_are_discoverable() -> None:
  # `discover` raises on a malformed filename or a version gap; non-empty rules out a vacuous pass.
  assert migrate.discover(_MIGRATIONS_DIR)


# Good — tests the enforcement, which can regress, rather than restating today's files
def test_discover_rejects_a_version_gap(tmp_path: pathlib.Path) -> None:
  (tmp_path / '0001_a.sql').write_text('SELECT 1;', 'utf-8')
  (tmp_path / '0003_c.sql').write_text('SELECT 1;', 'utf-8')
  with pytest.raises(ValueError, match='contiguous'):
    migrate.discover(tmp_path)


# Bad — change detector: every added migration has to edit this list
def test_committed_migrations_are_contiguous() -> None:
  migrations = migrate.discover(_MIGRATIONS_DIR)
  assert [(m.version, m.name) for m in migrations] == [
      (1, 'session_context'),
      (2, 'grants'),
      (3, 'litcache_crosswalk'),
  ]
```

- **Narrow assertions.** Assert only the fields relevant to the behaviour under test. Whole-object equality checks every
  unrelated field as well, so adding a column to a record breaks tests about unrelated workflows. Keep at most one
  whole-object equality check per type to cover basic construction, and narrow the rest.
- **Pin values only when policy freezes them.** Because an applied SQL migration is never edited in place, pinning its
  statement count catches an accidental edit rather than blocking valid changes
  ([`../design/migrations.md`](../design/migrations.md)). Ask whether a valid future change could alter the value; if it
  can, assert the invariant instead.
- **Compare computed floats with a tolerance.** Floating-point values produced by arithmetic carry rounding error and
  should be checked with `pytest.approx` rather than `==`. Numbers parsed directly from a fixture without arithmetic
  compare exactly.
- **Assert results, not call sequences.** Asserting the exact sequence of calls on mocked collaborators repeats the
  implementation and can pass while the output is wrong ([below](#test-doubles-in-order-of-fidelity) covers when
  verifying a call is warranted).
- **Bind cross-cutting enumerations with an exhaustiveness test.** When every member of an enum must be named, labelled,
  and rendered across several surfaces, one test iterates over every member so adding a case fails until every consumer
  handles it (for example, the `AnalysisInputs` check described in
  [`../design/review-policy.md`](../design/review-policy.md)).

## One behaviour per test, named for it

Write each test around a single behaviour rather than an entire method. If a withdrawal method both displays a notice
and sends an email when the balance drops low, give those behaviours separate tests; naming a test after the method name
alone usually signals that several cases were bundled together. Calling the system under test a second time after an
assertion is another warning sign, because a failure in the first check hides whether the second scenario still works.
Multi-step calls belong in one test only when the sequence itself is the behaviour under test, such as a state
transition, a compare-and-swap retry, or an open-compose-send flow.

Name each test after the scenario and the expected outcome so a failure log states what broke and the test names read as
a specification of the unit. If a test name needs "and" to describe two outcomes, split the test.

Failures should let an engineer diagnose the problem without adding print statements and rerunning. Prefer assertions
that print values on failure: in pytest, `assert a == b` formats a structural diff whereas `assert result.ok` prints
only `assert False`, and `pytest.raises(ValueError, match=...)` guards against catching an unrelated `ValueError` from
setup. When a test depends on an earlier step succeeding (such as receiving a response before inspecting its body),
assert that precondition first with a clear message so setup failures are not misread as assertion failures in the
behaviour under test.

## A test reads without its reader leaving it

Because test code has no test suite checking it, readers verify a test by reading it from top to bottom, which requires
keeping everything the assertion depends on in view. Production code avoids duplication, whereas test code favours
explicitness (often called DAMP, for descriptive and meaningful phrases): repeat a few lines of setup rather than hiding
cause and effect behind shared mutable state, a helper that runs the action and asserts internally, or a `for` loop
around assertions. In pytest, `@pytest.mark.parametrize` keeps literal inputs paired with literal expected outputs and
reports each failing row independently, whereas a loop inside a single test stops at the first iteration.

```python
# Bad — computes the expected value with string concatenation, and relies on a hidden default balance
account = make_account()
assert format_payout(close_account(account)) == f'${account.balance}.00'

# Good — sets the asserted input in the test body and checks against a literal expectation
account = make_account(balance=150)
assert format_payout(close_account(account)) == '$150.00'
```

- **Keep logic out of tests.** Write inputs and expected outputs as literals, or reference named constants exported by a
  fixture; computing expectations in the test risks duplicating a production bug. If non-trivial formatting or traversal
  is unavoidable, extract a dedicated test helper and test that helper directly.
- **Set asserted values in the test body.** Leave routine scaffolding to fixtures and builders, and pass every value an
  assertion depends on explicitly in the test body — even when the builder's default happens to match — so a reader
  never has to jump two hundred lines away to see why the expected number is right.
- **Use one keyword builder rather than a helper per combination.** Provide a zero-argument factory (or a baseline
  dataclass/proto paired with `dataclasses.replace`) that populates required fields with sensible defaults, letting each
  test override only the fields relevant to its case. Positional setup helpers that gain a parameter per new test force
  churn across unrelated tests whenever a model grows.
- **Pick input values that can expose bugs.** Avoid type defaults such as `0` or `""` where an uninitialised or dropped
  field would produce the same value, and use distinct values for distinct arguments of the same type so swapped
  parameters fail the test. Cover empty inputs, boundary values, and representative complex inputs in separate tests or
  parametrised rows.
- **Parametrise inputs to one behaviour, never distinct behaviours.** Adding flags or branches to a parameter table to
  steer different assertions obscures which behaviour broke and introduces branching logic into the test itself.
- **Assert the property when order or wording is incidental.** Tests that break on set iteration order, header ordering,
  or routine wording tweaks in error messages should assert unordered membership, a structural predicate, or a targeted
  substring match instead. When several suites need the same domain check, add a shared assertion helper under
  [`themis/testing/`](../../themis/testing/).

## Deterministic by construction

A flaky test — one that passes and fails on the same commit — erodes trust in the suite faster than a deterministic
failure, because engineers learn to shrug off red runs and cannot trust green ones. Diagnose the root cause rather than
rerunning: CI configures no automatic retries, and a flaky test may be quarantined only alongside a tracked issue.
Hermeticity ([below](#tests-run-with-no-cloud-and-no-network)) eliminates external network and cloud variance; the rules
below govern variance inside the process:

- **Never use `sleep` to wait for asynchronous work.** Coordinate on an explicit event, queue, or condition with a
  generous timeout, which finishes immediately on fast runs and bounds failures when work stalls. When testing
  concurrent interleavings against a fake, block the fake on an `asyncio.Event` or `threading.Event` that the test
  releases explicitly, and keep single-threaded logic tests separate from concurrency tests.
- **Pass time, randomness, and environment as explicit inputs.** Code that reads the system clock directly cannot be
  tested cleanly at midnight or expiry boundaries; accept a timestamp or clock callable instead. Apply the same pattern
  to random seeds, environment variables, and hostnames.
- **Isolate every test's state.** Suites must tolerate arbitrary execution order and parallel workers, so each test
  creates its own files under `tmp_path`, instantiates a fresh in-memory backend, and restores any mutated process state
  through `monkeypatch` or fixture teardown.

## Test doubles, in order of fidelity

A test double's fidelity is how closely its behaviour matches the production dependency. Prefer, in descending order:
the real implementation, a [*fake*](../../GLOSSARY.md#testing) (a lightweight working implementation such as an
in-memory store), and last a *stub* or *mock* whose responses are scripted for a single test. Use the highest-fidelity
option that stays hermetic, either in-process or against a gated local container
([below](#tests-run-with-no-cloud-and-no-network)). Any ad-hoc stand-in whose return values are hard-coded inside the
test counts as a stub or mock regardless of class name, because it encodes only what the test author assumed the
dependency would do.

```python
# Bad — scripts a mock and verifies a read call rather than checking the outcome
store.get_item.return_value = Item(id='x', archived=False)
archive_item(store, 'x')
store.get_item.assert_called_once_with('x')

# Good — runs against a working in-memory fake and asserts the resulting state
store = FakeItemStore({'x': Item(id='x', archived=False)})
archive_item(store, 'x')
assert store.get_item('x').archived is True
```

- **Fake at the lowest boundary.** Fake the storage or transport port rather than every class layered above it, which
  runs more real code and maintains fewer doubles. When an external client cannot be faked directly, put a narrow port
  interface around it and keep the live adapter thin, following the service pattern in
  [`../design/services.md`](../design/services.md).
- **Run a shared contract suite against both the fake and the real adapter.** Write one behavioural test suite against
  the port interface and execute it against both implementations, gating the live adapter on its local container
  fixture. The sheaf storage contract in [`themis/sheaf/tests/`](../../themis/sheaf/tests/) demonstrates this pattern
  across the local-directory backend and the GCS emulator.
- **Do not mock third-party types directly.** Mocking an external library bakes in today's reading of its API; if an
  upgrade changes a return status or exception type, mocked tests stay green while production breaks. Use the real
  library, an upstream-provided fake, or a thin wrapper owned by this repo whose own integration tests run against the
  real library.
- **Verify state-changing commands, never queries.** Asserting that a getter or lookup was called proves nothing about
  how its return value was used and breaks if caching or refactoring alters the read path. Stub queries to supply inputs
  and assert on returned values or mutated state, reserving call verification for side effects that *are* the contract —
  such as sending one notification, making one upstream RPC per cache miss, or acquiring an advisory lock before
  writing.
- **Match only relevant arguments when verifying a call.** Asserting every argument on a verified call couples unrelated
  tests to every parameter addition and obscures which argument matters to the scenario; use wildcard matchers for the
  rest.
- **Watch for over-mocking.** Needing more than one or two mocked collaborators, scripting multi-method call chains, or
  reading production code line by line to understand why a test sets up its mocks indicates that the test is coupled to
  implementation details rather than a clean boundary.

## Tests run with no cloud and no network

Pull-request CI jobs hold no cloud credentials and should never depend on live external services, because a required
check that skips its assertions appears indistinguishable from a passing run. Tests run hermetically using shared
scaffolding in this repository so that they exercise real wire and serialization contracts rather than ad-hoc mocks:

- **Service behaviour tests** drive each servicer over a real `grpc.aio` server on loopback using
  [`themis/testing/in_process_grpc.py`](../../themis/testing/in_process_grpc.py), exercising real protobuf
  serialization, gRPC status codes, and interceptors instead of calling methods with synthetic contexts.
- **Service backends** use the in-memory fixture implementation seeded explicitly by each test; fixture constructors
  never fall back to default sample data, so passing `{}` states that the store is intentionally empty
  ([`../design/services.md`](../design/services.md)). HTTP upstream adapters replay committed recordings of real
  upstream responses rather than hand-crafted stubs.
- **Authorization and session resolution** run through [`themis/testing/auth.py`](../../themis/testing/auth.py) for
  interceptor-gated services (and [`fixture_session.py`](../../themis/clients/auth/tests/fixture_session.py) where a
  smoke-test handler resolves sessions directly). The fixture session resolver returns a binding or raises on an unknown
  token, never returning `None`, and exports caller metadata and session constants that tests pass and assert against in
  place of string literals.
- **Unused dependencies fail on contact.** When a scenario should not touch an upstream or secondary backend, wire in a
  double that raises immediately if called so accidental coupling fails loudly.
- **Web tests run offline** against the fixture backend (`THEMIS_BACKEND=fixture`). Exercise UI behaviour through the
  rendered component — render, trigger user actions, and assert what appears in the DOM — rather than calling event
  handlers directly, since a direct handler call still passes when the button wired to it is disabled.

When a faithful local stand-in requires an external local process — Postgres, `fake-gcs-server`, or Linux `bubblewrap` —
gate the test explicitly. Container-backed tests request `docker_daemon` from the root
[`conftest.py`](../../conftest.py); sandbox tests carry the `sandbox` markers registered in
[`pyproject.toml`](../../pyproject.toml) under `strict_markers = true`, turning a typo in a marker name into a
collection error rather than a silent deselection. Both gates skip on developer machines when the dependency is absent
and raise `RuntimeError` when `CI` is set, where the hosted runner is expected to provide it. When no separate CI job
guarantees coverage, a missing binary fails the test run outright: the sheaf suite requires `git` on `PATH` and fails
without it ([`../design/sheaf.md`](../design/sheaf.md)).

## A fixture is an independent oracle

Generating test fixtures with the code under test only verifies that the code can read back its own mistakes; any bug
shared by the writer and reader passes undetected. The same blind spot arises when a helper test's expected output is
derived from reading the helper ([above](#test-at-the-surface-a-caller-uses)). Build fixtures from an independent
authority instead: the sheaf tests verify interoperability against packfiles and commits produced by a real `git` binary
rather than by sheaf ([`../design/sheaf.md`](../design/sheaf.md)).

Avoid hand-copying schemas or contracts into test fixtures for the same reason. Tests that need live Postgres tables
apply the committed SQL files through the migration runner rather than maintaining duplicate `CREATE TABLE` statements
in test code, ensuring tests cannot drift from the deployed schema
([`../design/migrations.md`](../design/migrations.md)). Likewise, a fake backend stays honest by running the same
contract test suite as the real adapter ([above](#test-doubles-in-order-of-fidelity)).

## Test data reaches the public mirror

Every commit merged to `main` is mirrored 1:1 to the public `themis` repository, so sensitive strings in test fixtures
become public on merge ([`../plans/screen-and-mirror-workflow.md`](../plans/screen-and-mirror-workflow.md)):

- **Never include strings shaped like CPG or XPG identifiers** (`[CX]PG\d+`) anywhere in test data; use an unmistakably
  synthetic prefix such as `TST123`. Tests of the leak screen itself carry the screen's inline suppression comment and a
  written reason in the file's comment syntax ([`tools/screen/`](../../tools/screen/)).
- **Include nothing derived from or resembling a real clinical case.** Anonymised phenotype narratives, referral-letter
  prose, real family structures, nucleotide runs longer than a few dozen bases, and cohort-sized variant tables all risk
  leaking case details or triggering false positives. Construct small, clearly synthetic inputs instead.
- **Omit internal operational details** — staff lists, internal issue keys used as primary context, and private bucket
  or project names — along with anything unsuitable for a public repository.

The PR-time LLM review ([`.github/review/leak-screen.md`](../../.github/review/leak-screen.md)) flags borderline
fixtures on suspicion, so keeping synthetic data obviously artificial avoids unnecessary review rounds.

## Where tests live

Place each Python package's tests in a `tests/` subdirectory of that package and add the directory to `testpaths` in
[`pyproject.toml`](../../pyproject.toml); pytest collects a new package's suite only after it is listed there
([`../runbooks/add-a-service.md`](../runbooks/add-a-service.md)). Cross-cutting integration and repository-hygiene tests
live under the top-level `tests/` directory. Web tests sit beside the module under test as `*.test.ts` or `*.test.tsx`
files and run via `bun test`. Shared Python test harnesses and doubles live in
[`themis/testing/`](../../themis/testing/) or alongside the client they double, and production modules never import
them.
