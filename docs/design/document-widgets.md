# Design: document widgets

**Related:** [`workbench-workspace.md`](workbench-workspace.md) (the browser's copy of the repository a revision is read
from, and how a curator's edit becomes a commit); [`document-pane.md`](document-pane.md) (the renderer, the citation
directives, and the reveal a citation raises); [`agent-output-rendering.md`](agent-output-rendering.md) (why agent text
never becomes markup, and how the client draws a value that a tab's build does not know); [`sheaf.md`](sheaf.md) (the
pre-receive hook every agent push passes); [`proto.md`](proto.md) (the option pattern, and the compatibility gate that
binds a schema from its first commit); [`svcv4-classification-widget.md`](svcv4-classification-widget.md) (the SVCv4
classification, the richest payload so far).

## Overview

A working document is markdown, and markdown is the wrong medium for a classification table, a pedigree, or a checklist
a curator works through. A model asked to describe such a structure in prose writes a different table on every run, and
nothing marks which numbers in its sentences came from a source and which it recalled. A widget replaces that prose with
a typed block. The agent writes a file holding a serialized protobuf message, names it in the document with one
directive, `::embed[<path>]`, and the client draws it.

The file names its own type as a `google.protobuf.Any`, and a hand-written registry in the client maps each type to the
component that draws it. Because the file is binary, the only way to produce one is to run a serializer over a typed
message. A widget's contents therefore arrive in a declared shape, through code, never hand-typed. An item that rests on
a source carries its own citation. A payload type's schema is reviewed in a change of its own, ahead of the component
that draws it. The schema carries the option that marks it as a widget payload from its first commit, because the
compatibility gate forbids adding that mark to an already-released message. CI fails a marked type that has no registry
entry, so the contract change registers a placeholder entry until the change that ships the component replaces it.

The document is the tip of a branch that the agent pushes to as it works, so at any moment it may name a file the agent
has not written yet. The pre-receive hook, which checks every agent push before it lands, therefore does not check
whether the document can be drawn. Instead, a linter in the agent's sandbox and the renderer in the browser apply the
same drawability rules. The linter reports every directive that would not draw while the agent can still fix it. The
renderer draws a visible placeholder for anything that fails and renders the rest of the document.

Some of a widget's contents are a user's judgement on the rest, such as a curator's tick on a checklist item. A tick
vouches for the item as the curator saw it, so it must not survive the agent changing that item, and the agent rewrites
the same file whenever it revises its work. The payload's schema marks each field that holds a judgement as a guard. By
default, a guard judges every other field of its message and everything beneath them, apart from the item's key and
other guards, so a tick on a checklist item judges that item's label and citation. A guard holds a judgement after an
agent change only when the version before held the same judgement on the same content; in every other case it resets to
its default ("no judgement"). The agent can therefore never set a guard, nor clear one without changing what it judges.
It may still remove an item, and the user's judgement on that item goes with it.

Each writer is held to its side of that rule where its changes pass. A user edits in the browser, which refuses an edit
that changes anything other than guards. The pre-receive hook refuses any agent push that breaks the guard rule, and a
helper in the sandbox builds widget files so the agent does not have to satisfy the rule by hand. The helper starts from
the committed version and carries each judgement across wherever what it judges is unchanged. When the agent's work and
a user's tick meet, the agent rebases onto the user's commits rather than merging them. If the rebase stops on a widget
file, the helper rebuilds the file from the user's version, keeping the user's ticks by the same rule the hook checks.

## Background

One shared markdown component
([`apps/web/src/components/workbench/markdown.tsx`](../../apps/web/src/components/workbench/markdown.tsx)) draws both
the working document and the conversation. The agent writes inline citation directives into them: `:paper[id]`, which
cites a paper, and `:quote[id, text]`, which cites a passage in it. The renderer turns each into a mark a curator can
click to open the paper beside the text ([`document-pane.md`](document-pane.md) §Reveal). Anything added to that
pipeline inherits its constraints. Nothing rewrites the served markdown, because `Locate`—the RPC that finds a quoted
passage—returns code-point offsets into that exact text. And the renderer turns any directive it does not recognise back
into literal text, because `remark-rehype` (which turns the markdown syntax tree into page elements) would otherwise
drop an unhandled directive and the token after it.

A model writes the document, often quoting pages it has read, so the renderer turns agent text into React elements
rather than HTML ([`agent-output-rendering.md`](agent-output-rendering.md)). Nothing the agent writes may choose how it
is drawn, carry styling, or introduce an arbitrary element.

A revision of the document is a commit of the Analysis's workspace repository, and the document is the file
`working_document.md` at the tip of its collaborative branch. The browser keeps its own copy of the repository and reads
the document, along with every file the document names, at one commit
([`workbench-workspace.md`](workbench-workspace.md)). A commit never changes, so a path in a directive refers to one
file for as long as that revision exists.

Two writers change that branch. The agent pushes from its sandbox, and every push passes sheaf's pre-receive hook, which
runs outside the sandbox and can refuse the push before any ref moves ([`sheaf.md`](sheaf.md)). The hook runs in the
sandbox worker, the trusted process that holds the session's credentials and runs the agent's commands in an isolated
guest ([`sandbox-worker.md`](sandbox-worker.md)). The browser commits a curator's edit and publishes it through the BFF
(the backend for the web tier) without passing the hook. Curators act in good faith, and the browser is a trusted
writer, as the sandbox worker is. The agent is the untrusted writer.

Clinical work has a shape that prose loses. An SVCv4 classification is a set of criteria, each with a status, a selected
outcome, evidence, and reasoning. Written as markdown, the table changes form from run to run, which defeats scanning
it, comparing runs, and seeing which criteria went unanswered. A reader cannot tell whether the model retrieved or
recalled a number in a sentence, and the product rule that every claim cites a reproducible source
([`../PRODUCT.md`](../PRODUCT.md) §6) cannot be enforced against free prose. Drawing is also deterministic work, so the
principle that puts parsing and arithmetic in code while keeping the model for judgement applies to layout as well.

## Non-goals

- **Presentation in the payload.** No payload field holds a colour, a width, a template, or a label drawn as chrome. The
  client owns the drawing, which keeps widgets inside the rule that agent text never becomes markup.
- **Computing a classification.** A widget draws what its file carries. Points, totals, and bands come from the
  `themis.svcv4` library, and a second implementation inside a renderer would be a second thing to audit
  ([`curation-surface.md`](curation-surface.md) §Non-goals).
- **Widgets in the conversation stream.** A widget's file is found relative to a document at a commit, and the stream
  has no commit to resolve it against, so the mechanism itself excludes stream widgets. The stream draws each tool call
  from the projection, which is the BFF's reading of the call's inputs into a label and a body
  ([`agent-output-rendering.md`](agent-output-rendering.md)).
- **Typed values inside a sentence.** A value in a sentence is still typed by the model, so the argument for widgets
  applies to it too. A directive naming a field of a file would be cheap to resolve, but each kind of value (a
  frequency, a p-value, a coordinate) has its own formatting rules, and a layer that formats one wrongly mid-sentence is
  worse than the model typing the number. That layer has decisions of its own, so this design stops at block widgets and
  leaves typed values in prose for a later design.

## Design

### One widget, end to end

A checklist—the widget type this doc uses as its running example—illustrates the path from the agent's write to a
curator's tick. The agent builds a checklist message with the widget helper in its sandbox
([`themis/services/sandbox_worker/guest/widgets.py`](../../themis/services/sandbox_worker/guest/widgets.py)), which
serializes the message, wraps it in a `google.protobuf.Any`, and writes the widget's file, called its *asset*
([`themis/widgets/`](../../themis/widgets/)). The message is shown below in protobuf text format; its schema is
[`checklist.proto`](../../schema/proto/themis/widgets/models/checklist.proto).

```
assets/review.binpb        a google.protobuf.Any
  type_url   type.googleapis.com/themis.widgets.models.Checklist
  value      items:
               { id: "PS3", label: "Functional studies support a damaging effect",
                 citation: { doc_id: "1111…", quote: "…loss of channel function…" }, checked: false }
               { id: "PM2", label: "Absent from population controls", checked: false }
```

The agent names the file on a line of its own in `working_document.md`, runs the document linter
([`themis/document_linter/`](../../themis/document_linter/)), and commits and pushes both files:

```
### Curator checks

::embed[assets/review.binpb]
```

The browser reads the document at the new commit from its copy of the repository. For the directive, it reads
`assets/review.binpb` at the same commit, decodes the `Any`, looks up `themis.widgets.models.Checklist` in its widget
registry ([`apps/web/src/components/widgets/`](../../apps/web/src/components/widgets/)), and draws the checklist
component in place of the line:

```
 Curator checks
   [ ] PS3  Functional studies support a damaging effect   ⧉ source
   [ ] PM2  Absent from population controls
```

A curator ticks PS3. The widget sets PS3's `checked` field in the asset as it stood at the commit on screen, and the
browser publishes the new file as a commit of its own on top of that commit. If the agent has moved the branch in the
meantime, the browser applies the edit on the new tip only if the asset there has the same blob and mode as the version
the curator saw. Otherwise, the browser asks the curator to redo the tick on the new version
([`workbench-workspace.md`](workbench-workspace.md) §"A curator's edit is rebased onto the tip, file by file").

When the agent later revises the checklist, it rebuilds the file with the helper, which carries PS3's tick across for as
long as PS3's label and citation are unchanged. If the agent changes PS3's label or citation, the tick clears and the
curator reviews PS3 again. The pre-receive hook refuses any push that drops the tick from an unchanged PS3 or keeps it
on a changed one. The sections below explain each step.

### One directive, and the file names its own type

The obvious syntax names the widget type in the directive, such as `::pedigree[…]`, which reads naturally in the
markdown source. That breaks because the asset file already names its own type, and two declarations of one fact can
disagree: a `::pedigree` pointing at a file of aligned reads is a mismatch somebody would have to define behaviour for,
and a rule the agent would have to learn. Instead, the directive names only the file, and the file's type decides what
draws it. Adding a widget type then touches no markdown grammar; it requires only a payload schema and a component, and
the directive set stays at `:paper`, `:quote`, and `::embed`.

`::embed[assets/review.binpb]` is a leaf directive whose label is a path relative to the document. Its two colons are
the directive grammar's block form, whereas the single colon on `:paper` and `:quote` is the inline form. A citation
renders inside the paragraph that holds it, while a widget is a block: placing a block chart inside a paragraph produces
invalid HTML that the browser repairs by closing the paragraph early, so the rendered page would no longer match the
document structure.

The renderer draws an embed only when it stands on a line of its own at the top level of the document, written as
`::embed[<path>]`. Anything else that starts with `::embed`—such as an embed inside a list, a block quote, or a
footnote, or one with text after the label—draws the placeholder. Markdown nests blocks in more ways than two parsers
reliably agree on, and the sandbox's linter (`markdown-it-py`) and the browser's renderer (`remark-directive`) use
different parsers. Restricting `::embed` to a top-level line of its own gives one rule that both parsers apply
identically, and it costs the agent nothing it needs.

The same concern shapes how both parsers read the directive's label. In the directive grammar, a label is inline
markdown, so a parser that interprets inline syntax reads `assets/_b_.binpb` as emphasis and `www.x.binpb` as a link. If
the linter and the browser interpreted the label, they could resolve one directive to two different files. Neither
interprets it. The renderer ignores the parsed inline children of the directive node and slices the source text between
the label's `[` and `]`, and the linter takes the same source span from its own parse:

```
document text                ::embed[assets/_b_.binpb]
renderer's parsed children   "assets/"  emphasis("b")  ".binpb"      ignored
renderer's source slice      assets/_b_.binpb
linter's source text         assets/_b_.binpb
```

Both readers produce `assets/_b_.binpb`, whereas a reader that used the parsed inline children would look for
`assets/b.binpb`. Slicing the source text works only if the label ends at the first `]` after its `[` and the characters
between them mean themselves. The linter and the renderer therefore admit paths only over a conservative character set:
ASCII letters, digits, `-`, `_`, `.`, and `/`. That keeps the path a plain relative path on every filesystem, and it
excludes `]` and `\`, the two characters that could make the readers disagree on where a label ends. The linter refuses
a path outside the character set and the renderer draws the placeholder for one, prompting the producer to rename the
file.

The renderer resolves the path against the tree of the revision at render time, so nothing rewrites the markdown and
`Locate`'s offsets stay valid. A surface turns widgets on by giving the markdown component a way to read files at its
revision, just as it turns citations on by passing a click handler. A surface that does not pass a revision, such as the
conversation stream, renders the directive as literal text.

### The file is a binary `Any`, so a program built it

The obvious file format for a widget asset is JSON, which is human-readable and diffs cleanly in git. JSON fails here
because the agent is a language model: it can write JSON by hand, and when a tool call fails and the file looks easy to
patch, it will. A model cannot hand-write a serialized protobuf. Producing those bytes requires running a serializer
over a typed message, so the Python type checker and protovalidate rules apply to a widget's contents just as they apply
to an RPC's. The helper in the sandbox builds the message, validates it, wraps it, and writes it.

The file contains the serialized `Any` and nothing else, with no outer envelope for who made it or when. The git commit
already records when the file was produced, and the run's trace records what produced it, so the file carries only the
payload. A `created_at` field filled in by the model would only be another field the model could get wrong. An envelope
would also record provenance at the wrong granularity: provenance belongs on each item, as the citation pattern below
shows.

The asset format ([`themis/widgets/asset.py`](../../themis/widgets/asset.py),
[`apps/web/src/widgets/asset.ts`](../../apps/web/src/widgets/asset.ts)) also holds the `Any` to one canonical framing:
the type URL field, then the payload bytes field, and no other fields. Protobuf parsers disagree on some malformed wire
bytes. If the pre-receive hook's parser and the browser's parser read a different type URL or different payload bytes
out of one file, the hook could treat the file as a non-widget while the browser drew a ticked checklist whose tick was
never checked. In the canonical framing, every parser reads the same type URL and the same payload bytes. The linter and
the hook refuse any file that names a widget type in another framing. A file the hook cannot parse as an `Any` at all
passes the hook as a non-asset, and because the renderer draws only the canonical framing, such a file never draws as a
widget.

A binary format cannot prove that a value was retrieved from a database rather than recalled by the model, because the
model still chooses what arguments it passes to the constructor. Provenance of values is the concern of the evidence
services the agent reads its values from. What the binary format guarantees is that every value went through a declared
schema and a program, arriving in a named, typed field with a slot for its citation, in a shape the client and the
schema agree on.

### Where structure lives

An alternative way to get structure out of a working document is to parse it out of the prose, using a tag vocabulary
inside the markdown or a parser over headings. A vocabulary shared across the model prompt, a parser, and a renderer
drifts over time, and a parser over prose fails silently: a heading one level off drops a criterion without reporting an
error. The curation surface makes the same choice for a curator's worksheet, which is a form backed by a schema rather
than parsed notes ([`curation-surface.md`](curation-surface.md) §"What is captured, and why").

Widgets apply that rule to the agent's side. The markdown carries no schema, because `::embed` names a file and says
nothing about its contents. All structure lives in the asset file, where the protobuf schema, the compatibility gate,
and the linter apply to it. Anything that must be structured gets a declared schema, and nothing parses prose for
structure.

### Declaring a widget type

A payload type is a protobuf message in the `themis.widgets.models` package, marked with the `widget` message option
([`widget.proto`](../../schema/proto/themis/widgets/models/widget.proto)). That option follows the same pattern as the
method option that marks an RPC the sandbox may call ([`sandbox-rpc-exposure.md`](sandbox-rpc-exposure.md)): one
declaration in the schema, read from the descriptor pool, where absence means false. The linter reads the set of marked
types from the descriptor pool, so the types it accepts cannot drift from the schema contract.

Engineers maintain the web client's registry
([`apps/web/src/components/widgets/registry.tsx`](../../apps/web/src/components/widgets/registry.tsx)) by hand, because
each entry maps a payload schema to a bespoke React component. A generated registry would have to derive each
component's name from the protobuf message name; nothing would check that naming convention at compile time, and a
mismatch would surface only as a missing component at render time. Writing the registry by hand gives up automatic
registration, and a CI pairing test restores the guarantee: it fails whenever a message marked `widget` has no entry in
the registry, so no payload type ships without one.

A payload type's schema lands in a contract change of its own, ahead of its component, and is reviewed as an interface
([`review-policy.md`](review-policy.md)). The compatibility gate
([`tools/schema/buf_compat.py`](../../tools/schema/buf_compat.py)) and the pairing test together determine what that
contract change contains. The compatibility gate compares every committed proto from the change that first commits it,
and it refuses any change that adds the `widget` mark or a guard option to an already-released message, because a push
hook built before that change would check the message's assets differently from a newer build (§"A guard protects
everything beside it unless it says otherwise"). The `widget` mark and the ownership options on the payload's fields
therefore land with the message in the contract change. Because the pairing test requires every marked type to appear in
the registry, the contract change also registers the new schema with a `placeholder` entry, just as a service contract
change stubs the RPCs whose implementation lands later.

| Change               | What it does to the schema                                       | What the registry maps the type to |
| -------------------- | ---------------------------------------------------------------- | ---------------------------------- |
| The contract change  | declares the message, with its widget mark and ownership options | the placeholder                    |
| The component change | changes it additively, if at all                                 | the component                      |

The `placeholder` entry draws the same neutral box that the renderer shows when a tab's build has no component for a
type, naming the asset's path and stating that the build does not draw it (§"Drawability is checked where the file is,
and tolerated where it is shown"). It still parses the payload and validates its rules, so an asset written against the
new type during component development is checked the same way as under the finished component.

Engineers develop and test components against committed example payloads in
[`apps/web/src/widgets/examples/`](../../apps/web/src/widgets/examples/), written in protobuf text format so humans can
read and edit them. A local page on the fixture backend (`/dev/widgets`) opens each example in a working document drawn
by the real component, where ticks and notes work as they do in the workbench
([`widget-browser.md`](../runbooks/widget-browser.md)). A CI test validates every committed example against its schema
rules, so an example that falls out of step with its schema fails CI.

Because the compatibility gate holds the schema from the contract change onward, anything component development learns
about the payload lands as an additive change, such as a new field or a new message beside the old one. Any structural
reshape that the gate would refuse has to be settled while the contract change is under review.

From its first push on a deployment whose data is kept, a payload schema is persisted data. A pushed revision is never
deleted ([`sheaf.md`](sheaf.md)), and `buf breaking` holds the schema's fields to additive evolution
([`proto.md`](proto.md) §Schema evolution), while `buf_compat.py` checks the widget options (§"A guard protects
everything beside it unless it says otherwise"). Retiring a type is therefore a data migration rather than the removal
of a registry entry: a type referenced by any pushed revision must keep decoding, with a converter feeding its
successor's component. For a type a tab's build does not yet know, the placeholder covers only the minutes of a
deployment while an open tab still runs the previous build. A revision from years ago still draws in full.

### Patterns a payload follows

Several field shapes recur across widget payloads. Each is a framework convention rather than a rule for a single
widget.

An item that rests on a source carries its own citation, consisting of a litcache `doc_id` and an optional verbatim
`quote`. Clicking the citation opens the paper and highlights the quote in the document pane, just as `:paper` and
`:quote` in prose do, and a malformed ID draws the same visibly broken mark rather than guessing at a match. The
citation sits on the individual item rather than on the asset file because different items in one widget (such as the
criteria of an SVCv4 classification) rest on different sources. Citation remains a field pattern rather than a shared
message type because not every payload cites literature (an aligned-reads view cites nothing; the reads themselves are
the evidence), and a future citation into a figure or table may need more than a document ID and a quote.

A payload about a place in the genome identifies that place as an interval: the shared
`themis.evidence.models.GenomicSpan` paired with the chromosome accession it lies on, matching the evidence service
contracts. Normalising an allele belongs to the evidence layer, and a widget carrying a normalised variant ID would
become a second place that decides variant identity. Carrying the locus on each genomic widget means a document that
embeds both a pedigree and an aligned-reads view states which locus each widget describes, helping both a reader who
arrives at a widget out of context and an analysis whose document weighs more than one candidate variant.

### A user's judgement stands only while what it judges is unchanged

Most of a widget payload is written by the agent: a checklist's items, their labels, and their citations. Specific
fields record a user's judgement on that content, such as whether a curator ticked an item. Both live in one asset file,
and the agent rewrites that file whenever it revises its work. In the walk-through above, the curator ticked PS3.
Suppose the agent then rebuilds the checklist from its own notes, where PS3 is unticked, and pushes. Nothing in the file
format stops that push from silently undoing the curator's tick, and nothing stops the agent from setting
`checked: true` itself, which the workbench would draw identically to a curator's tick.

The obvious rule is to mark user-written fields with a `written_by` option in the schema, match list items across
versions by a key, and require the agent to preserve the user's fields on every item it keeps. That prevents the agent
from forging a tick or dropping one when rebuilding the list, but it fails because a tick is a judgement on a specific
statement and citation, and nothing in that rule ties the tick to them. On the dev deployment, an agent commit
relabelled every item of a checklist in which a curator had ticked two items; both ticks survived because the item keys
had not changed. Under that rule, the agent could reword "PM2 applies" to "PM2 does not apply" and keep the curator's
tick, leaving the tick to vouch for a claim the curator never saw. Clearing a tick when it could have stayed costs the
curator one click to re-apply it, whereas keeping a tick after what it judged has changed leaves a false attestation in
the clinical record.

The design therefore ties each judgement to the content it judges
([`themis/widgets/ownership.py`](../../themis/widgets/ownership.py),
[`apps/web/src/widgets/ownership.ts`](../../apps/web/src/widgets/ownership.ts)). The schema marks each field that holds
a user's judgement with the `guard` field option. A guard's default value (unset) means "no judgement". For a plain
proto3 `bool` such as `checked`, unset and `false` serialize to the same empty bytes. For a guard with explicit
presence, such as a message or an `optional` scalar, an explicit `false` is a judgement and "no judgement" is unset. The
agent never sets a guard: it either carries a user's existing judgement across or leaves the guard unset.

To compare two versions of an asset, an *element* is an entry in a repeated message field, such as one item of a
checklist. The ownership check matches elements across two versions of a list by a scalar field marked with the
`element_key` option (here, the item's `id`). The payload's root message counts as a single element with no key. An
`element_key` field on a singular message or on the root message is ordinary content: if the agent changes it, it has
changed what the judgement is about.

What a guard judges is its *protected content*. By default, a guard's protected content is every other field of the
guard's message and everything beneath those fields at any depth, except for:

- The element's `element_key`, which is how the element is matched across versions and is therefore outside the
  protected content of that list element.
- Every guard at any depth, so one user judgement never clears another.
- Any field listed in the guard's `ignores` option (along with everything beneath that field), because the schema
  declares that this judgement is not about that field.

A guard's position in its message and its field number do not affect what it protects. By convention, a payload schema
declares its guards first, starting at field 1, so a reader meets the judgement before the fields it judges, and fields
added later take higher numbers rather than landing between a guard and the content it protects. Because a released
message cannot gain new guards (§"A guard protects everything beside it unless it says otherwise"), all of a message's
guards are declared in its initial commit and can always occupy its first field numbers.

A guard's scope is bounded by its own message. A tick on one checklist item covers that item alone, not neighbouring
items or the list that holds them:

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

The ownership check compares protected content by its bytes under deterministic serialization. It compares a repeated
message field whose elements match by `element_key` as a set of elements, so reordering the list does not count as a
change to the protected content.

When the agent changes an asset, a guard on an element holds a judgement if and only if the version before the change
had the same element, with the same protected content (including any unknown fields) and the same judgement value. In
every other case, the guard must be at its default value:

- On a new element, all guards are at their default value, because there was no element before the change to hold a
  judgement.
- On an element present before and after the change, a guard whose protected content is unchanged stays unchanged, and a
  guard whose protected content changed resets to its default value.
- An element may be removed. If a later change adds an element back under its old key or a new one, its guards start at
  their default value.
- Removing a singular submessage that holds a guard clears that guard, because the judgement and what it judged are
  removed together.
- Reordering a keyed list changes nothing, neither for the guards on its elements nor for any guard on an enclosing
  message that protects the list.

For that comparison to be well-defined across builds and inputs:

- On an element present before and after the change, any unknown fields (written by a newer schema than the checking
  build knows) must stay byte-identical, and a new element may carry no unknown fields.
- A keyed list may not contain two elements with the same `element_key`, because matching an element across versions
  would be ambiguous.

Consequently, the agent can never move a guard to a non-default value; only a user creates a judgement. Nor can the
agent perform a bare retraction by clearing a guard while leaving its protected content unchanged. A bare retraction
would overrule the curator on content they had already reviewed and accepted—which is what happens when an agent
rebuilds a checklist from stale notes. To clear a judgement, the agent must either change the content the judgement is
about or remove the element.

The agent does not have to maintain guards by hand. It updates an asset through the sandbox helper's
`update(path, build)` function. The caller's `build` callback returns the new payload with every guard left at its
default value. `update` matches elements against the committed version at `HEAD` by `element_key`, copies each guard
across where its protected content is unchanged, and prints a line to stderr for each judgement it cleared explaining
which field changed. Below, a curator has ticked `PM2` in the committed file; the first two rows show `update` carrying
or clearing the tick, and the last two show what happens when the agent writes the file directly and bypasses `update`:

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

If a push bypasses `update`, sheaf's pre-receive hook refuses the push and returns `remote:` diagnostic lines to
`git push`. Each refusal names the asset path, the element and guard (such as `items[PM2].checked`), what the change did
to the guard, and how to comply (keep the user's value, change the content it judges, or remove the element).

The rule allows removing an element because the agent's analysis genuinely changes shape over time: it may split a
criterion in two or drop an item that no longer applies. Removing an item costs the user's judgement on that item, which
the curator must give again. The agent's prompt instructions state this cost so that it restructures a reviewed widget
only when the analysis requires it. Moving an asset file to a new path has the same effect: the file at the new path is
a new asset, and all of its guards start at their default values.

Holding unknown fields byte-identical on kept elements (and forbidding them on new elements) covers payloads written by
a newer build than the one checking the push. A build that does not recognise a field number cannot tell whether that
field is a guard or protected content. Requiring unknown fields to stay byte-identical on existing elements and absent
on new elements satisfies both possibilities: if the unknown field is a guard, the agent cannot set or alter it; if it
is content, the agent cannot change it under a standing guard.

That same constraint closes a wire-parsing gap that canonical `Any` framing alone does not cover (§"The file is a binary
`Any`, so a program built it"). Canonical `Any` framing ensures every parser sees the same payload bytes, but different
protobuf implementations (`upb` in Python vs `@bufbuild/protobuf` in the browser) handle mismatched wire types on known
fields differently. The Python parser refuses any payload that encodes a known field with a wire type other than its
schema's, and any other unrecognised tag becomes an unknown field that the ownership rule forbids adding or changing. As
a result, a payload that the browser would read differently from the hook cannot carry a forged or altered tick past the
hook.

A merge commit has more than one parent, and the ownership rule must hold against every parent individually
([`themis/widgets/commits.py`](../../themis/widgets/commits.py)). Suppose the branch is at `c41e…`, where a curator has
ticked PM2. The curator's commit `U` on `c41e…` unticks PM2. The agent's commit `A`, also branched from `c41e…`, edits
only PS3 and still has PM2 ticked. A merge commit `M` takes the agent's side of the file:

```
c41e…  PM2 ticked
  ├── U  curator unticks PM2 ───────────┐
  └── A  agent edits PS3, PM2 ticked ───┴── M  merge keeping A's file
```

`M` matches parent `A` byte-for-byte on PM2, with the same content and the same tick. If the hook checked `M` against
only one parent or checked only what `M` introduced beyond both parents together, `M` would pass and silently undo the
curator's untick in `U`. Checked against `U`, `M` sets a tick on PM2 where `U` had none, so the hook refuses `M`.
Requiring a merge commit to satisfy the rule against every parent means the agent cannot merge a branch in which a user
edited a widget file, because keeping the user's guard change violates the rule against the agent's parent, while
dropping it violates the rule against the user's parent. Instead, the agent rebases onto the user's commits (§"Each
writer's change is checked where it passes").

The schema needs no separate option declaring who writes a field. Because the browser's write path allows a user edit to
change only guards (§"Each writer's change is checked where it passes"), the guards are the exact set of user-writable
fields, and a second option stating the same fact could only disagree with the first.

Other models for keeping judgements valid were rejected:

- **Freezing a judged element.** Preventing the agent from changing any field protected by a set guard would preserve
  every tick, at the cost of making reviewed work uneditable. When new evidence arrives, the agent must be able to
  revise a claim a curator has already ticked. Under a freeze, the agent could revise the claim only by deleting the
  element and re-adding it under a new key, which still loses the tick and obscures that the element is the same one.
- **Stamping each judgement with `judged_commit`.** Recording the commit hash on which the user made each judgement
  would let the tick stay in the file after an edit and let readers compute whether the item had changed since. That
  preserves history inside the file, at the cost of requiring every reader—including the agent—to diff git history
  before trusting any tick. A reader that forgot to check history would mistake a stale tick for a current one. Clearing
  the guard on change means any tick present in the file always applies to the content beside it.

### A guard protects everything beside it unless it says otherwise

The obvious way to specify what a guard judges is an explicit allowlist, such as a `protects` option naming the fields
the tick covers. While an allowlist makes the guard's scope explicit at declaration time, it fails unsafely when a
schema evolves: if an engineer adds a field later and forgets to add it to `protects`, the new field is unprotected, and
the agent can change it under a standing tick without clearing the judgement. The `guard` option inverts that default:
every sibling field in the message is protected automatically, and `ignores` names the exceptions. If an engineer adds a
field and forgets to consider `ignores`, the new field is protected by default, and the worst consequence is clearing a
tick that could have stayed.

Nested messages follow the same rule. A guard protects everything beneath the fields of its message, including nested
lists, unless `ignores` names the field. Consider an evidence-tree node whose claim rests on its child nodes:

```proto
message Node {
  bool accepted = 1 [(guard) = {}];                           // a changed sub-node clears it
  // bool accepted = 1 [(guard) = { ignores: ["children"] }]; // judges only this node's own claim
  string id = 2 [(element_key) = true];
  string claim = 3;
  repeated Node children = 4;
}
```

Suppose a curator accepts the PS3 node, whose claim that functional studies support a damaging effect rests on two child
nodes, one per study. The agent later re-reads one study and changes that child's `claim`. Under the default rule, the
child's content is part of PS3's protected content, so PS3's `accepted` guard clears and the curator reviews PS3 again.
That matches a tree where accepting a parent node means accepting the argument beneath it. A schema in which accepting a
node judges only its own `claim`—with each child accepted independently—declares `ignores: ["children"]`. Even under the
default rule, each child's own `accepted` field is a guard and therefore excluded from PS3's protected content, so a
curator ticking a child never clears its parent. The dependency runs upward only: changing PS3's own `claim` clears
PS3's `accepted` guard while leaving the children's `accepted` guards intact.

A CI schema check (`check_schema` in [`themis/widgets/ownership.py`](../../themis/widgets/ownership.py) and
[`apps/web/src/widgets/ownership.ts`](../../apps/web/src/widgets/ownership.ts)) rejects any payload schema to which the
ownership rule cannot be applied, such as a repeated message that holds a guard without marking a single scalar
`element_key`, a map whose values hold guards, a closed enum, or an `ignores` entry that names a non-existent field, an
`element_key`, or another guard.

During a deployment, the sandbox worker checking a push may run a build older than the schema with which an asset was
written. That older worker reads the widget options as they stood when it was built. For a released message, an older
hook may check an asset more strictly than a newer hook, never more laxly, because a laxer hook would let the agent
change content beside a guard the older build does not know about. Because `buf breaking` does not inspect custom
protobuf options, the compatibility gate ([`buf_compat.py`](../../tools/schema/buf_compat.py)) compares the widget and
ownership options of every released message against the baseline ref:

- A released field cannot gain or lose `guard` or `element_key`.
- A guard's `ignores` list may gain a field only if that field is added in the same change. An older hook treats the new
  field as an unknown field and holds it byte-identical, which is stricter than ignoring it.
- A guard's `ignores` list cannot drop a field, because an older hook would still ignore that field and let the agent
  change it under a standing judgement.
- A released message cannot gain a new guard field. An older hook, not knowing the new field is a guard, would hold it
  byte-identical as an unknown field, whereas the newer build requires it to clear when its protected content changes,
  so the two builds would disagree.
- A released message that previously held no guards (directly or in nested messages) cannot gain a guard beneath it. The
  ownership check descends only into submessages that hold guards; an older hook would not descend into that submessage
  at all, leaving the new guard unchecked even as an unknown field.
- A released message cannot gain or lose the `widget` option.

### Each writer's change is checked where it passes

The agent and the curator write to the repository through different paths, and each path enforces its writer's side of
the ownership rule.

Every agent push passes sheaf's pre-receive hook. Sheaf itself is domain-agnostic; the sandbox worker passes it the
widget content check
([`themis/services/sandbox_worker/widget_check.py`](../../themis/services/sandbox_worker/widget_check.py)) alongside the
protected-path list ([`sheaf.md`](sheaf.md) §"Protecting what the agent must not write"). To the hook, a widget asset is
any changed regular file that parses as a `google.protobuf.Any` whose type URL falls under `themis.widgets.`. Any other
file—such as a PNG, a markdown file, or bytes that do not parse as an `Any`—is not a widget asset and passes the widget
check untouched. For each pushed commit, the check inspects every widget asset that differs from any parent and verifies
the ownership rule against every parent individually. That differs from sheaf's protected-path check on a merge commit:
the protected-path check compares a merge against all of its parents together, so a merge that takes one parent's
version of a protected file unchanged introduces no new change and passes. The widget check compares a merge against
each parent separately, which is why merge `M` in the previous section is refused.

If a changed `Any` names a `themis.widgets.` type that the worker's build does not know, or if its payload bytes fail to
parse or use mismatched wire types, the hook refuses the push and names the file, because it cannot check the guards in
that file. A commit whose `working_document.md` embeds a path the agent has not written yet still passes the hook,
because the hook inspects only the files changed in the push.

In normal operation the agent does not hit hook refusals, because the sandbox runs the same checks before pushing. The
helper's `update` function carries judgements across only where their protected content is unchanged and logs any
cleared judgements to stderr so the agent knows which items need fresh curator review. Both `write`/`update` in the
helper and `themis.document_linter` run `against_push` ([`themis/widgets/commits.py`](../../themis/widgets/commits.py))
against `HEAD` and any unpushed commits on the branch, reporting violations with instructions on how to fix them before
`git push` runs.

Because a merge commit that incorporates a user's guard edit is refused by the hook, the agent rebases onto the user's
commits (`git pull --rebase`) instead of merging them. Rebasing replays the agent's commits on top of the user's latest
commit. When a rebase stops on a widget file that both the user and the agent changed, the agent resolves the conflict
by running `update`, which reads the user's committed version at `HEAD` and carries each judgement across wherever its
protected content matches the agent's new version.

On the curator's side, a widget edit in the browser may change only guards. Before publishing an edit, the browser's
SharedWorker runs `checkUserEdit` ([`apps/web/src/widgets/user-edit.ts`](../../apps/web/src/widgets/user-edit.ts)) to
verify that the new asset bytes keep the same payload type, use the canonical encoding, pass validation, and differ from
the base commit's version in guards alone. When the agent has moved the branch tip while the curator was viewing the
document, the browser's SharedWorker
([`apps/web/src/workspace-copy/publish.ts`](../../apps/web/src/workspace-copy/publish.ts)) rebases the curator's commit
onto the new tip only if the asset file at the tip has the same blob and mode as the base commit the curator edited.
When re-encoding the edited payload, the browser preserves any unknown fields from a newer agent build unchanged
([`workbench-workspace.md`](workbench-workspace.md) §"A curator's edit is rebased onto the tip, file by file").

`checkUserEdit` in the browser is the only content check a curator's edit passes; the BFF checks only that the curator
is authorised for the Analysis's Project, and curator publishes do not run the pre-receive hook. That matches the trust
boundary: the browser is a trusted writer, so `checkUserEdit` guards against a bug in a widget component that might
accidentally mutate an agent-owned field. Malicious script running in the page could call the BFF directly and bypass
`checkUserEdit`, and keeping script out of the page is the markdown and widget renderer's responsibility everywhere
([`agent-output-rendering.md`](agent-output-rendering.md)).

### Drawability is checked where the file is, and tolerated where it is shown

Because `working_document.md` sits at the tip of the working branch, every push lands whether or not the document at its
tip is currently drawable ([`workbench-workspace.md`](workbench-workspace.md)). The sandbox linter and the browser
renderer apply the same drawability rules at different points in the lifecycle: the linter runs while the agent holds
the workspace and can fix problems, and the renderer runs when displaying a revision. A document draws in full when:

- `working_document.md` exists at the root of the revision's git tree;
- every `::embed` stands alone on a line at the top level of the document, written as `::embed[<path>]` (§"One
  directive, and the file names its own type");
- every path the document names (for an `::embed` asset or an image) uses only characters from `A-Z`, `a-z`, `0-9`, `-`,
  `_`, `.`, and `/`, and resolves segment by segment to a regular file in the revision's tree, with no leading `/`, no
  empty, `.`, `..`, or `.git` segments, and no symbolic links;
- every widget asset decodes as a marked payload type: the outer `Any` is in the canonical framing, its type URL names a
  message in the descriptor pool marked with `(themis.widgets.models.widget) = true`, its payload bytes parse as that
  message with matching wire types, and the message passes its protovalidate rules;
- every image file's bytes match one of the admitted raster formats: PNG, JPEG, WebP, or GIF.

Whenever any of those conditions fails for an embed, the renderer draws a neutral placeholder
([`apps/web/src/components/widgets/placeholder.tsx`](../../apps/web/src/components/widgets/placeholder.tsx)) naming the
path and the reason, and renders the rest of the document normally. That matches how the client handles an unknown enum
or oneof value during a rolling deploy ([`agent-output-rendering.md`](agent-output-rendering.md)): a partially drawn
document is more useful than a blank pane, and the branch tip is legitimately incomplete whenever the agent has
committed `working_document.md` before writing all of its assets. A runtime error thrown inside a widget component is
caught by a React error boundary around that single embed and replaced by the same placeholder, isolating the failure to
that widget.

The linter ([`themis/document_linter/`](../../themis/document_linter/)) reports every drawability or ownership problem
to the agent while the agent can still fix it before pushing. It runs in the sandbox alongside the widget helper, reads
the marked types from the same protobuf descriptors, and checks both markdown syntax and git tracking state: if an
embedded file is untracked or ignored by git, the linter flags it because no push would carry it to the browser.

Nothing in the pre-receive hook blocks a push that fails the linter. Enforcing drawability in the hook would guarantee
that every stored revision renders without placeholders, at the cost of either rejecting the agent's intermediate
work-in-progress commits or requiring a separate "publish finished document" step. Tolerating an unfinished state at the
branch tip with a visible placeholder is simpler than either alternative, and the linter gives the agent the exact same
diagnostics inside the sandbox.

Both the linter and the renderer parse the document as CommonMark with GFM tables and leaf directives, so an `::embed`
inside a fenced code block or inline code span is treated as literal code text by both and is neither linted nor drawn.

Images in the working document are restricted to raster formats (PNG, JPEG, WebP, GIF) because SVG is an executable
document format that can contain `<script>` tags, event handlers, and external links. Although browsers disable scripts
when rendering an SVG inside an `<img>` tag, the workbench serves workspace files from its local copy via same-origin
`blob:` URLs: if a curator opened an SVG image in a new tab, the browser would execute its scripts in the workbench's
origin under the curator's session. A prompt-injected agent that read a hostile web page could write such an SVG into
the workspace. Supporting SVG safely would require either maintaining an element-and-attribute sanitizer in both the
linter and the renderer, or rasterising SVGs in the client before creating a `blob:` URL. Any structured diagram the
agent produces is better represented as a typed widget drawn by client code, so images remain raster-only unless a
concrete need for SVG figures arises.

### Widgets over live data carry references, not bytes

Some widgets display data far too large to store inside a git revision, such as aligned sequencing reads at a locus,
coverage across a region, or a reference genome track. For these widgets, the asset file carries only the references
that identify the slice of data (sample IDs, the genomic locus, and track identifiers), never the underlying data bytes.
The browser fetches byte ranges through an authenticated app route following the same pattern as the paper pane: the
route resolves the object and responds with a redirect to a short-lived signed GCS URL so the bytes stream directly
between the browser and the bucket ([`document-pane.md`](document-pane.md) §"Backend seam"). That route uses its own
resolver, scoped to the Project and pinned to the case-genomes bucket, because a storage resolver never spans multiple
buckets. Because every range request signs a fresh URL, revoking a curator's access takes effect on their next read,
even in a tab left open at a locus.

Keeping raw genomic slices out of git revisions is also an authorisation requirement. Every pushed git revision is kept
indefinitely and remains readable by anyone in the Project, whereas access to patient genomic data must be checked
against the reader at the time of each read. Embedding a slice of patient reads inside a committed widget asset would
freeze that access check at commit time and apply it to the wrong principal.

The genomic data such a widget reads is part of the Analysis's input datasets. Whether those input datasets are pinned
or can change beneath an Analysis is a workspace-level question ([`workspace-model.md`](workspace-model.md) §"Open
questions"), so the widget payload carries no checksum or dataset version of its own.

An aligned-reads view is the prototype for this class of widget: its asset names the genomic span, the sample IDs, and
the tracks, and the browser fetches BAM/CRAM ranges through the authorising route alongside the reference genome and
shared annotation tracks. Building that widget depends on pieces outside this doc's scope: per-sample alignment files in
the case datasets, and a Project-membership check on sample IDs in the browser route. Inside the sandbox, a sample ID
can only reach the sample registry configured for the session, so checking the session is sufficient; a browser route
can be asked for any sample ID and must verify that the requested sample belongs to the curator's Project before signing
a read URL ([`workspace-model.md`](workspace-model.md)).

Ranged reads across a redirecting route rely on standard Fetch behaviour: when following an HTTP redirect on a `GET`,
the browser re-issues the request with the original `Range` header and drops only `Authorization` when crossing origins
([Fetch Standard, "HTTP-redirect fetch"](https://fetch.spec.whatwg.org/#http-redirect-fetch)). The case-genomes bucket's
CORS policy must therefore allow the `Range` request header.

### The SVCv4 classification and the pedigree

Beyond the checklist in the walk-through, this design was shaped by the SVCv4 classification (the most complex
structured record in the curation workflow), the pedigree (the simplest diagram that does not fit in prose), and the
aligned-reads view above (whose data lives outside the git revision).

The SVCv4 classification widget draws one run's point tally: the routing choice, a row per evidence code with its
status, points, evidence, and reasoning, and the curator's tick and note at the item, code, and overall classification
levels. Its payload
([`svcv4_classification.proto`](../../schema/proto/themis/widgets/models/svcv4_classification.proto)) shares the
vocabulary of the curator's worksheet so the run-review loop can compare agent runs against human curations in the same
terms. [`svcv4-classification-widget.md`](svcv4-classification-widget.md) covers its design in full.

A pedigree widget draws the family structure around a proband as a standard clinical pedigree chart. Its payload names
the genomic locus the chart describes using a `GenomicSpan`. Its family representation will build on grus
([github.com/populationgenomics/grus](https://github.com/populationgenomics/grus)), CPG's open-source protobuf pedigree
intermediate representation and renderer.

## Alternatives considered

- **A JSON payload in a fenced code block.** Putting JSON directly inside `working_document.md` would avoid protobuf
  schemas, generated code, and separate asset files. It was rejected because the model can hand-edit JSON in prose and
  because parsing agent-written text blocks in the client to drive rendering reintroduces the failure modes that
  [`agent-output-rendering.md`](agent-output-rendering.md) rules out.
- **A shared `Citation` message type embedded by every payload.** Sharing one `Citation` protobuf message across all
  widget schemas would make the citation reveal uniform at the type level. It was rejected because it locks in one
  citation shape before any payload needs figure or table coordinates, and several widgets (such as aligned reads or a
  pedigree) do not cite literature at all.
- **Rendering widgets to HTML or SVG on the server.** Server-side rendering into markup would spare the web client from
  shipping a React component per widget type. It was rejected because it puts markup back on the path from agent-written
  data to the page, and a static server-rendered image cannot support interactive curator ticks and notes.
- **One bundle file per document holding every widget payload.** Packing all of a document's widgets into a single
  binary file would reduce the number of files in the workspace. It was rejected because each `::embed` directive would
  have to name a key inside that bundle (reintroducing a second identifier that can drift), and corrupting or
  conflicting on that one file would break every widget in the document instead of one.
- **Storing user judgements in a separate protected file.** Putting curator ticks and notes in a separate file on a path
  the agent is forbidden to write would reuse sheaf's existing protected-path check without a custom content check. It
  was rejected because the payload schema would no longer declare which fields are user judgements, each widget type
  would need a separate convention for pairing judgements with items, and the two files could drift out of sync when
  items change. Keeping judgements in a file the agent cannot write also makes it impossible for an agent edit to clear
  a stale tick when the claim it judged changes.
- **A custom git merge driver for widget assets.** Configuring a git merge driver in the sandbox would let `git rebase`
  resolve conflicting widget files automatically by running the ownership carry rule during the rebase. It was rejected
  because the agent already resolves a rebase stop on a widget file with one call to `update`, so a custom merge driver
  would add git plumbing only to save a single tool call when a conflict occurs.
