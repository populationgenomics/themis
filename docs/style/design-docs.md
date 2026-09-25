# Design docs

A design doc is the durable record of one area's design. Its readers were not in the conversation that produced it, and
this guide follows from what they need. Everything up to and including the Style section applies to every doc under
`docs/`. The sections after it apply to design docs.

## The reader

Under the review policy ([`../design/review-policy.md`](../design/review-policy.md)), a second maintainer normally
reviews each design doc, and every later reader of the area starts from it. Write for a maintainer who has read
[`../PRODUCT.md`](../PRODUCT.md) and [`../../GLOSSARY.md`](../../GLOSSARY.md) but knows nothing about this area: not its
adjacent docs, and not its code. They read the doc once, on GitHub, and then decide whether to approve it.

Reviewing a design means doubting it. Before the reviewer can ask whether a constraint really binds, or whether an
alternative was dismissed too quickly, they have to rebuild the reasoning in their own head. So a doc that contains
every decision and every reason can still fail its reviewer. If they can only rebuild the argument by reading slowly, or
by asking someone to walk them through it, the doc has not done its job. The test is whether the reviewer can follow the
argument at reading speed and then disagree with a specific step of it.

A doc written for this human also serves a model that reads it as context. The reverse does not hold. Prose compressed
for a model drops the antecedents, transitions and consequences that a person needs when they meet an argument for the
first time.

## What a doc costs to keep true

Agents rewrite these docs in place, so the cost of keeping one current is not the cost of a person editing prose by
hand. A longer explanation, a second example or another diagram costs almost nothing to maintain: the agent that changes
the decision regenerates the illustration beside it.

What does cost is a copy of a fact that something else owns. When a doc repeats a field list, a path or a constant, the
code can change without anything telling the doc, and the next reader believes the stale copy. That is the reason for
every cut of detail this guide asks for (§"Where the low-level detail goes"). It is also the only reason to cut an
explanation, an example or a figure. Never cut one to make a doc shorter.

## How to explain a design

### Start from the obvious answer

Many designs answer a question that has an obvious answer, and the reader arrives holding it. Say what that answer is
and what it gets right. Then show the fact that breaks it. The decision now reads as a consequence of that fact, and the
reviewer knows exactly which premise to attack if they disagree.

Take a store of transcript sequences. The obvious design derives each transcript from the genome, by cutting out its
exons and joining them. It needs no extra files. It is also wrong for 2.7% of transcripts. For those, the sequence the
publisher curates differs from the genome, and ClinVar and the tools that check variant names read names against the
publisher's sequence. Stated in that order, "store the publisher's sequences" needs no further defence. Stated alone at
the top of a Design section, it looks like a preference.

Not every decision has a rival the reader would think of. Where none exists, state the decision and its reason directly.
An invented alternative is a straw man, and the reviewer will read it as one.

### Put an instance beside every claim the argument rests on

An abstract claim asks the reader to take it on trust. An instance lets them check it. "The two sources disagree for
2.7% of transcripts" is a number. One named transcript, with the base that differs and the variant name each source
produces, shows the reader what the number means and why it matters.

Put that instance next to the claim, where the reader meets it. The appendix holds the evidence behind a claim: the
survey, the benchmark, the measurement table. It is not the place for the one example that makes the claim intelligible.

An invented example with toy values is often clearer than a real one, because it can leave out everything irrelevant.
Follow one input through the design: what arrives, what each step does to it, and what comes out.

An example's input can be invented, but the output it shows is a claim about the design. Choose outputs that follow from
a decision rather than from the current contents of a table in the code. "A file extension the map does not list stays
unhighlighted" stays true when the map grows; "`.rs` stays unhighlighted" does not.

### Draw what the reader would otherwise hold in their head

A figure takes seconds to read and saves paragraphs of description. Draw each of these that the design has:

- a **surface**, as a mockup. An ASCII sketch is enough.
- a **flow** across services or processes, as a request diagram with example values on the arrows.
- a **shape** the reader has to picture, such as a record layout, a tree, an alignment or a set of states.
- an **architecture** that is easiest to see as code, as a sketch of a class or a service with the method bodies elided
  to `...`. The sketch is a guess at writing time, and the contract file or the code replaces it once that exists.

Most designs have at least one of these. Before concluding that yours has none, check again. Do not draw a figure that
shows nothing the prose does not.

Two other forms help in the same way without being figures. Where the design decides what is stored where, say so
plainly: this in GCS, this in Postgres, this only at the model provider. Where the doc covers several interfaces, give
each its own subsection in the same order (what happens when it is called, where the request goes, what is stored, what
side effects follow), so the reader can compare them.

Mermaid and ASCII are text, so they are as cheap to maintain as prose. Where GitHub cannot render a diagram well, for
example a large generated graph that needs a layout engine GitHub lacks, commit an SVG rendered from its source beside
the doc, as [`../design/deployment.md`](../design/deployment.md) does for its IAM grant graphs. GitHub renders mermaid
in the file view but not in a PR diff, so a reviewer has to open the file to see it.

### Teach the background

The Background section teaches the reader what the rest of the doc assumes. Introduce each term when the argument first
needs it, in a sentence that says what it is and why it matters here. A block of bolded definitions at the top asks the
reader to memorise words before they know what problem the words are for.

When the area needs a lot of domain knowledge, start from the basics. A reader who already knows them can skip ahead,
and a line at the start of the section saying where to skip to costs them one sentence. A reader who does not know them
cannot review the design at all.

### Connect each step to the last

Each paragraph should say how it follows from the one before it: "so", "that leaves", "this breaks when", "the same
argument applies to". Without that connection the reader has to work out the logic between two facts, and they may work
out something different from what the author meant. Where two decisions are independent, say so. A connective that
claims a link the argument does not have is worse than none.

A bold label at the start of a paragraph suits a list the reader scans, such as a set of rpcs or a set of rules. In the
middle of an argument it cuts each step off from the last. Use a heading or a transition there instead.

## Style

Write with the clarity of Martin Kleppmann: plain prose built up from first principles, each term defined before it is
used, and the trade-offs of every option stated honestly rather than slanted towards the one chosen. The habits below
are how that looks at the level of a sentence. They are written out because compressed technical prose drifts towards
their opposites.

**One idea per sentence.** A sentence that carries a claim, its qualification and its consequence forces the reader to
unpack it before they can judge it. Split it.

**Write noun phrases out in full.** A phrase such as "a value a build predates" packs a whole clause into a noun, and
the reader has to reverse the compression. Write the clause: "a value that the tab's build does not know about". The
same goes for stacks of nouns that stand in for an explanation.

**One aside per sentence at most.** A parenthesis or a pair of dashes counts as one. With two, the reader has to hold
the main clause open across both.

**Say who does what.** "The BFF diffs the two sides" tells the reader where the work happens. "A replacement is diffed
server-side" makes them guess.

**Prefer a plain description to a coined label.** A project-specific term is worth introducing only if the doc uses it
many times. Define it at first use, in a sentence of its own.

**State the consequences.** Do not leave the reader to derive what follows from a decision. They will derive something
slightly different from what you meant, and review that instead.

Here is one sentence rewritten with these habits:

```
Before: A value a build predates renders as unknown, not as an error: a stream is worth
more partly drawn than not at all.

After: During a deploy, a browser tab keeps running the build it loaded, so the server can
send it an enum value that build does not know about. The tab draws that value as
"unknown" and carries on. A curator is better served by a stream with one unlabelled pill
than by a stream that failed to draw.
```

### Patterns that read as machine-written

Some patterns are harmless once but read as generated when they recur, because the reader notices the distribution
rather than any single sentence. Keep each of these to one per doc at most:

- **Contrastive headings**, such as "What is stored, and what is not". The second half only negates the first. Name the
  topic instead ("Storage"), or state the claim as a sentence.
- **Definition by negation**: describing a thing by what it is not, such as "a design choice, not a limit". Say what it
  is. A rule that excludes something ("a thing not yet built is not a non-goal") is not this pattern.
- **Negation stacks**, such as "No test. No sign-off. No access decision." Fold them into one sentence that carries the
  content.
- **Epigrams and balanced pairs**, such as "Agreement is not corroboration; disagreement is not a defect."
- **Closing lines that announce their own significance**, such as "That is the whole mechanism." End the section on its
  last real point.
- **Counting before listing**, such as "Three things follow." Start with the first thing.

Em-dashes are fine at a normal rate. Prose scrubbed of every one of them reads as machine output too.

For the style applied to a whole doc, read [`../design/agent-output-rendering.md`](../design/agent-output-rendering.md).
Most other docs under `docs/design/` were written before this guide, so read them for what they decide rather than for
how they are written.

## Where the low-level detail goes

The doc names decisions and interfaces, such as "the `Variant.Normalize` rpc" or "the `themis.svcv4` library". Where an
interface has an entry point in code, link it where the interface is first introduced: the module, the directory, the
proto file. Anything more specific restates code and stays out. That covers a field-by-field paraphrase of a proto or
schema, env-var names, file paths beyond the entry point, names of functions, classes and tests, error strings, and
constants. Each would be a second copy of something that already has an authoritative one.

A detail that leaves the doc moves to the code's own documentation: the comment on a proto field or message, a module or
function docstring, or a test. That text is written for the caller. It says what the field or rpc implies for them, what
they must do and what they can rely on. It does not go into an inline comment beside the implementation, which the
Comments rule in [`general.md`](general.md) governs. If a passage stops making sense once its detail is gone, it was
written at the wrong altitude. Rewrite it at a higher level rather than restoring the detail.

The doc and the code can state the same decision without making a copy, as long as the argument lives in one place. The
doc gives the decision and the reason for it. The comment on the message gives what it means for the caller, with at
most a clause of why. For example, the doc explains that a reply is empty because a reply carrying the result would let
the client skip the poll. The comment on the message says only that the caller learns the result from the poll. Where
context would help a reader of the code, the comment can carry a one-line pointer to the doc, but never a restatement of
it ([`general.md`](general.md), Comments).

An example is not a restatement of code. Toy inputs and the outputs they produce illustrate a decision, and they stay
true until the decision changes.

Nor does the doc narrate what a fixture, a test or a first implementation happened to constrain. When an implementation
constraint really does shape an interface, state it as a decision and give its reason. Otherwise leave it out. A
reviewer who reads an interface explained by what one implementation does sees the implementation driving the interface,
which is the wrong way round.

The rule on low-level detail holds even when the contract file is the only code so far. That file is the spec, and the
doc explains it. When nothing is written yet, the doc names the interfaces and says what each one promises, and the
field-level detail waits for the contract file.

## A default shape

This is the shape that has worked, not a template. Change it wherever the design reads better another way.

The doc opens with a `**Related:**` line that links the docs a reader might need, with a few words on what each covers.
Keep it short, because it is the first thing on the page. If more than about eight docs are closely related, the doc
probably covers two areas.

- **Overview**: a reader who stops here should know what problem the doc solves, what it decides, and why. Write it as a
  short argument, where each decision follows from the problem or from the decision before it. A list of bolded
  conclusions gives the reader the answers without the reasoning that connects them.
- **Background**: the problem, the constraints that bound the design, and whatever the reader has to learn to follow the
  rest (§"Teach the background"). A constraint is something found to be true, such as how a third-party API behaves or
  what users will not accept. How the design came about is not background.
- **Non-goals**: what the design deliberately excludes, and the reason for each exclusion. Something not built yet is
  not a non-goal, since the doc describes the design and the code shows how much of it exists. Omit the section when
  there are none.
- **Design**: one subsection per decision. Each states the question and the decision, the obvious answer and what breaks
  it where the reader would arrive holding one (§"Start from the obvious answer"), an instance, and what the decision
  costs. Where the design has a flow, open the section with one realistic input traced through it, drawn with example
  values.
- **Alternatives considered**: options that are not already argued in a Design subsection. For each, say what it does
  better than the chosen design and why it lost anyway. An alternative described only by its weaknesses gives the
  reviewer no reason to believe anyone weighed it.
- **Open questions**: points that still need a decision or an input. Omit the section when there are none.
- **Appendix**: the evidence behind claims the argument rests on, such as a measurement, a survey or a benchmark table.
  Never a restatement of code.

No section records what is built, planned or shipped. That changes with every merge, and git already holds it. What such
a section would say belongs elsewhere:

- A deliberate deferral is a design decision, with its reason.
- An unresolved gap is an open question.
- An accepted transitional cost, such as a migration's failing window, sits beside the decision that accepts it.

## Policy

- **One living doc per area**, under `docs/design/`. There are no ADRs: the rationale lives in the doc, and the
  chronology lives in git.
- **Rewrite in place.** A design change edits the doc. It never appends a layer that supersedes an earlier section, and
  it never creates a "v2". When a doc is superseded, delete it and fold what is still live into its successor, in the
  same PR.
- **State each decision once.** One passage owns a decision and its reason. If a later section restates the decision,
  the next edit will update only one of the two. An example, a figure or a sentence that recalls the decision in passing
  is not a restatement.
- **A doc is followable on its own.** Where the argument depends on another doc's decision, state that decision in a
  clause and link to it.
- **One area per doc, at whatever length the argument needs.** Figures and examples make a doc longer on the page and
  faster to read. A doc that runs long because it covers two areas splits along its own subsection boundaries. Where the
  mechanism of a sub-design stands on its own, the design doc says why it matters and links to a doc of its own for the
  rest.
- **Bring a doc up to this guide** when it is next substantially edited, or in a rewrite of its own. The review at PR
  time looks only at the docs a PR changes.
