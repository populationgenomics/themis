# Check a candidate fix for generality

For the coordinating session, when an instruction change might be fitted to the variants that prompted it: run this
before the change is written, in a fresh-context subagent that has read none of the round's variants. Replace
`{FINDING}` with the finding as it would read without naming a variant, a gene or disease entity, a curator or anything
a curator wrote, `{FIX}` with the change as it would be written, and `{SKILLS_RUN}` with each of our skills the round's
runs read, as `agents/skills/<directory>` at `<commit>`, from any collected run's `provenance.skills`. Hand over
everything below the rule.

______________________________________________________________________

A loop that fixes an agent against a small fixed set of variants drifts towards fitting them. Each round finds a real
defect, and the cheapest change that makes it go away is usually narrower than the principle behind it, which nothing on
the variants that motivated it can tell apart. Your job is to read a proposed fix against the framework rather than
against those variants.

**The finding:** {FINDING}

**The proposed fix:** {FIX}

Never open `tools/run_review/rounds/` or any copy of the curated variants' source material, inside the repository or
outside it, such as the pilot's practice-variant workbook (a spreadsheet listing the same variants with their curation
hints). They hold the variants the fix came from, and the check is only worth something if you have not seen them. Find
your counter-examples in the framework's text and the code.

The framework is SVCv4. Its transcription into code, the workflows, cells, caps and the readings applied where the
standard is silent, is under `themis/svcv4/`, with the transcription's rules in
`docs/design/svcv4-worksheet-transcription.md` and `docs/design/svcv4-interpretations.md`; the agent's instructions are
under `agents/`. Read them, since the cells no variant reaches are there.

The runs behind the finding read the agent's skills as they stood at {SKILLS_RUN}. Read them there with
`git show <commit>:agents/skills/<directory>/SKILL.md` to see what the runs were told, and read the checkout's `agents/`
for what the fix would change. If the two differ on the point the finding is about, say so: the finding may already be
closed, or the fix may be written against text the runs never saw.

Answer these, citing the framework's text or the code for each claim:

- Which codes, cells and paths does the fix change the behaviour of? Name them, including the ones it does not mention.
  A fix that reaches one workflow on one path may be a special case stated in general words, and saying so is a useful
  answer.
- Where would it be wrong? Look for a cell where applying it gives the wrong result, such as a path that scales, caps or
  routes differently from the one it was written for, or an entity under another of the framework's five modes of
  inheritance, which the population and clinical rules route on. Give the counter-example in full.
- What does it rest on besides the variants that motivated it? A fix earns its place from the standard, a contract, or
  an upstream's own documentation. If the only support is that a run got some variant wrong, it is a hypothesis fitted
  to that observation.
- Can it be stated, as it would appear in the instructions or the code, without naming a variant, a gene or a disease
  entity? If not, it is not a rule.
- What does it cost elsewhere: an instruction made ambiguous, a cell made harder to reach, a check made weaker?

Write prose. End with one of: general; fitted; or general but wrong in a case you name. If you found no counter-example,
say what you looked at, so the absence carries weight. Where the fix fails, say whether the finding behind it still
stands.
