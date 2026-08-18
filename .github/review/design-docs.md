### Concern: design-doc readability

A design doc is written for a human first ([`CLAUDE.md`](CLAUDE.md) "Docs"): under the review policy a second maintainer
who was not in the conversation reviews it, and every later reader of the area starts from it. That reader has read
`docs/PRODUCT.md` and `GLOSSARY.md`, knows nothing about the area, and has to make a review decision from one read on
GitHub. [`docs/style/design-docs.md`](docs/style/design-docs.md) states what such a reader is owed and the style that
delivers it — read it on the head branch before reviewing. You are the pass that catches what the author's own read
missed, while it is still cheap to fix.

Triggers when the diff adds or changes a file under `docs/design/`. Read the changed doc **whole, as it stands on the
head branch**, not only the hunks — whether a term is introduced before it is used, or motivation precedes mechanism, is
a property of the doc, not of a line — then flag the passages this PR adds or changes. Whole-doc properties — the
take-aways at the top, the order of the argument, detail interleaved with it — are demanded only of a doc the PR adds or
substantially rewrites (more than about half its lines); a small edit to a doc that does not yet meet the guide is not a
finding.

Two things are findings. The first is a passage where that reader cannot get the decision and the reason for it from one
read: the Overview does not carry the take-aways, mechanism arrives before the motivation for it, an area term or an
identifier is used before it is introduced, specifics are interleaved so the reasoning breaks and the reader has to
reconstruct where it was, or an implementation accident — what a fixture or a test happens to do — is narrated as if it
were design. The second is a passage that restates code — a field list, a path, an env-var name, a function or test
name, an error string, a constant — where a link to the source of truth belongs. Quote the passage and say which it is.
This is judgement, not a checklist: the question is whether that reader comes away with the decision and the reason, not
whether a rule was broken.

T2 by construction. The design doc is the artifact the second maintainer reviews and the one every later reader of the
area starts from, and a readability failure that lands stays until someone rewrites the doc — the doc-gardener does not
touch style by design ([`.github/doc-garden/instructions.md`](.github/doc-garden/instructions.md)) — so its cost climbs
with every read. The finding is worth most before the second maintainer's read.

One finding per problem per doc, anchored to the first passage that shows it — not one comment per sentence. A problem
that shows by absence (no usable Overview, a motivation stated nowhere) is anchored to the heading of the section that
should carry it, or to the title line for the Overview. Remediation: rewrite the passage per the guide; for a passage
that restates code, replace it with a link to the source of truth (the proto file, the module, the test).

Do not flag a doc this PR does not change, a doc outside `docs/design/` (`docs/plans/` and the rest are out of scope),
or the guide itself. Not taste: prose that carries the decision and its reason is not a finding because you would word
it differently. Not staleness or a contradiction with the code — general review and the doc-gardener cover those.
