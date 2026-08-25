# SVCv4 reference oracle

On-demand developer tool that proves the `themis.svcv4` combining engine still matches the ClinGen SVCv4 pilot
calculator's scoring logic. It fetches the calculator's scoring bundle at runtime, extracts its cap tables and pure
combining functions, and diffs them against our library. Run it by hand after touching `themis/svcv4/**` or the
reference data; the diff run is **not** wired into CI, because it needs the network and a Node toolchain. Its own tests
need neither, and run in CI.

## What it validates (cookie-free)

The calculator's scoring bundle (`calc-phase3.js`) is a public, cookie-free asset. The tool runs the bundle's own
functions and reads its own constants, then compares:

1. **Cap tables** — `EVIDENCE_CODE_CAP`, `EVIDENCE_CONCEPT_CAP`, `EVIDENCE_CATEGORY_CAP`, and
   `EVIDENCE_CONCEPT_TO_CODES` against the loaded reference's per-code ranges, concept caps, category caps, and
   concept-to-codes mapping.
1. **Banding** — `calClassification` against `scoring.band_for_total` across every classification edge (Benign / Likely
   Benign / VUS low-mid-high / Likely Pathogenic / Pathogenic), probing both sides of each boundary.
1. **Missense-vs-splice max path** — `getMaxOrMin` against `scoring.select_path` across the tie / both-negative /
   higher-wins cases.
1. **Clamping** — `applyConstraint` against `scoring.clamp`, including the unbounded (`NA`) sides.

It prints a per-row report and exits non-zero on any unexpected finding — a real diff, or a pinned divergence that has
silently resolved (see below) — so it doubles as a manual gate.

## What it cannot validate (out of scope)

- **Per-code point values.** The points a curator enters per evidence code are not in the scoring bundle, so full
  end-to-end scoring (evidence in, class out) is out of scope here; this tool validates the combining machinery, not the
  point inputs.
- **The gene-disease-validity gate.** `calPatho()` maps the summed points straight to a band and stops, so the
  calculator holds no gate for `scoring.apply_gate` to diff against.

## Expected divergences

The library reads its caps off the supplements, so a cap the calculator states and no supplement does is a divergence by
construction rather than a transcription slip. Three are of that kind:

- `POP` (concept): no supplement bounds the sum of the two population codes. The calculator floors it at −10.0, the sum
  of the two per-code floors; the library states only the ceiling, which SM3 implies by making both codes benign-only.
- `SPL_PRD_SPA` (concept): the supplements cap the SPL_PRD + SPL_SPA combine once per colour of the splice tree rather
  than once overall — 0.0 to +6.0 on yellow, −1.0 to +6.0 on orange, −2.0 to +2.0 on blue, −3.0 to 0 on violet. The
  library carries their union; the calculator's −8.0 floor sits below every one of them.
- `HOD` (category): the calculator holds an unbounded category cap under this name, which no supplement names and
  nothing in the library scores. The pin states the absence — there is no value on our side to pin.

Per-code caps that diverge from the calculator:

- `CDS_PRD`: SM8 para 32 and SM10 para 32 state the range as −1.0 to +6.0, and SM8 para 31 and SM13 para 100 award the
  −1.0 leaf. The calculator floors it at −4.0, two points below anything the coding workflow can reach.
- `CLN_DNV`: SM4 sums the point values of all proband de novo occurrences and states no bound on that sum; its +7.0 is
  the highest weight for one proband, not a ceiling on the total. The calculator bounds the sum at 12; the library
  follows SM4 and leaves the upper side unbounded, so the calculator is the side expected to move.
- `LOC_SEG`: SM5 states the code range as 0.0 to +4.0 twice, in its title block and para 34, while para 33 recommends
  −4.0 for non-segregation in AD / AR-homozygous / X-linked contexts. The calculator follows the range; the library
  follows para 33. The pin stands until the supplement is reconciled (see `docs/design/evidence-interfaces.md`).
- `NUL_PRD`: every path the code appears on floors at 0.0, and its subgenic ceilings are +4.0 (SM9 para 72) and +6.0
  (SM8 para 11, SM15 para 11). The whole-gene-deletion +10.0 tier (SM13 para 13) is admitted by the `NUL_PFD` category
  cap, the bound that path is built under, so the code range does not have to reach it. The calculator states −4.0 to
  +6.0.
- `POP_HMZ`: SM3 states per-occurrence tariffs (para 72) and no code-level bound anywhere, so the benign side
  accumulates and the library leaves it unbounded. The calculator floors it at −4.0.

Each is pinned in one of `oracle.py`'s three pin maps — `EXPECTED_CODE_DIVERGENCES`, `EXPECTED_CONCEPT_CAP_DIVERGENCES`,
`EXPECTED_CATEGORY_CAP_DIVERGENCES` — reported as `EXPECTED`, and does not fail the run. The pin is exact: if either
side of a pinned divergence changes shape it becomes an unexpected `DIFF`; if the two sides converge so the divergence
no longer exists it is reported as `RESOLVED` (remove the pin); a pin naming a key absent from both tables is flagged
too. All three failure modes fail the run, so the reconciliation stays honest.

## Licensing

`calc-phase3.js` is © Baylor College of Medicine (BRL). Its source is never vendored, quoted or committed to this
(publicly-mirrored) repository: the tool fetches it and evaluates it in memory, never writing it to disk.

That leaves one question — where the library's own numbers come from — and the rule is:

- Every per-code range and every cap is the supplements', wherever a supplement states one; the reference cites the
  passage at the value.
- Where a supplement gives a direction but no magnitude, the calculator's value is the only executable statement of one,
  and the library carries it. `CLN_CCS` is the only such code: SM4 awards +4.0 at §23 and at §13 names a benign
  direction with no value attached, so the range is the calculator's `EVIDENCE_CODE_CAP`. It is deliberately *not*
  pinned — the oracle holds both sides to it, so either side moving fails the run — and stands until a supplement states
  a magnitude of its own.
- Every departure from the calculator is pinned in `oracle.py`, which is why the pin maps name the calculator's side of
  each: without it the oracle cannot tell a deliberate departure from drift, and stops being a gate. Each pin is listed
  above.

No other calculator constant belongs in a tracked file.

## Running it

```
uv run python tools/svcv4-oracle/oracle.py
```

Requirements: Node 24+ on `PATH` (the reference side runs in Node under its `--permission` model; zero npm dependencies)
and network access to `calculator.clinicalgenome.org`. The Python side imports `themis.svcv4` from the repo.

## How it works

`oracle.py` (Python) owns the diff and the probe inputs; `reference_oracle.js` (Node) owns the reference. Python sends
the probe inputs to the Node oracle on stdin; Node fetches the bundle, evaluates it, and returns the cap tables plus the
reference functions' answers as JSON; Python scores the same inputs through `themis.svcv4` and diffs.

Node evaluates the bundle in a Node `vm` context with no DOM: every jQuery/DOM reference in the bundle sits inside a
function the oracle never calls, so no `jsdom`/`jQuery` is needed — the tool has no npm dependencies. The extraction
lifts the bundle's top-level symbols by name (`EVIDENCE_CODE_CAP`, `getMaxOrMin`, `calClassification`,
`applyConstraint`, …); a rename or restructuring in a future bundle revision makes the extraction fail loud with a
shape-change error rather than silently diffing stale values. A non-200 fetch, a fetch timeout, an over-size response, a
login-gated HTML redirect, or a missing marker identifier also fails loud.

## Trust and sandboxing

The bundle is untrusted remote code. It is evaluated in a fresh `vm` context whose global exposes only inert DOM stubs
and a quiet console — no `process`, `Buffer`, `fetch`, or timers, and no dynamic-`import()` callback, so a compromised
origin cannot trivially reach the host. This is **not** a hard security boundary: Node's `vm` shares intrinsics with the
host realm, and a determined bundle can walk the passed-in global's constructor chain back out. The residual trust is
the ClinGen origin and its TLS; the sandbox removes the ambient Node globals that would otherwise make a compromised
origin or a TLS-MITM a one-line RCE on the developer's machine.
