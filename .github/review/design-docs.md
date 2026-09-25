### Concern: design-doc readability

A design doc is written for a human first ([`CLAUDE.md`](CLAUDE.md) "Docs"): under the review policy a second maintainer
who was not in the conversation reviews it, and every later reader of the area starts from it. That reader has read
`docs/PRODUCT.md` and `GLOSSARY.md`, knows nothing about the area, and has to make a review decision from one read on
GitHub, and reviewing means doubting: they must rebuild each decision's reasoning well enough to dispute a step of it.
[`docs/style/design-docs.md`](docs/style/design-docs.md) states what such a reader is owed and the style that delivers
it — read it on the head branch before reviewing. You are the pass that catches what the author's own read missed, while
it is still cheap to fix.

Triggers when the diff adds or changes a file under `docs/design/`. Read the changed doc **whole, as it stands on the
head branch**, not only the hunks — whether a term is introduced before it is used, or motivation precedes mechanism, is
a property of the doc, not of a line — then flag the passages this PR adds or changes. Whole-doc properties — the
take-aways at the top, the order of the argument, detail interleaved with it — are demanded only of a doc the PR adds or
substantially rewrites (more than about half its lines); a small edit to a doc that does not yet meet the guide is not a
finding.

Three things are findings.

The first is a passage where that reader cannot get the decision and the reason for it from one read: the Overview lists
conclusions without the reasoning that connects them, mechanism arrives before its motivation, an area term or an
identifier is used before it is introduced, specifics are interleaved so the reasoning breaks, or an implementation
accident (what a fixture or a test happens to do) is narrated as design.

The second is a passage the reader can follow only by re-reading, or cannot check. A sentence that packs several claims,
compressed noun phrases or stacked asides; a claim the argument rests on with no instance beside it; a decision about a
surface, a flow or a shape with no figure of it; a decision argued without the obvious alternative the reader would
arrive holding, where one exists; an alternative described only by its weaknesses; a Background that lists definitions
instead of teaching them; a run of the machine-register patterns the guide lists.

The third is a passage that restates code — a field list, a path, an env-var name, a function or test name, an error
string, a constant — where a link to the source of truth belongs. Toy data in an example is not this.

Quote the passage and say which of the three it is. This is judgement, not a checklist: the question is whether that
reader comes away able to state the decision, the reason, and the step they would attack to reject it.

T2 by construction. The design doc is the artifact the second maintainer reviews and the one every later reader of the
area starts from, and a readability failure that lands stays until someone rewrites the doc — the doc-gardener does not
touch style by design ([`.github/doc-garden/instructions.md`](.github/doc-garden/instructions.md)) — so its cost climbs
with every read. The finding is worth most before the second maintainer's read.

One finding per problem per doc, anchored to the first passage that shows it — not one comment per sentence. A problem
that shows by absence (no usable Overview, a motivation stated nowhere, a surface or flow with no figure) is anchored to
the heading of the section that should carry it, or to the title line for the Overview. Remediation: rewrite the passage
per the guide, naming the instance, figure or alternative it lacks; for a passage that restates code, replace it with a
link to the source of truth (the proto file, the module, the test).

Do not flag a doc this PR does not change, a doc outside `docs/design/` (`docs/plans/` and the rest are out of scope),
or the guide itself. Not taste: a sentence that reads cleanly at first pass is not a finding because you would word it
differently. Not staleness or a contradiction with the code — general review and the doc-gardener cover those.
