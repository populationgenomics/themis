# Design: document widgets

**Status:** draft **Related:** [`workbench-workspace.md`](workbench-workspace.md) (the browser's copy of the repository
a revision is read from, and how a curator's edit becomes a commit); [`document-pane.md`](document-pane.md) (the
renderer, the citation directives, and the reveal a citation raises);
[`agent-output-rendering.md`](agent-output-rendering.md) (why agent text never becomes markup, and how the client draws
a value that a tab's build does not know); [`sheaf.md`](sheaf.md) (the pre-receive hook every agent push passes);
[`proto.md`](proto.md) (the option pattern, and the compatibility gate that binds a schema from its first commit);
[`curation-surface.md`](curation-surface.md) (the SVCv4 capture the evidence-tree sketch lines up with).

## Overview

A working document is markdown, and markdown is the wrong medium for a classification table, a pedigree or a checklist a
curator works through. A model asked to describe such a structure in prose writes a different table on every run, and
nothing marks which numbers in its sentences came from a source and which it recalled. A widget replaces that prose with
a typed block. The agent writes a file holding a serialized protobuf message, names it in the document with one
directive, `::embed[<path>]`, and the client draws it.

The file names its own type, as a `google.protobuf.Any`, and a hand-written registry in the client maps each type to the
component that draws it. The file is binary, so the only way to produce one is to run a serializer over a typed message:
a widget's contents arrive in a declared shape, through code, never hand-typed. An item that rests on a source carries
its own citation. A payload type's schema is reviewed in a change of its own, ahead of the component that draws it. A
type is *marked* once its schema carries the option that lets a document embed it. CI fails a marked type with no
component, so the schema lands unmarked and the change that ships its component adds the mark.

The document is the tip of a branch that the agent pushes to as it works, so at any moment it may name a file the agent
has not written yet. The pre-receive hook, which checks every agent push before it lands, therefore does not check
whether the document can be drawn. Instead a linter in the agent's sandbox and the renderer apply the same drawability
rules. The linter reports every directive that would not draw while the agent can still fix it, and the renderer draws a
visible placeholder for anything that fails and renders the rest.

Some of a widget's contents are a user's judgement on the rest, such as a curator's tick on a checklist item. A tick
vouches for the item as the curator saw it, so it must not survive the agent changing that item, and the agent rewrites
the same file whenever it revises its work. The payload's schema marks each field that holds a judgement as a guard. By
default a guard judges every other field of its message and everything beneath them, apart from the item's key and other
guards, so a tick on a checklist item judges that item's label and citation. The rule is that a guard holds a judgement
exactly when the version before held the same judgement on the same content; in every other case it is at its default,
"no judgement". So the agent can never set a guard, nor clear one without changing what it judges. It may still remove
an item, and the user's judgement on it goes with it.

Each writer is held to its side of that rule where its changes pass. A user edits in the browser, which refuses an edit
that changes anything but guards. The hook every agent push passes refuses a change that breaks the rule, but the agent
is not left to satisfy it by hand: it rebuilds a widget file with a helper that starts from the committed version and
carries each judgement across wherever what it judges is unchanged. When the agent's work and a user's tick meet, the
agent rebases onto the user's commits rather than merging them. If the rebase stops on a widget file, the helper
rebuilds the file from the user's version, so the agent keeps the user's ticks by the same rule the hook checks.

## Background

The working document and the conversation are drawn by one shared markdown component. The agent already writes two
directives into them: `:paper[id]`, which cites a paper, and `:quote[id, text]`, which cites a passage in it. The
renderer turns each into a mark a curator can click to open the paper beside the text
([`document-pane.md`](document-pane.md) §Reveal). Anything added to that pipeline inherits two of its properties. The
markdown as served is never rewritten, because `Locate`, the rpc that finds a quoted passage, returns code-point offsets
into that exact text. And a directive the renderer does not recognise is turned back into literal text, because
`remark-rehype`, the step that turns the markdown syntax tree into page elements, drops an unhandled directive and the
token after it.

Everything in the document was written by a model, and much of it quotes pages the model read, so agent text reaches the
page as elements, never as HTML ([`agent-output-rendering.md`](agent-output-rendering.md)). Nothing the agent writes may
choose how it is drawn, carry styling, or introduce an element.

A revision of the document is a commit of the Analysis's workspace repository, and the document is the file
`working_document.md` at the tip of its collaborative branch. The browser keeps its own copy of the repository and reads
the document, and every file the document names, at one commit ([`workbench-workspace.md`](workbench-workspace.md)). A
commit never changes, so a path in a directive means one file for as long as that revision exists.

Two writers change that branch. The agent pushes from its sandbox, and every push passes sheaf's pre-receive hook, which
runs outside the sandbox and can refuse the push before any ref moves ([`sheaf.md`](sheaf.md)). The hook runs in the
sandbox worker, the trusted process that holds the session's credentials and runs the agent's commands in an isolated
guest ([`sandbox-worker.md`](sandbox-worker.md)). A curator's edit is committed by the browser and published through the
BFF, the web tier's backend, without passing the hook. Curators act in good faith, and the browser is a trusted writer,
as the sandbox worker is. The agent is the one untrusted writer.

Clinical work has a shape that prose loses. An SVCv4 classification is a set of criteria, each with a status, a selected
outcome, evidence and reasoning. Written as markdown, the table changes form from run to run, which defeats scanning it,
comparing runs, and seeing which criteria went unanswered. A reader cannot tell whether a number in a sentence was
retrieved or recalled by the model, and "every claim cites a reproducible source" ([`../PRODUCT.md`](../PRODUCT.md) §6)
cannot be enforced against a sentence. Drawing is also deterministic work, and the principle that puts parsing and
arithmetic in code and keeps the model for judgement applies to layout too.

## Non-goals

- **Presentation in the payload.** No payload field holds a colour, a width, a template or a label drawn as chrome. The
  client owns the drawing, which is what keeps a widget inside the rule that agent text never becomes markup.
- **Computing a classification.** A widget draws what its file carries. Points, totals and bands come from the
  `themis.svcv4` library, and a second implementation inside a renderer would be a second thing to audit
  ([`curation-surface.md`](curation-surface.md) §Non-goals).
- **Widgets in the conversation stream.** A widget's file is found relative to a document at a commit, and the stream
  has no commit to resolve it against, so the mechanism itself enforces the exclusion. The stream draws each tool call
  from the projection, the BFF's reading of the call's inputs into a label and a body
  ([`agent-output-rendering.md`](agent-output-rendering.md)).
- **Typed values inside a sentence.** A value in a sentence is still typed by the model, so the argument for widgets
  applies to it too. A directive naming a field of a file would be cheap to resolve, but each kind of value (a
  frequency, a p-value, a coordinate) has its own formatting rules, and a layer that gets one wrong mid-sentence is
  worse than the model typing the number. That layer has decisions of its own, so this design stops at the block and
  leaves typed values in prose for a later design.

## Design

### One widget, end to end

Here is a checklist, the widget type this doc uses as its example, from the agent's write to a curator's tick. The agent
builds a checklist message with the helper its sandbox ships, which serializes it, wraps it in an `Any` and writes the
widget's file, its *asset*. The message is shown below in protobuf's text form; its schema is
[`checklist.proto`](../../schema/proto/themis/widgets/models/checklist.proto).

```
assets/review.binpb        a google.protobuf.Any
  type_url   type.googleapis.com/themis.widgets.models.Checklist
  value      items:
               { id: "PS3", label: "Functional studies support a damaging effect",
                 citation: { doc_id: "1111…", quote: "…loss of channel function…" }, checked: false }
               { id: "PM2", label: "Absent from population controls", checked: false }
```

It names the file on a line of its own in `working_document.md`, runs the linter, and commits and pushes both:

```
### Curator checks

::embed[assets/review.binpb]
```

The browser reads the document at the new commit from its copy of the repository. For the directive it reads
`assets/review.binpb` at the same commit, decodes the `Any`, finds `themis.widgets.models.Checklist` in its registry,
and draws the checklist component in place of the line:

```
 Curator checks
   [ ] PS3  Functional studies support a damaging effect   ⧉ source
   [ ] PM2  Absent from population controls
```

A curator ticks PS3. The widget sets PS3's `checked` field in the asset as it was at the commit on screen, and the
browser publishes the new file as a commit of its own on that commit. If the agent has moved the branch in the meantime,
the browser applies the edit on the new tip only if the asset there has the same blob and mode as the version the
curator saw. Otherwise the curator redoes the tick on the new version
([`workbench-workspace.md`](workbench-workspace.md) §"A curator's edit is rebased onto the tip, file by file").

When the agent later revises the checklist, it rebuilds the file with the helper, which carries PS3's tick across for as
long as PS3's label and citation are unchanged. If the agent changes PS3's label or citation, the tick clears and the
curator reviews PS3 again. The hook refuses any push that drops the tick from an unchanged PS3, or keeps it on a changed
one. The sections below explain each step.

### One directive, and the file names its own type

The obvious syntax names the widget in the directive, `::pedigree[…]`, which reads better. It breaks because the file
already names its type, and two declarations of one fact can disagree: a `::pedigree` pointing at a file of aligned
reads is a state somebody would have to define behaviour for, and a rule the agent would have to be taught. So the
directive names only the file, and the file's type decides what draws it. Adding a widget type then touches no grammar,
because it is a payload schema and a component, and the directive set stays at three.

`::embed[assets/review.binpb]` is a leaf directive whose label is a path relative to the document. Its two colons are
the directive grammar's block form, where the citations' single colon is the inline one. A citation renders inside the
paragraph that holds it, and a widget is a block: a chart inside a paragraph is markup the browser repairs by closing
the paragraph early, so the rendered page would no longer match the document.

An embed is drawn only where it stands on a line of its own at the top level of the document, written exactly as
`::embed[<path>]`. Anything else that starts with `::embed` draws the placeholder, for example an embed inside a list, a
block quote or a footnote, or one with text after the label. Markdown nests blocks in more ways than two parsers
reliably agree on, and the linter and the renderer are two different parsers. One rule that both can apply the same way
avoids the disagreement, and it costs the agent nothing it needs.

The same concern shapes how the label is read. A label is inline markdown, so a parser that interprets it would read
`assets/_b_.binpb` as emphasis and `www.x.binpb` as a link, and the linter and the browser could resolve one directive
to two different files. Neither interprets it. Both take the label's source text as written, by the mechanism in
§"Appendix: reading an embed's label as source text". A path is admitted only over a conservative character set:
letters, digits, `-`, `_`, `.` and `/`. That keeps it a plain relative path on every filesystem, and it keeps out `]`
and the backslash, the two characters that could make the readers disagree on where a label ends. The linter refuses a
path outside the set and the renderer draws the placeholder for one, so the producer renames the file.

The path is resolved against the revision's tree at render time, as a lookup, so the markdown itself is never rewritten
and `Locate`'s offsets stay valid. The renderer knows `::embed` as it knows `:paper` and `:quote`, and a surface turns
widgets on by giving it a way to read files at its revision, as it turns citations on by giving it a click handler. A
surface that does not, such as the conversation stream, shows the directive as literal text.

### The file is a binary `Any`, so a program built it

The obvious format is JSON, which is readable and diffs well. It breaks because the agent is a model: it can write JSON
by hand, and sooner or later it will, when a tool call fails and the file looks easy to fix. It cannot hand-write a
serialized protobuf. Producing those bytes means running a serializer over a typed message, so the type checker and
protovalidate, the proto validation rules, apply to a widget's contents as they apply to an rpc's. The helper the
sandbox ships ([`sandbox-worker.md`](sandbox-worker.md) §"The guest's world is assembled at build time") builds the
message, wraps it and writes it.

The file is the `Any` and nothing else: no envelope carrying who made it or when. The commit already records when the
file was produced, and the run's trace records what produced it, so the file carries only the payload. A `created_at`
field the model filled in would only be a field the model could get wrong. An envelope would also carry provenance at
the wrong granularity: provenance belongs to each item, as the citation pattern below shows.

The `Any` is also held to one framing: the type URL, then the payload, and no other field. Protobuf parsers disagree on
some malformed bytes. If the hook's parser and the browser's read a different type URL or different payload bytes out of
one file, the hook might read it as no widget at all while the browser draws a ticked checklist, and that tick would
never have been checked. In the one framing every parser reads the same type URL and the same payload bytes. The linter
and the hook refuse a file that names a widgets type in any other framing. A file the hook cannot read as an `Any` at
all passes it as no asset, but the renderer draws only the one framing, so such a file can never draw as a widget.

The binary format cannot show that a value was retrieved rather than recalled, because the model chooses what it passes
to the constructor. That is the concern of the evidence services the agent reads its values from. What the format does
guarantee is that each value went through a declared schema and a program. So it sits in a named, typed field with a
slot for its citation, in a shape the client and the schema agree on.

### Where structure lives

The other way to get structure out of a document is to read it out of the prose, with a tag vocabulary inside the
markdown or a parser over headings. A vocabulary shared by the author, a parser and a renderer drifts, and a parser over
prose fails silently: a heading one level off drops a criterion without saying so. The curation surface makes the same
choice for a curator's worksheet, which is a form whose fields are a schema
([`curation-surface.md`](curation-surface.md) §"What is captured, and why").

Widgets make that choice for the agent's side. The markdown carries no schema, because `::embed` names a file and says
nothing about its contents. The structure lives in the file, where a schema, the compatibility gate and the linter apply
to it. Both apply one rule: anything that must be structured gets a declared schema, and prose is never parsed for
structure.

### Declaring a widget type

A payload type is an ordinary proto message in the widgets package, marked by a message option
([`widget.proto`](../../schema/proto/themis/widgets/models/widget.proto)). It works like the method option that marks an
rpc the sandbox may call ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)): one place to declare it, read from the
descriptor, and absent means false. The linter reads the set of marked types from the descriptors, so the types it
accepts cannot drift from the contract.

The client's registry is written by hand, because behind each entry is a React component somebody wrote. A generated
registry would find a type's component by deriving its name from the type's name. Nothing would check that convention,
and when a component does not follow it, the failure is a component silently not found. Writing the registry by hand
gives up the guarantee generation offers, an entry for every type, and a check gets it back: the pairing check fails CI
for a marked type with no registered component, so no payload type ships without its component.

A payload type's schema lands in a change of its own, ahead of its component, and is reviewed as an interface
([`review-policy.md`](review-policy.md)). Two checks decide what that change carries. The compatibility gate compares
every committed proto from the change that first commits it, and it refuses a released message gaining the widget mark
or a guard, since a push hook built before that change would check the message's assets differently from a newer one
(§"A guard protects everything beside it unless it says otherwise"). So the mark and the ownership options on the
payload's fields land with the message, in the contract change; once it merges, no change can add them to that message.
The pairing check then fails the marked type until a component is registered for it, so the contract change also
registers a placeholder, the way a contract change for a service stubs the rpcs whose implementation comes later.

The placeholder draws what the renderer draws for a type this build has no component for: the neutral box that names the
asset's path and says the build does not draw the type (§"Drawability is checked where the file is, and tolerated where
it is shown"). It still parses the payload and checks it against its rules, so an asset written against the new type
during component work fails as it would under the finished component.

| Change               | What it does to the schema                                       | What the registry maps the type to |
| -------------------- | ---------------------------------------------------------------- | ---------------------------------- |
| The contract change  | declares the message, with its widget mark and ownership options | the placeholder                    |
| The component change | changes it additively, if at all                                 | the component                      |

Because the gate holds the schema from the contract change on, whatever the component work learns about the payload
arrives as an additive change, a new field or a new message beside the old. A reshape the gate would refuse has to be
settled while the contract change is under review.

From its first push on a deployment whose data is kept, a payload schema is persisted data. A pushed revision is never
deleted ([`sheaf.md`](sheaf.md)), and `buf breaking` holds the schema's fields to additive changes
([`proto.md`](proto.md) §Schema evolution). The widget options have a comparison of their own (§"A guard protects
everything beside it unless it says otherwise"). Retiring a type is therefore a migration, never the removal of a
registry entry. A type any pushed revision references must keep decoding, and a converter feeds its successor's
component. For a type the tab's build does not know, the placeholder covers only the minutes of a deploy, when a tab
still runs the previous build. A revision from years ago still has to draw in full.

### Patterns a payload follows

The shapes below recur across payloads. Each is a decision about the framework rather than about one widget.

An item that rests on a source carries its own citation: a litcache `doc_id` and an optional quote. A click on it opens
the paper, and the quote within it, as a `:paper` or `:quote` in prose does, and a malformed id draws the same visibly
broken mark, never a guess at the nearest match. The citation sits on the item rather than on the file, because an
evidence tree's criteria rest on different sources. It stays a pattern rather than a type because not every payload
cites literature (an aligned-reads view cites nothing; the reads are the evidence), and because a citation into a figure
or a table may need more than a doc id and a quote. The first payload that needs more, the evidence tree, decides its
shape.

A payload about a place in the genome says which place, as an interval: the shared `themis.evidence.models.GenomicSpan`
beside the chromosome accession it lies on, the pairing the evidence contracts use. Normalising an allele belongs to the
evidence layer, and a widget carrying a normalised id would be a second place that decides identity. A document that
embeds a pedigree and an aligned-reads view then states which locus each is about, for a reader who arrives at a widget
out of context and for a run whose document weighs more than one candidate.

### A user's judgement stands only while what it judges is unchanged

Most of a payload is the agent's: a checklist's items, their labels, their citations. One field records a user's
judgement on the rest: whether a curator ticked the item. Both live in one file, and the agent rewrites that file
whenever it revises its work. In the walk-through above, the curator ticked PS3. Suppose the agent then rebuilds the
checklist from its own notes, where PS3 is unticked, and pushes. Nothing in the file format stops that push from
silently undoing the tick, and nothing stops the agent from writing a tick itself, which the workbench would draw
exactly like a curator's.

The obvious answer is to mark the fields a user writes, with a `written_by` option in the schema, to match the items of
two versions of the list by a key, and to hold the agent to keeping the user's fields on every item it keeps. That gets
two things right: the agent cannot write a tick, and it cannot undo one by rebuilding the list. It breaks because a tick
is a judgement on a specific statement and its citation, and nothing in that rule ties it to them. On the dev
deployment, an agent's commit relabelled every item of a checklist in which a curator had ticked two, and both ticks
survived, because the keys had not changed. By the same rule the agent could reword "PM2 applies" to "PM2 does not
apply" and keep the tick, which would then vouch for a claim the curator never saw. The two ways of getting this wrong
cost different amounts. A tick cleared when it could have stayed costs the curator a click to give it again, whereas a
tick that outlives what it judged is a false attestation in the record.

So a judgement is tied to what it judges. The schema marks each field that holds a user's judgement as a *guard*. A
guard's default value, unset, means "no judgement". For a plain proto3 `bool` such as `checked`, unset and `false` are
the same bytes. For a guard that has presence, such as a message or an `optional` field, an explicit `false` is a
judgement, so "no judgement" is unset. The agent never writes a guard: it either carries a user's judgement across or
leaves the guard unset.

Comparing two versions of an asset needs two more terms. An *element* is an entry of a repeated message field, such as
one item of a checklist. Two versions of a list are matched element by element through a field the schema marks as the
element's *key*, here the item's `id`. The payload's root message counts as an element with no key. A key anywhere else,
on a singular message or on the root, is ordinary content: an agent that changes it has changed what the judgement is
about.

What a guard judges is its *protected content*. By default that is every other field of the guard's message and
everything beneath them at any depth, with three exceptions. The element's key is how the element is recognised, so it
is not part of what is judged. A guard is never part of another guard's protected content, so one user's judgement never
clears another. And a guard's `ignores` option lists fields of its message that the judgement is not about, which are
excepted with everything beneath them.

The guard's position in its message and its field number play no part in what it protects. By convention a payload
declares its guards first, from field 1, so a reader meets the judgement before the fields it judges, and a field added
later takes the next number after them rather than landing between a guard and the content it protects. A released
message gains no guard (§"A guard protects everything beside it unless it says otherwise"), so a message's guards are
all declared with it and can always take its first numbers.

Its scope stops at its own message. A tick on one checklist item covers that item only, not the neighbouring items or
the list that holds them:

```proto
message Checklist {
  message Item {
    bool     checked  = 1 [(guard) = {}];   // the user's judgement on label and citation
    string   id       = 2 [(element_key) = true];
    string   label    = 3;
    Citation citation = 4;
  }
  repeated Item items = 1;
}
```

§"A guard protects everything beside it unless it says otherwise" argues why protection is every field rather than a
list of them, and why it reaches all depths. Protected content is compared by its bytes under a deterministic
serialization, except that a list whose elements are matched by key is compared as a set of elements, so that its order
is not part of the content.

The rule itself is stated from the version before the change. A guard holds a judgement on an element exactly when the
version before had the same element, with the same protected content, unknown fields included, and the same judgement.
In every other case the guard is at its default. The rule gives the following:

- a new element's guards are at their default, because there is no version before to hold a judgement;
- on an element present before and after, a guard whose protected content is unchanged is itself unchanged, and a guard
  whose protected content changed is at its default;
- an element may be removed. If a later change adds an element back, under its old key or a new one, it starts with its
  guards at their default;
- removing a singular message that holds a guard clears that guard, because the judgement and what it judged are gone
  together;
- reordering a list whose elements are matched by key changes nothing, neither for its elements nor for a guard that
  protects the list.

Two constraints make the rule checkable:

- on an element present before and after, fields the checking build does not know stay byte-identical, and a new element
  carries none;
- a list whose elements are matched by key may not repeat a key, since "the same element" would then name two.

Between them, the first two consequences mean the agent never moves a guard to a value other than its default. Only a
user creates a judgement. The second also rules out a bare retraction: the agent may not clear a tick while leaving what
it judges as it was. A retraction like that would overrule the curator on content they had seen and accepted, and the
rebuild from stale notes at the start of this section would be one. To clear a judgement, the agent changes what the
judgement is about, or removes the element.

The agent does not have to hold itself to the rule by hand. It rebuilds an asset through the helper's
`update(path, build)`. The `build` function returns the agent's new version with every guard unset. `update` matches its
elements against the committed version by key, carries each guard across where the element's protected content is
unchanged, and reports on stderr each judgement it cleared, with the reason. Here is PM2, ticked by a curator, rebuilt
two ways through `update`, and then written twice bypassing it:

```
committed          { id: "PM2", label: "PM2 applies",        checked: true }

through update
  build returns    { id: "PM2", label: "PM2 applies" }
  update writes    { id: "PM2", label: "PM2 applies",        checked: true }    the tick is carried across
  build returns    { id: "PM2", label: "PM2 does not apply" }
  update writes    { id: "PM2", label: "PM2 does not apply" }                   the tick is cleared, and stderr says:
                   items[PM2].checked cleared: its label changed; the user reviews it again

bypassing update
  agent writes     { id: "PM2", label: "PM2 does not apply", checked: true }    refused: the label changed and the tick stayed
  agent writes     { id: "PM2", label: "PM2 applies" }                          refused: the tick cleared and nothing changed
```

A push that bypasses the helper meets the hook, and git relays the hook's refusal to the agent's `git push` as `remote:`
lines. Each names the file, the element and the guard, and says how to comply. The last row above, a bare retraction, is
refused with:

```
remote: sheaf: refused: assets/review.binpb: items[PM2].checked is a user's judgement, and the agent's change alters it though nothing it judges changed; keep the value the user set; to clear a judgement, change what it is about, or remove the element
```

Removal is allowed because the agent's work does change shape: it splits a criterion in two, or drops an item it no
longer thinks applies. What removal costs is the user's judgement on that item, which the user then has to give again.
The agent's instructions say so, so that it restructures a reviewed checklist only when the work calls for it. Moving a
file to another path counts the same way: the file at the new path is new, and its guards start at their default.

The constraint on unknown fields covers a payload written by a newer build than the one checking it. A build that does
not know a field cannot tell whether it is a guard or content. If it is a guard, "the same judgement" means its value
must not change, and on a new element it must be absent, since the agent could have set it. So the build holds the field
to its exact bytes, and a new element may carry no such field.

The same constraint closes a gap that the `Any`'s one framing leaves open (§"The file is a binary `Any`, so a program
built it"). The framing gives every parser the same payload bytes, but parsers can still disagree on how to read them.
The hook refuses a payload that carries a known field in a wire type other than its own. Any other bytes its parser
cannot place as a known field become unknown fields, which the rule refuses when they are new or changed. So a payload
that the browser reads differently from the hook cannot carry a new tick past the hook.

A merge has more than one version before it, and the rule holds against every parent. Suppose the branch is at `c41e…`,
where a curator has ticked PM2. The curator's commit U, on `c41e…`, unticks PM2. The agent's commit A, also on `c41e…`,
edits only PS3 and still has PM2 ticked. A merge M takes the agent's side of the file:

```
c41e…  PM2 ticked
  ├── U  curator unticks PM2 ───────────┐
  └── A  agent edits PS3, PM2 ticked ───┴── M  merge keeping A's file
```

M matches parent A exactly, with the same PM2 content and the same tick. Checked against A alone, M would pass and
silently undo the curator's untick. Against U, M's tick on PM2 changed while PM2 did not, so M is refused. Holding a
merge to every parent means the agent can never merge a user's edit to a widget file, because a merge that takes in the
user's change to a guard breaks the rule against the agent's own parent. That costs nothing, since the agent rebases
onto the user's commits instead of merging them (§"Each writer's change is checked where it passes").

The schema has no separate declaration of who writes a field. A user's edit may change only guards (§"Each writer's
change is checked where it passes"), so the guards are exactly the fields a user writes, and a second option stating the
same fact could disagree with the first.

Freezing a judged element would keep every tick: while a guard is set, the agent could change nothing it protects. It
lost because the agent's work has to stay revisable. When new evidence arrives the agent must be able to correct a claim
a curator has ticked. Under a freeze it could do that only by removing the item and adding it again under a new key,
which loses the tick anyway and hides that the item is the same one.

Stamping each judgement with the commit it was made on, a `judged_commit`, would keep the tick in the file and let a
reader work out whether the item has changed since. It keeps more than a cleared guard does, since a reader could show
that the tick was given on an earlier version and what has changed. It lost because every reader, the agent included,
would have to recompute staleness from the history before trusting a tick, and a reader that skipped the step would take
a stale tick for a current one. Under the guard rule, no change the agent makes can leave a tick beside content other
than the content it was given on, so a reader can take a tick in the file to refer to the content next to it.

### A guard protects everything beside it unless it says otherwise

The obvious way to say what a guard judges is to list it, with a `protects` option naming the fields the tick is about.
Each guard's scope would then read straight off its declaration. It fails in the dangerous direction when a schema
grows. A field added later and left off the list is unprotected, so the agent can change it under a standing tick and
nothing reports it. The guard option works the other way round: every field of the guard's message is protected, and
`ignores` names the exceptions. A field someone forgot to think about stays protected, and the worst the omission does
is clear a tick that could have stayed.

Depth follows the same argument. A guard protects everything beneath the fields of its message, nested lists included,
unless `ignores` names the field. Here is a sketch of an evidence-tree node whose claim rests on its sub-nodes:

```proto
message Node {
  bool accepted = 1 [(guard) = {}];                           // a changed sub-node clears it
  // bool accepted = 1 [(guard) = { ignores: ["children"] }]; // judges only this node's own claim
  string id = 2 [(element_key) = true];
  string claim = 3;
  repeated Node children = 4;
}
```

Suppose a curator accepts the PS3 node, whose claim that functional studies support a damaging effect rests on two
children, one per study. The agent later rereads one of the studies and changes that child's claim. Under the default,
the child is part of PS3's protected content, so PS3's acceptance clears and the curator reviews PS3 again. That is
right when accepting PS3 meant accepting the argument beneath it. A schema in which accepting a node judges only its own
claim, with each child accepted separately, declares that with `ignores: ["children"]`. Each child's own `accepted` is a
guard, so it is never part of PS3's protected content, and a curator accepting a child never clears its parent. The
dependency runs one way only: a child's acceptance judges the child's own content, so a change to PS3's claim clears
PS3's acceptance and leaves the children's standing.

A guard declared without anyone thinking about depth then clears a tick too often rather than keeping one too long,
which is the cheaper of the two mistakes (§"A user's judgement stands only while what it judges is unchanged").
`ignores` is for a field the judgement is not about, and naming it is a decision somebody makes on purpose.

A CI test fails a payload type the rule cannot be applied to, such as a list whose elements hold a guard but mark no
key, or an `ignores` entry that is not a field of the guard's message.

The worker checking a push may run a build older than the schema an asset was written with, during a deploy. Such a
build reads the widget options as they were when it was built. The principle for a released type is that an older hook
may check an asset more strictly than a newer one, never more laxly, since a laxer hook would let the agent change
content beside a guard it cannot see. `buf breaking` reads no custom option, so the compatibility gate
([`buf_compat.py`](../../tools/schema/buf_compat.py)) compares the widget options of every released message beside it.
It allows a difference between builds only where one cannot be avoided, and it refuses the rest:

- a released field keeps whether it is a guard and whether it is an element key;
- a guard's `ignores` may gain a field only if the field is new in the same change. An older hook holds that field
  byte-identical as an unknown field, which is stricter;
- a guard's `ignores` may not lose an entry, because an older hook would still ignore the field and let the agent change
  it under a standing judgement;
- a released message gains no guard. An older hook, not knowing the new field is a guard, holds it byte-identical as an
  unknown field, where the newer rule requires it to clear when what it judges changes, so the two builds would
  disagree;
- nor does a released message gain a guard beneath a message that held none, the case below;
- a released message neither gains nor loses its `widget` mark.

The rule descends only into messages that held a guard when a build was made. Suppose an evidence-tree payload was
released with a `Node` that held no guard, and a later change adds `accepted` to it. A hook built before that change
does not look inside `Node` for guards at all, so it would not hold the agent to the new judgement, not even as an
unknown field.

### Each writer's change is checked where it passes

The two writers reach the repository by different routes, and each route checks its writer's side of the rule.

The agent's pushes pass the pre-receive hook. The hook knows nothing about widgets: the sandbox worker hands it this
check, as it hands it the protected paths ([`sheaf.md`](sheaf.md) §"Protecting what the agent must not write"). An
asset, to the hook, is a changed file that parses as a `google.protobuf.Any` naming a type in the widgets package,
`themis.widgets`. Any other file, such as a PNG, a markdown file or bytes that are no `Any`, is not an asset to the hook
and passes it. For each new commit, the check takes every asset the commit writes differently from any one of its
parents, and holds it to the rule against every parent. The protected-path check reads a merge another way. It looks at
what the merge introduces compared with all its parents together, so a merge that takes one side's protected file
unchanged introduces nothing and passes. Under the widget check, a merge that takes one side's asset unchanged still has
to keep the rule against the other side, which is how M in the previous section is refused.

The hook refuses an `Any` that names a widgets-package type its build does not know, or whose payload does not parse,
and names the file, because it cannot read the guards in it. A document that names a file the agent has not written yet
still passes, since the hook reads only the files a push changes.

The agent should rarely meet a refusal, because the same check runs earlier. The helper's `update` carries a judgement
across only where its protected content is unchanged, and reports each judgement it clears, so the agent knows which
items the user has to review again. The helper and the linter both apply the rule before the push, against the versions
the hook will compare the file with, and each failure says how to comply.

The agent rebases onto the user's commits rather than merging them, since a merge that takes in a user's judgement is
refused. Rebasing replays each of the agent's commits on top of the user's latest version, and the sandbox's git rebases
when it pulls. When the rebase stops on a widget file that both sides changed, the agent resolves it with `update`,
which rebuilds the file from the user's version and carries each judgement across where what it judges is unchanged. The
hook checks the result as it checks any push.

On the user's side, a widget's edit changes only guards, and the browser's write path checks that the new payload
differs from its version at the edit's base in guards alone. When the agent has moved the tip, the edit is applied there
only under the condition the walk-through gives. The browser writes the asset in the one framing. The payload inside is
the widget's re-encoding of what it decoded, and that re-encoding carries back unchanged any field the browser's build
does not know ([`workbench-workspace.md`](workbench-workspace.md) §"A curator's edit is rebased onto the tip, file by
file").

The browser's check is the only one a curator's edit meets. The BFF checks only that the curator belongs to the
Analysis's Project, and a publish does not pass the hook. That follows from the trust placed in each writer: the browser
is trusted, so its check guards against a defective widget, one that would change a field it should not. Script running
on the page could publish through the BFF directly and skip the check, and keeping script off the page is the renderer's
job everywhere ([`agent-output-rendering.md`](agent-output-rendering.md)). This write path adds no defence of its own.

### Drawability is checked where the file is, and tolerated where it is shown

The document is the tip of a branch, and a push lands whether or not the document at its tip can be drawn
([`workbench-workspace.md`](workbench-workspace.md)). The linter and the renderer apply the same drawability rules. The
linter applies them while the agent holds the file and can fix it; the renderer applies them to what it is asked to
draw. A document draws in full when:

- `working_document.md` exists at the root of the revision's tree;
- every `::embed` stands alone on a line at the top level of the document, written exactly as `::embed[<path>]` (§"One
  directive, and the file names its own type");
- every path the document names, an `::embed` file or an image, is over the admitted character set and resolves segment
  by segment to a regular file in the tree, with no absolute path and no segment that is empty, `.` or `..`;
- every widget file decodes as a marked type: the `Any` is in the one framing, its type names a message the build knows
  and the option marks, its payload parses as that message, and the message passes protovalidate. Checking the type
  alone would accept bytes that do not decode, and decoding alone would accept a payload missing a field its component
  needs;
- every image's bytes are one of a fixed set of raster formats: PNG, JPEG, WebP or GIF.

For everything that fails, the renderer draws a neutral placeholder naming the path and the problem, and renders the
rest of the document. The client draws a value that a tab's build does not know the same way
([`agent-output-rendering.md`](agent-output-rendering.md)): a partly drawn document is worth more than none, and the tip
is partly drawn whenever the agent has written a directive and not yet its file. A component that fails while drawing is
contained the same way. The placeholder replaces that one widget, and a failure of the document as a whole is confined
to the document pane.

The linter reports each error to the agent, the one party that can fix it, while the agent still holds the file. It
ships into the sandbox beside the helper, reads the marked types from the same descriptors, and reports each failing
directive with the reason, so the agent fixes it before it commits, or after it sees the placeholder in the
conversation. It also reports an `::embed` that is nested or malformed, and a file the push would not carry, one that is
untracked or ignored by git, since the browser only ever sees what was pushed.

Nothing stops a push that fails the linter. The pre-receive hook is the one point the producer cannot skip, so checking
drawability there would mean a stored revision could never be undrawable. But the document is a branch tip, and a gate
there would either refuse the agent's ordinary work-in-progress commits or demand a second act that marks a state
finished. A visible unfinished state costs less than either, and the linter gives the producer the same report where it
can act on it.

The linter parses the document as generic markdown, with a directive rule that understands fences and code spans, and
reads `::embed` lines and images out of the syntax tree. A directive inside a fenced block or a code span is text to the
linter and the browser alike, so it is neither checked nor drawn.

Images are raster because an SVG is a document the browser interprets, with scripts, event handlers and links. Inside an
`<img>` tag the browser neutralises all of that. But the client draws each image from a `blob:` URL over the bytes in
its copy of the repository, and a `blob:` URL is same-origin and can be opened: a curator who opens the image in a new
tab would load the SVG as a page on the workbench's origin, where its script runs in the curator's session. A run that
read a hostile page could write that file. Admitting SVG would need a safeguard that makes the bytes inert wherever they
can be reached. One option is an allowlist of elements and attributes applied by both the linter and the renderer, which
is a second parser to maintain. The other is rasterisation in the client before anything gets a URL. A vector drawing
the agent produces is better expressed as a widget, typed data the client draws. So images stay raster, and whichever
safeguard is chosen is built when a need for SVG figures appears.

### Widgets over live data carry references, not bytes

Some widgets draw data far larger than a revision should hold: aligned reads at a locus, coverage across a region, a
reference track. The file carries what identifies the data (sample ids, the locus, the tracks) and never the data
itself. The client reads ranges through an authenticated app route on the pattern the paper pane uses: the route
resolves the object and redirects to a short-lived signed URL, so the bytes flow between the browser and the bucket
([`document-pane.md`](document-pane.md) §"Backend seam"). The route has its own resolver, scoped to the Project and
pinned to the case-genomes bucket, because a resolver never spans two buckets. Every request signs afresh, so when a
curator loses access, it takes effect at their next read, even in a session left open at one locus.

Size is not the only reason. A pushed revision never changes and stays readable by the Project for as long as it exists,
while an authorisation decision has to be made on each read, against the reader, at the time of the read. Baking a slice
of patient data into a committed file would move that decision to commit time and apply it to the wrong subject.

The data such a widget reads is the Analysis's input. Whether that input can change under an Analysis is a question for
the Analysis rather than the widget ([`workspace-model.md`](workspace-model.md) §"Open questions"), so a widget carries
no checksum or version of the data it names.

The aligned-reads view is the example of this class: its file names the locus, the samples and the tracks, and the
client fetches ranges through the authorising route, with the reference genome and shared tracks resolved the same way.
It depends on things this design does not supply. The case data needs per-sample alignments to read from. And the route
has to resolve a sample to a Project before it signs a read, because a Project is the access boundary over datasets as
well as users ([`workspace-model.md`](workspace-model.md)). In the sandbox, a sample id can only reach the sample
registry, so checking the session is enough. A browser route can be asked for any sample id, so it has to check that the
sample belongs to the curator's Project before it signs.

Ranged reads through a redirecting route rely on browser behaviour defined by the Fetch standard: a browser re-issues a
redirected `GET` with the original headers, dropping only `Authorization` when the redirect crosses origins
([Fetch Standard, "HTTP-redirect fetch"](https://fetch.spec.whatwg.org/#http-redirect-fetch)). So a `Range` header
survives the redirect to the signed URL, and the case-genomes bucket's CORS rule has to admit it.

### The evidence tree and the pedigree, sketched

Beyond the checklist in the walk-through, the design was pressure-tested against three types, none of them declared
here: the evidence tree below, the hardest structure the scenario needs; the pedigree below, the simplest thing that is
not prose; and the aligned-reads view above, whose data cannot live in a revision.

The SVCv4 evidence tree follows the framework's own structure: the routing decisions, then a node per evidence code with
its status (scored, not applicable, no data), the decision-tree cell selected in the framework's vocabulary, the
evidence the call rests on and the reasoning from one to the other, each item that rests on a source carrying its
citation. That is the capture [`curation-surface.md`](curation-surface.md) §"What is captured, and why" fixes for a
curator's worksheet, and the alignment is deliberate: the run-review loop joins the curator's selected cell to the
run's, and a join needs both sides to name the same thing in the same vocabulary. Everything below the top level, such
as how the tree carries the library's audit trail and how strengths and paths are represented, is decided against
SVCv4's own revisions, not here.

A pedigree draws the family behind a proband as the chart a clinical geneticist reads. Its payload names the locus the
chart is about, as a genomic span. How it represents the family is decided with the type, starting from grus
([github.com/populationgenomics/grus](https://github.com/populationgenomics/grus)), CPG's open-source pedigree IR in
protobuf, with a renderer and importers.

## Alternatives considered

- **A JSON payload in a fenced code block.** No proto, no generated code and no separate file, so it is the least
  machinery. It lost because agent-written data would sit in the document as text the client parses and draws, the shape
  [`agent-output-rendering.md`](agent-output-rendering.md) rules out.
- **A shared `Citation` type every payload embeds.** The reveal would be structural in every widget. It lost because it
  fixes a shape before the payload that needs a richer citation exists, and some payloads have no use for the field.
- **Rendering widgets to HTML or SVG on the server.** The client would need no component per type. It lost because it
  puts markup back on the path from agent-written content to the page, and a server-drawn image cannot be interacted
  with.
- **One file per document holding every payload.** Fewer files to manage. It lost because the directive would need an
  index into that file, which is the widget-name coupling again, and one unreadable file would lose every widget rather
  than one.
- **A user's judgement in a file of its own, on a path the agent may not write.** The existing protected-path rule would
  keep the agent out with no new check. It lost because the payload's schema would no longer say which fields hold a
  user's judgement, each widget type would need its own convention for where that file lives and how it refers to the
  agent's items, and the two files could disagree about which items exist. A judgement kept apart from its content also
  could not be cleared when that content changes, since the agent may not write the file that holds it.
- **A git merge driver for widget files.** git would resolve a widget file that both sides of a rebase changed by
  itself, applying the rule, so the rebase would not stop there. It lost because the agent already resolves such a stop
  with one call to `update`, so a driver would only save it a turn.

## Open questions

- **Whether the evidence-tree payload reuses the curation contract's messages or maps onto them.** Sharing them makes
  the join structural, but couples the agent's output to a surface built for a curator working alone, whose future is
  unsettled ([`curation-surface.md`](curation-surface.md) §"Where it lives, and what it is coupled to"). Mapping keeps
  the two free to change and puts the correspondence in code that can drift.

## Appendix: reading an embed's label as source text

The linter and the renderer parse the document with different parsers, and each has to arrive at the same path for a
label. The renderer's markdown parser parses a directive's label as inline content, as it does every label. The renderer
ignores those parsed children. It takes the directive node's position in the source and slices the document's text
between the label's `[` and `]`. The linter takes the same span as source text from its own parse. For
`::embed[assets/_b_.binpb]`:

```
document text                ::embed[assets/_b_.binpb]
renderer's parsed children   "assets/"  emphasis("b")  ".binpb"      ignored
renderer's source slice      assets/_b_.binpb
linter's source text         assets/_b_.binpb
```

Both readers produce `assets/_b_.binpb`, the file the agent wrote. A reader that used the parsed children would look for
`assets/b.binpb`. The slice is right only if the label ends at the first `]` after its `[`, and only if the text between
them means itself. The character set keeps `]` and the backslash out of any admitted path, so a label that passes it
holds no escape and no bracket for the two readers to treat differently.
