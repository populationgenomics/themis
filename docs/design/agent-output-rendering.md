# Design: rendering agent output

**Related:** [`conversation-view.md`](conversation-view.md) (the stream these rows render into, the poll that computes
them, and the offline fixture run that drives this derivation); [`document-pane.md`](document-pane.md) (the region the
stream sits in); [`proto.md`](proto.md) (the wire and its compat gate).

## Overview

An agent run produces text and tool calls, and a curator has to be able to read them. This doc decides how anything the
agent produced is drawn.

- **The client renders bodies, not tools.** A call's label, its body and that body's language are derived server-side
  from the tool's input, so the client holds no per-tool knowledge and a tool added upstream reaches the screen without
  a client change.
- **A replacement is diffed server-side** and crosses as kinded lines, so every client draws the same alignment and a
  copied block is still a patch.
- **Agent-authored text never becomes markup.** It reaches the DOM as elements, never as HTML, and the projection — not
  the agent, not the client — decides how a call is displayed.
- **A value a build predates renders as unknown**, not as an error: a stream is worth more partly drawn than not at all.

## Background

**What there is to render.** A run's event log carries the agent's prose and its tool calls
([`conversation-view.md`](conversation-view.md) §Background). A tool call arrives as a **tool name** and an **input** —
an untyped dictionary, whose keys are the toolset's own and which nothing on our side declares — followed later by a
**result**, the text the tool printed and whether it failed. That is all there is: no title, no rendering hint, nothing
that says how the call should look.

**Who decides.** The browser polls the BFF (the web tier's backend-for-frontend) every few seconds; the BFF reads the
log and returns the whole conversation as a **projection**, a display model it computes fresh each tick
([`conversation-view.md`](conversation-view.md) §"The four methods"). Every decision this doc makes is made in that
projection. The client draws what it is given and nothing more — that division is the point of the first take-away, and
most of what follows is a consequence of it.

**The wire.** A projected tool call carries `intent`, the one-line label; either `command`, the body text, or `diff`, a
replacement line by line; and `language`, the syntax of the body text. Those names are used here only where the field
identity matters; elsewhere this doc says label and body.

## Design

### The agent never authors its own presentation markup

Everything this doc draws was written by a model, and much of it quotes third-party text the model read — untrusted
content by [`../PRODUCT.md`](../PRODUCT.md) §9. If agent-authored text could carry markup, a run that read a hostile
page could put arbitrary markup, styling or links into a curator's browser, and a curator could not tell the agent's
report from the page's. Two rules follow, and they are why the mechanisms below are shaped as they are.

**Agent text reaches the DOM as elements, never as HTML.** Prose renders as markdown whoever wrote it — the agent's
narration, a curator's turn, a sub-agent card's summary — through the one markdown surface, with the inline grammar and
nothing that could introduce raw markup. Tool inputs, results and diffs are source, not prose, and stay monospace. The
curator's bubble does honour a single newline as a line break, since Shift+Enter makes a multi-line turn reachable and
standard markdown would fold what it produced back into one paragraph.

**The projection decides how a call is displayed — not the agent, and not the client.** The agent supplies a tool name
and an input; the projection turns that into a label, a body and a language; the client renders those. So no
agent-supplied string chooses a rendering.

Highlighting follows from the first rule. A body's text is lexed to a syntax tree and that tree is turned into React
elements, so there is no HTML string anywhere on the path to inject through. The highlighter brings no palette of its
own either: every scope it emits maps onto one of the app's own colour tokens, and an unmapped scope inherits the
block's ink rather than introducing a hue the palette does not hold. A grammar is registered per language the enum
names, and a language the enum names with no grammar behind it does not build; a language a build predates renders unlit
(§"An enum value a build predates renders as unknown").

Results and diffs are never highlighted. A result is whatever the tool printed, not a program in a language, and lexing
it as one would invent structure that is not there. A diff interleaves two texts, so no block handed to a lexer is one
program — and a second ink axis would fight the added/removed one that carries the meaning.

### A tool call is a label and a body

All per-tool knowledge is server-side. The projection derives a row's one-line label, its body, and that body's language
from the tool's input; the client holds no tool names, no input field names, and no per-tool branches. That is what
"renders bodies, not tools" buys: a tool added upstream reaches the screen without a client change.

The label is a model-stated intent where the tool supplies one, else a well-known target field of the call, else the
tool's own name — never empty, and always a plain field read rather than command parsing. Which fields those are belongs
to the message's own comment. They are the toolset's keys, and a tool's input is an untyped dictionary as far as the SDK
is concerned, so nothing declares them and nothing but a delivered event can confirm them: a key that stops matching
degrades the row — a plainer label, a missing body, or a dump of the whole input — rather than failing the tick.

**The body is the call's own text**, untruncated: the shell command, the content a write wrote, the two sides of an
edit. Two things follow. A tool that names its target in the label does not repeat it on expand — a write already shows
its path, so showing the path again hides the only thing left to show. And a tool with no body at all shows its **whole
input**, because a read's line range and a search's root live there and nowhere else.

Nothing is clipped: an expanded body and its result scroll within a maximum height, and there is no "show more". A dead
truncation hides the one thing expanding a row exists to reveal, and a second expand state is a second thing to get
wrong.

### The language belongs to the text, not the tool

A body's language names the syntax of *that text*, so it is read off the input carrying the body — the text itself, or
the name of the file it was written to — and never off the tool's name. Three consequences:

- A shell command that splices a **heredoc** is not shell: what follows the opener is a payload in whatever language the
  command feeds it to, so the text is no one lexable unit and goes unspecified, rendering unlit. Detecting the opener is
  a heuristic that errs toward unlit, not a shell lexer — that would be a parser in the projection for a cosmetic gain.
- A write states its language only through the name of the file it wrote, so an **extension map** supplies it; an
  extension outside the map is unspecified.
- The whole-input fallback is a **JSON** dump, and says so.

### A replacement crosses as kinded lines

An edit's two sides are diffed **server-side** — one alignment for every client, whatever bundle it is on — and cross as
a list of lines, each carrying its kind and no sign prefix, so the client draws the sign and a copied block is still a
patch. The diff is of the replacement the model stated, not of the file: the BFF never reads the file. Past a per-side
line cap the alignment, whose cost grows with the sides and with the distance between them, is skipped in favour of a
whole-for-whole replacement, which loses no content.

Three costs come with computing the lines server-side. First, a widened field. `command` is the field the body text
crosses in, and it kept its name — the wire is compat-gated, so the name cannot change — but not its meaning: it once
carried the tool's invocation and now carries the body, a write's content included. It is empty both when `diff` carries
the body instead and when the body is itself empty, as a write that empties a file has. A tab still on the old bundle
through a deploy therefore reads `command` by its old meaning, and until it reloads a replacement reaches it as an empty
box. Second, the proto no longer says structurally that a call has exactly one body; one place in the projection is what
enforces it. Third, the diff is frozen at *line* granularity on that same compat-gated wire, so marking the changed span
within a line later is a schema change, not a client change.

### An enum value a build predates renders as unknown

Through a deploy a tab keeps polling on the bundle it loaded, so it can be handed a sub-agent thread's status, a body
language, or a diff-line kind its build predates. Each renders as its neutral unknown — an unknown-status pill, an unlit
body, an unsigned line — rather than throwing, because a stream is worth more partly drawn than not at all. The zero
value is that same case and not a projection bug: proto3-JSON is name-keyed, so an unrecognised enum *name* decodes to
zero client-side. That the projection itself never emits zero is asserted server-side.

## Alternatives considered

- **A oneof over a tool call's two body shapes.** Exactly-one-body would then be structural, but it retires a field
  already on the wire and reshapes the message for the sake of one tool; the right escalation only once a *third*
  structured body appears.
- **Shipping both sides of a replacement and diffing in the client.** Two neutral fields carry no more per-tool
  knowledge than one, but they add a *second* aligner: the same replacement would render one way in a tab still on last
  week's bundle and another beside it on today's, and a screenshot of a row would not be reproducible from the row.

## Open questions

- **What to draw for a result that is not text.** The projection keeps only the text blocks of a result, so a result
  carrying anything else — an image, a structured block — reaches the stream as empty output. The input side always
  shows something: an input it does not recognise degrades to a dump of the whole thing (§"A tool call is a label and a
  body"). The result side has no such fallback, and what it should show in place of an empty box is undecided.
