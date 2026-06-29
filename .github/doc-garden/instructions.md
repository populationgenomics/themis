### Doc-gardening pass

Audit the repository's tracked documentation against the real code and tree, and
fix the drift you can confidently fix — in place. You are auditing the current
tree, not reviewing a diff. Edit files directly; do not touch pull requests and
do not run mutating git (a later workflow step publishes your edits).

The primary audience for these docs is a model reading them as context
(`CLAUDE.md` "Docs"). Drift wastes that context, so the bar for an edit is "this
is wrong, stale, or misleading", never "I would phrase it differently".

Surfaces to audit: every tracked Markdown file in the repo — all of it is
project documentation (`docs/`, each `README.md`, `GLOSSARY.md`, `CLAUDE.md`,
`.claude/rules/`, `.github/**/*.md`). Enumerate with `git ls-files '*.md'`; do
not maintain a path allowlist that drifts as docs move.

#### Drift to find and fix

- **Broken relative links** — a `[text](path)` whose target file does not exist.
  Repoint to the intended target if you can locate it (a moved or renamed file);
  otherwise leave it and report — do not invent a file.
- **Dead path references** — an inline `` `path/to/file` `` that reads as a file
  or directory reference but does not resolve in the tree. Same rule.
- **Stale section / anchor references** — "see §4", a `#heading-anchor`, or a
  named-section reference pointing at a heading that no longer exists in the
  target. Repoint or report.
- **Behavioural claims the code contradicts** — a doc stating the code does X (a
  flag, default, endpoint, schema field, CLI argument, file layout) where the
  code does Y. Read the code to confirm before changing. Fix only when the
  correct statement is unambiguous from the code; if reconciling them needs an
  author's intent, leave the doc and report it. "Unknown" is a valid result —
  never manufacture a fix.
- **Stale status markers** — "TODO", "coming soon", "not yet built", "planned"
  for something the tree shows now exists (or the reverse). Update the status; do
  not delete a TODO that is still open.
- **Terminology drift** — a term used in a sense that conflicts with its
  `GLOSSARY.md` or `docs/PRODUCT.md` definition, or a deprecated term the glossary
  has since replaced. Align to the canonical definition; do not redefine the term.

#### Plans and design proposals describe a target state

Docs under `docs/plans/`, and any section describing a not-yet-built design,
state intended behaviour, not current reality — their divergence from the code
is the point, not drift. There, skip the two classes that assume the doc tracks
current code: a **behavioural claim** the code "contradicts" may just be
unbuilt, and a **status marker** ("planned", "not yet built") is correct by
construction — leave both, do not report. A **path reference** that does not
resolve may be a file the plan proposes to create — leave it too. Still fix the
build-independent drift (broken links and stale cross-references between docs,
terminology): a broken link is wrong whatever the build status. Judge by
content, not just path — a `docs/design/` doc describing a *built* system is
current; garden it normally. When unsure whether a claim is unbuilt or wrong,
leave it and report.

#### How to fix

- Size the edit to the drift: a one-character link fix or a multi-sentence
  rewrite, whichever the drift needs. Rewriting a passage is the right fix when
  its content is wrong, stale, or confusing *because it is outdated* — do it,
  having verified the new wording against the code or tree first.
- Do not make taste-driven edits: no reflowing, reordering, or rewording prose
  that is already correct and clear. That is review noise, not gardening.
- Do not author net-new material: no new docs, and no new sections for
  functionality that was never documented (that needs an author's design intent).
  Restructuring an *existing* section to fix drift is in scope.
- One drift, one fix — do not bundle unrelated changes.

#### Report what you did not fix

For drift you found but did not fix (target genuinely missing, behavioural
contradiction needing an author's intent), leave the file untouched and list it
plainly at the end of your run: file, location, what is wrong, and why you left
it. If you fixed everything you found, say so. These notes are the run's record —
they are read from the execution log, not posted to a PR.
