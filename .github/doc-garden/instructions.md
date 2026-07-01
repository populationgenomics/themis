### Doc-gardening pass

Audit the repository's tracked documentation against the real code and tree, and fix the drift you can confidently fix —
in place. You are auditing the current tree, not reviewing a diff. Edit files directly; do not touch pull requests and
do not run mutating git (a later workflow step publishes your edits).

The primary audience for these docs is a model reading them as context (`CLAUDE.md` "Docs"). Drift wastes that context,
so the bar for an edit is "this is wrong, stale, or misleading", never "I would phrase it differently".

Surfaces to audit: every tracked Markdown file in the repo — all of it is project documentation (`docs/`, each
`README.md`, `GLOSSARY.md`, `CLAUDE.md`, `.claude/rules/`, `.github/**/*.md`). Enumerate with `git ls-files '*.md'`; do
not maintain a path allowlist that drifts as docs move.

For the link and anchor classes, a report from `tools/check_links.py` (run over the current tree) is provided in your
prompt: every broken relative link and dead `#anchor` across all tracked Markdown, as `file:line -> target  (reason)`.
Use it as the candidate list rather than checking links by hand; treat each hit as something to judge, not a confirmed
defect — a design doc may deliberately link a sibling it proposes but has not yet authored (see the plans/design rule
below).

#### Drift to find and fix

- **Broken relative links** — a `[text](path)` whose target file does not exist. Repoint to the intended target if you
  can locate it (a moved or renamed file); otherwise leave it and report — do not invent a file.
- **Dead path references** — an inline `` `path/to/file` `` that reads as a file or directory reference but does not
  resolve in the tree. Same rule.
- **Stale section / anchor references** — "see §4", a `#heading-anchor`, or a named-section reference pointing at a
  heading that no longer exists in the target. Repoint or report.
- **Behavioural claims the code contradicts** — a doc stating the code does X (a flag, default, endpoint, schema field,
  CLI argument, file layout) where the code does Y. Read the code to confirm before changing. Where the correct
  statement is unambiguous from the code, fix it; where reconciling them is a judgement call, handle it by the
  confidence tiers below rather than skipping it outright. "Unknown" is a valid result — never manufacture a fix.
- **Stale status markers** — "TODO", "coming soon", "not yet built", "planned" for something the tree shows now exists
  (or the reverse). Update the status; do not delete a TODO that is still open.
- **Terminology drift** — a term used in a sense that conflicts with its `GLOSSARY.md` or `docs/PRODUCT.md` definition,
  or a deprecated term the glossary has since replaced. Align to the canonical definition; do not redefine the term.

#### Plans and design proposals describe a target state

Docs under `docs/plans/`, and any section describing a not-yet-built design, state intended behaviour, not current
reality — their divergence from the code is the point, not drift. There, skip the two classes that assume the doc tracks
current code: a **behavioural claim** the code "contradicts" may just be unbuilt, and a **status marker** ("planned",
"not yet built") is correct by construction — leave both, do not report. A **path reference** that does not resolve may
be a file the plan proposes to create — leave it too. Still fix the build-independent drift (broken links and stale
cross-references between docs, terminology): a broken link is wrong whatever the build status. Judge by content, not
just path — a `docs/design/` doc describing a *built* system is current; garden it normally. When unsure whether a claim
is unbuilt or wrong, leave it and report.

#### Confidence: fix, propose, or report

Sort every drift you find into one of three tiers by how sure you are of the correct text. The PR is the handoff: an
author points an agent at it, resolves the open questions, and merges — so a flagged best guess is useful, a silent
wrong guess is not.

- **Fix** — the correct text is unambiguous from the code or tree (a moved file's new path, a "not yet built" marker for
  something that now exists, a renamed heading). Edit it in place; no flag needed.
- **Propose (best guess)** — the drift is real and the tree shows the fix *direction*, but the exact wording is a
  judgement call (how to classify a new top-level directory in a structure table; whether a named tool should now read
  differently). Make your best edit *and* flag it in the PR description's best-guess section with the open question, so
  the author can correct it on review.
- **Report only** — the correct content is not knowable from the tree: a broken link whose target exists nowhere, a
  contradiction whose resolution needs a decision you cannot derive. Do not invent a target or a fact. Leave the file
  and list it in the PR description's needs-input section.

The line between *propose* and *report* is whether a best guess would be *grounded* (the directory demonstrably exists,
so a row describing it is grounded) or *manufactured* (a link target that exists nowhere, so any path is invention).
When you cannot tell which, report — never manufacture.

#### How to fix

- Size the edit to the drift: a one-character link fix or a multi-sentence rewrite, whichever the drift needs. Rewriting
  a passage is the right fix when its content is wrong, stale, or confusing *because it is outdated* — do it, having
  verified the new wording against the code or tree first.
- Do not make taste-driven edits: no reflowing, reordering, or rewording prose that is already correct and clear. That
  is review noise, not gardening.
- Do not author net-new material: no new docs, and no new sections for functionality that was never documented (that
  needs an author's design intent). Restructuring an *existing* section to fix drift is in scope.
- One drift, one fix — do not bundle unrelated changes.

#### Your final message is the PR description

The workflow publishes your edits as a pull request and uses your final message verbatim as the body (after a fixed
preamble). Write it as the PR description:

- A one-line summary of what you changed.
- `## Best-guess fixes (please verify)` — one bullet per *propose*-tier edit: `file:line` — what you changed and the
  open question for the author. Omit the section if there were none.
- `## Found but not fixed (needs your input)` — one bullet per *report*-tier item: `file:line` — what is wrong and what
  you need to resolve it. Omit if none.

If you made no edits at all, say so plainly: the workflow then opens no PR, and any report-only items remain only in
this execution log.
