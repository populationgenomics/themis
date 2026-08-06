# Design: workbench document pane

**Status:** draft **Related:** [`frontend-framework.md`](frontend-framework.md) (web tier + BFF this rides on;
anchored-comment offset convention); [`literature-evidence-layer.md`](literature-evidence-layer.md) (litcache + the
evidence service that gains `Locate`/`Validate`); [`litcache-manifest.md`](litcache-manifest.md)
(`Manifest`/`Rendering`/`AssociatedFile` this resolves against); [`services.md`](services.md) (the
proto+`grpc.aio`+fixture pattern the evidence-service additions follow); [`proto.md`](proto.md) (schema +
serialization).

## Overview

The workbench is a **conversation region** beside a **tab area**. Every document surface is a tab of one **content
kind** — the working document, an opened paper (markdown or PDF), a supplementary file. The **conversation is not a
tab**: it is a dedicated region docked to any of the **main** window's four edges, in a resizable split with the tab
area. The tab area is a **tab pane** that splits **left/right into at most two panes**, each with its own tabs and its
own active tab; a **tab lives in exactly one pane** across all windows. A citation opens its paper **beside its source**
— a document citation splits the pane, a conversation citation opens in the tab area — so the source and the cited paper
are visible at once; a paper already open is **surfaced, not duplicated**. Three-or-more surfaces at once come from
**more windows**: exactly one **main** window holds the single source of truth (and the conversation), and every other
is a **thin mirror** with a tab area only.

One structural model spans every surface — reveal, split, pop-out, and collapse fall out of it rather than being
special-cased per pane. Every layout curators have described reduces to two shapes: **at most two documents side by
side**, and **the conversation spanning an edge**. A two-pane tab area plus a special conversation region delivers both
at the lowest structural cost — an in-window two-up split for the common pairings, more windows for more density.

## Background

Citations exist upstream as markdown directives `:paper[id]` and `:quote[id, text]` (the syntax originated in
litmanager, which is reference-only and not in this loop; the agent authors the directives to that convention). Papers
live in litcache on GCS as XML→markdown `Rendering`s and/or PDF revisions with an optional recoverable text layer;
`Manifest.files` lists `AssociatedFile`s (figures and supplementary, same structure, distinguished by `role`), lazily
fetched. Quote→location resolves two ways: code-point offsets in markdown, bounding boxes in a PDF (via anchorite,
folded into the evidence service). `id` in a directive is the litcache canonical `doc_id` (UUID).

The earlier two-group model targeted every reveal at *the other group* — teleporting the paper into a distant pane that
could displace whatever was parked there. Opening the paper **beside its source** by splitting locally makes placement
predictable and never disturbs a distant pane. Treating the conversation as a tab among documents was also a poor fit:
it is a persistent companion, not a document.

## Non-goals

- **More than two panes in one window / recursive grids.** The tab area splits into at most two panes (left/right).
  Three-or-more simultaneous surfaces come from additional windows, not a wider in-window layout, and not a recursive
  N-way grid. A split half cannot split again — the window is a bounded depth-2 arrangement (conversation region beside
  the tab area; the tab area optionally split), no tree, no grid.
- **The conversation as a tab or tile.** The conversation has no tab strip and no icon; it never sits in a tab pane. It
  is a dedicated region of the main window (as VS Code docks its terminal rather than nesting it among editors). It
  cannot be placed alone on a second monitor; the whole main window goes there instead — an accepted limitation.
- **Drag-to-desktop tear-off to spawn a window.** HTML5 DnD delivers no `drop` outside a document, and `dragend` screen
  coordinates are zeroed in Firefox/Safari; tab tear-off is privileged browser chrome. Window creation is a menu item.
- **Restoring a multi-window arrangement on reload.** A reload or crash **consolidates every tab into a single main
  window**; the **open-paper set is preserved** (papers reconstituted as tabs), but window distribution and geometry are
  not restored (see §Windows).
- **Rich supplementary-file rendering.** Supplementary files (arbitrary MIME) open as a download / open-externally tab
  only. Figures (images) do render inline.
- **Directives outside the conversation and working document.** Paper content is a pure display target; it carries no
  `:paper`/`:quote`, so papers never cite papers here.
- **Cross-device layout sync.** Layout memory is per-browser (`localStorage`), not a server-side per-user setting.
- **Client-side quote matching.** All quote→location resolution is server-side (see §Highlight resolution); the client
  only applies a returned location.

## Design

### Layout: a conversation region beside a two-pane tab area

- The **tab area** is a **tab pane** — a tab strip + content, holding document tabs (working document, papers,
  supplementary). It splits **left/right into two** panes (at most two), one draggable divider; each pane has its own
  tabs and its own active tab. A tab belongs to exactly one pane; moving it removes it from its source. Closing or
  collapsing returns a two-pane area to one. When the tab area is **empty** (its sole pane holds no tabs — e.g. the
  working document popped to a child), the **conversation fills the window**; a reveal or reparent brings the split back
  (the reducer keeps a zero-tab pane as the reveal/reparent target). A pane's **header collapses its actions** — strip
  label mode (icons ↔ titles), split, swap, move, and (in a child window) move-back — into a single **overflow menu**
  (`⋯`), one affordance rather than a row of glyphs.
- The **conversation** is a **special region** (no tab strip, no icon) docked to **any of the four edges** of a
  resizable split with the tab area, chosen from an `AppBar` four-edge selector (the analogue of the earlier
  `[ ]`/`[|]`/`[-]` control), persisted. Present only in the **main** window.
- **Structure.** A window is a bounded **depth-2 panel arrangement**: an outer split `[conversation | tab area]` whose
  direction flips for a left/right vs top/bottom dock edge, and an inner split `[pane | pane]` for the tab area. Nested
  `react-resizable-panels` `PaneGroup`s; the outer ratio is **orientation-specific** (a width % when docked left/right,
  a height % when top/bottom), so a flip does not carry a width ratio into a height. The "New analysis" prompt bar stays
  as workbench chrome above the split.
- **Default:** conversation on the left, the working document the sole tab of a single pane.

### Tabs: kinds, pinning, movement

A tab is one **content kind**:

- `working-doc` — the deliverable. **Pinned** (non-closable), movable. One exists.
- `paper{id, representation}` — closable; `representation` toggles markdown ↔ PDF (below). One tab per `id`.
- `supplementary{id, name}` — closable; download / open-externally fallback (non-goal: rich rendering).

(The conversation is no longer a content kind — it is the region above.) Each pane has its own active tab. Tabs move
three ways:

- **Split** — split the pane left/right and place the acted-on tab in the new pane (at most two; splitting when two
  exist is a no-op).
- **Move** — to the other pane; to a **new** pane (creates the split from a single pane); or to a new/existing **child**
  window, by cross-window drag or the context menu. Move-to-window is offered only when the move **leaves something
  behind** in the source window: the **main** window always keeps its conversation region, so it always qualifies (even
  the sole working document, which pops out to read standalone); a **child** window is a tab area only, so a lone tab
  there does not offer it (moving it would just bounce the sole tab into another empty window). The pane-menu variant
  moves the **whole pane**; the per-tab menu moves a single tab.
- **Swap** — when the tab area is split, swap the two panes left ↔ right. Their contents (tabs + active tab) trade sides
  while each slot keeps its id, so the divider widths stay put; it is involutive.
- **Close** — closable tabs close; closing a pane's last closable tab collapses a two-pane area to one. A **pinned** tab
  (working document) never closes, so a pane holding only a pinned tab does not collapse by closing.

Drag (native HTML5, §Alternatives) is the discoverable in-window gesture; the **context menu** is the non-drag path and
the only one that is keyboard-operable (`Shift+F10` / Menu key, arrow-navigable, focus returned on close), works on
touch, reaches a **popped-out** window, and creates the split from a single pane. It reuses the repo's dependency-free
`role="menu"` pattern. Drops are handled **type-agnostically**: a strip accepts any drag (`preventDefault` at
`dragover`, since payload values are unreadable until `drop`) and **validates the payload's shape at `drop`** — a tab
move, a same-place no-op, or (a capability this affords, specced separately) an **external file** dropped for ingestion.
A cross-window tab move carries its payload via a `dragstart` `BroadcastChannel` handshake keyed to an **opaque
drag-session id** (placed in a standard drag type), so it survives a cross-window OS drag without leaking structured
data into external drop targets; the drop validates a live session matches. **Window creation is menu-only** (a drop
outside any document is undetectable), and cross-window drag can only target a **visible, non-overlapping** window — the
inherent DnD limit, with the menu as the secondary path.

### Reveal: beside the source, surface if already open

`:paper`/`:quote` are parsed by `remark-directive` on the **shared `Markdown` component** (`markdown.tsx`), which both
the conversation and working-document renderers use; a click handler wires each directive to a `reveal`. A reveal opens
the paper **beside its source** so both are visible. The **already-open check runs first**: a paper is never duplicated
(a tab lives in one pane). Let the *computed target* be the pane the placement rules below select.

- **Already open somewhere.** Open in the **same window** → its existing tab **moves to the computed target** (so a
  document-sourced reveal still lands it beside the source). Open in a **different window** → that window is **raised**
  and the tab activated there (surfaced, not yanked); if the browser blocks programmatic `focus()`, the tab moves into
  this window's computed target instead.
- **Document-sourced** (citation in the working document or a paper, in pane *X*): the computed target is the **other
  pane**, splitting the tab area if it is single-pane; if that pane already holds tabs, the paper **appends and
  activates** there. Source stays in *X* — side by side, the verification loop.
- **Conversation-sourced** (the conversation is main-only and is not a pane): the target keys on where the working
  document is —
  1. working doc in main, tab area **unsplit** → the working document's pane (a zero-tab second pane does not count as
     split);
  1. working doc in main, tab area **split** → the pane that is **not** the working document's;
  1. working doc in a **child window** → main's **active pane** (a bare tab).

The source-dependent asymmetry is deliberate: a document-sourced reveal always splits beside (verification is inherently
side-by-side), while conversation-sourced case 1 merely tabs the paper in (the conversation source stays visible in its
dock). A `:paper[id]` whose `id` is not a well-formed, known canonical UUID renders as a **visibly broken, non-navigable
citation showing the raw id** — fail loud, debuggable, never a fuzzy nearest-match guess.

A revealed paper's tab appears **immediately** in a loading state (a spinner and a "Loading…" label), placed before its
metadata resolves, then filled in when the fetch lands; a failed fetch **keeps the tab in an error state** (labelled
"Unavailable", with the reason in the content area) rather than letting it vanish, and records nothing for reopen. So a
click gives instant feedback rather than waiting on the round-trip, and a not-in-corpus paper is a visible failure, not
a silently swallowed one.

Highlight lifecycle is **per-tab**: each paper tab retains at most one active highlight (its most-recent citation);
manually switching tabs shows each tab's retained highlight. The per-tab quote lives in shared state; the **window
rendering the paper** resolves offsets and applies the visual highlight locally (offsets are per-rendering).

### Representation: markdown vs PDF

A paper tab prefers **markdown when an XML-derived rendering exists** (`Rendering.from_source = jats-xml`), because that
text is high-fidelity. When the only rendering is **LLM-OCR of a PDF** (lossy), the tab defaults to **PDF**. The toggle
is always available. The citation (id + quote) is the durable anchor; the highlight is **recomputed per representation**
on toggle (an XML-derived markdown and a PDF text layer differ in whitespace/hyphenation/ligatures, so a location valid
in one may be a `Locate` miss in the other).

### Highlight resolution: server-side, one matcher

All quote matching is server-side, so `Validate` (agent authoring-time) and `Locate` (UI reveal-time) share **one
implementation** and cannot skew. The evidence service exposes `Locate(id, quote, representation)` returning either
**code-point offsets** (markdown; the offset convention `frontend-framework.md` fixes for comments) or
**`{page, rects[]}`** (PDF). The client only **applies** the result:

- offsets → a DOM `Range` → the **CSS Custom Highlight API** (`CSS.highlights`); no DOM mutation, so clear = drop the
  range, add = register the next. `scrollIntoView` on the range.
- `{page, rects[]}` → absolutely-positioned overlay divs over the `pdfjs` page (`react-pdf`), mapping the coordinate
  space through the page viewport transform. Clear = remove overlays.

A quote **unlocatable in the shown representation** clears the highlight and shows a **persistent warning chip** with
the quote text (docked at the top of the tab's content area), occupying the tab's single highlight slot until the next
successful reveal. No auto-toggle to the other representation.

The shipped markdown arm diverges from "the client only applies the result": `Locate(…, MARKDOWN)` is consumed as a
**boolean** (matched / not), and the client re-derives the DOM position itself with `indexOf(quote)` over the rendered
text, then walks text runs to a `Range`. This holds while the matcher's normalization and the DOM's text agree
character-for-character; a quote the **normalization-aware** matcher locates but whose exact substring is not present in
the rendered DOM (collapsed whitespace, an entity, a soft hyphen) is a server hit but a client `indexOf` miss, and
surfaces as the warning chip. Wiring the client to apply the server's returned offsets instead of re-finding the quote
closes that gap (see Open questions).

### Windows: one source of truth, N thin mirrors

Exactly one window is **main** and holds the single source of truth (and the conversation); every other is a **thin
mirror** with a tab area only. A **group** never pops; a **window** does, holding one or two panes of tabs.

- A `BroadcastChannel` carries a **whole-workspace snapshot** to every window and **commands back** to main; each window
  renders only its own tab area; main applies and re-broadcasts — no split-brain. The snapshot is **structure plus small
  signals** (window/pane/tab descriptors, ids, ratios, active ids, the highlight quote map, the strip label mode) and,
  for the working document, its **version + analysisId** — the refetch signal, not the body. The two large bodies stay
  **off** the channel: the conversation transcript is main-only (children never render it), and the working-document
  **body is fetched by each window from the BFF**, keyed on the broadcast version, so a child re-fetches when the agent
  republishes. (Unlike an immutable paper, the working document is versioned; the version is what a mirror needs, the
  body is what it must not carry.)
- **Process model: one process, one main thread.** Children open via `window.open` with the opener retained, so
  (same-origin) they share main's renderer **process and event loop** — not N parallel processes. Main holds the child
  handles for a direct `child.close()`, and a crash takes all windows together (no orphaned mirror outlives its source
  of truth). The trade is **no compute isolation** — a heavy synchronous render janks all windows; pdfjs's worker
  offloads PDF *parsing* but **canvas rasterization stays on the main thread**, mitigated only by lazy/virtualized
  rendering. Mirror actions round-trip through main and share that thread, so they can lag under heavy render (not
  split-brain).
- **Continuous gestures are local, committed on release.** A divider drag updates only the acting window and writes to
  shared state on pointer-up — one broadcast, not one per frame.
- **Lifecycle.** The **conversation is anchored to main** — it relocates only within main (any of the four edges), never
  to a child. The **working document may move to a child window**; if that child closes it **reparents into main's
  active pane** (appended as a tab); if main closes it reconstitutes on relaunch (a singleton, server-backed), so no
  arrangement can lose it. Closing a child reparents its pinned tabs into main's active pane and closes its closable
  tabs; a child whose tab area becomes empty closes itself; **main** may show an empty tab area (the conversation only),
  which stays a single **zero-tab pane** so an active pane always exists as a reveal/reparent target. Closing a child
  via OS chrome may not fire `beforeunload`/`pagehide` reliably; the backstop is relaunch reconstitution of pinned
  content, so a pinned tab can transiently vanish but is never lost. Bookkeeping is **O(N)**: per-child window handles
  (runtime refs, outside the snapshot/persistence), per-child close routing, disambiguating which child sent a close,
  and re-registering a child the user manually reloads.
- **Persistence and restore.** Only the **main-window layout** persists (`localStorage`) — per-pane tab order, active
  tab, active pane, split state, conversation edge + orientation-specific outer ratio, strip label mode — plus the
  **open-paper id set** and the **reopen-last-closed** stack. Window distribution and geometry are **not** persisted. On
  reload/crash everything **consolidates into a single main window**: main's own panes restore from the persisted
  layout, and papers that were open in child windows **append into main's active pane** (re-fetched by id) after the
  restored tabs. `highlights` are transient (not persisted). `window.open` needs a user gesture (no silent respawn), and
  restoring saved positions misfires exactly when the environment changed — a monitor unplugged, a laptop undocked — so
  consolidating to one window is predictable and the open-paper set is preserved.

### Accessibility

- The **context menu** is the keyboard/touch path for split, move-to-pane, and move-to-window; drag is a mouse
  enhancement with no keyboard requirement.
- Dividers are `role="separator"` with an `aria-label` (e.g. "Resize conversation and documents"), resizable by arrow
  keys. The four-edge conversation selector uses text labels.
- Since windows have no user-assigned titles, **move-to-window names each destination by its active (or pinned) tab**.
  After a tab leaves a window (move-to-window) or a pane collapses, focus lands on the source pane's new active tab.

### Backend seam: service resolves, BFF streams

Litcache resolution logic (`doc_id → chosen rendering hash → object path`,
`figure name → AssociatedFile → content- addressed path`, lazy-fetch of un-fetched files) stays in **one language** —
the Python evidence service — and is not reimplemented in the TypeScript BFF. The evidence service, per `services.md`
(hand-authored proto, `grpc.aio` servicer, port ABC + fail-loud fixture backend), gains:

- `Locate(id, quote, representation)` — the shared matcher above (absorbs anchorite for the PDF path).
- `Validate(id, quote)` — an **agent tool** (sandbox-side, authoring-time): "does `Locate` succeed in any
  representation?", preventing broken citations at the source.
- Content resolution — given `(id, what)`, return the **GCS object path** (fetching first when a file's `path` is
  absent).

The BFF reaches the service behind a TypeScript **evidence port** (`server/adapters`, the existing `THEMIS_BACKEND`
fixture/live split): a fixture adapter serves a seeded corpus offline (all pane dev runs on it), and a live adapter
calls the gRPC service. The BFF holds no litcache logic; the port answers "describe this paper" and returns **content
bytes** for a selected object — the fixture returns bundled bytes, the live adapter resolves the object via the service
then **streams it from GCS** (`gcs.ts`), so the gcs read is the live adapter's internal detail, not the route's. Routes:
`GET /api/papers/[id]` (PaperInfo), `GET /api/papers/[id]/markdown` (chosen rendering, figure refs rewritten to the
files route), `GET /api/papers/[id]/pdf`, `GET /api/papers/[id]/files/[name]` (figures inline; supplementary as
download).

**Authorization is IAP-only.** A paper belongs to no Project — litcache is a shared corpus and entitlement is a deferred
non-goal (`literature-evidence-layer.md`) — so, unlike the Project-scoped analysis routes, the paper routes require only
a verified IAP identity (`proxy.ts` + `context.ts`, app-wide), not Project membership: there is no Project to scope
against. A real entitlement model lands at the evidence service later, not bolted onto these routes.

The live adapter is the BFF's **first gRPC client** — today the BFF speaks only Anthropic HTTP (`client.ts`), Cloud SQL,
and GCS, with no TS→Python gRPC transport. It reuses the `protobuf-es` messages `regen` already emits
(`apps/web/src/gen/themis/rpc/evidence_pb.ts`) over a Connect gRPC transport (`@connectrpc/connect-node`
`createGrpcTransport`), authenticated by an ID-token interceptor (audience = the service URL), mirroring Python's
`id_token.channel_credentials`. The fixture adapter carries no transport, so the pane is built and tested without it;
the live adapter (and its dependency) lands with the deploy.

## Alternatives considered

- **N-up resizable pane row / arbitrary in-window tiling** — rejected. Three-plus simultaneous surfaces come from more
  windows; an N-row multiplies divider and reorder logic for a density a reading surface rarely wants.
- **Single tab pane per window, no in-window split** (OS-window tiling only) — rejected. It hands the common
  document-beside-paper pairing to the OS window manager, which on a single screen tiles worse than an in-app divider.
- **Conversation as a tab / co-equal tile** — rejected. It is a persistent companion, not a document; a tab strip and
  tiling among papers misrepresents it and reintroduces the reveal-into-the-other-group feel.
- **Recursive split tree** (per-pane orientation, arbitrary nesting) — rejected. Buys only mixed-orientation nesting no
  described layout needs, at real persisted-structure and get-lost cost.
- **Reveal in place, tab-switch to compare** — rejected. Verifying a claim against its cited quote is inherently
  side-by-side; tab-switching shows them only in sequence. Document-sourced reveal auto-splits beside instead.
- **Reveal into "the other group"** (the two-group model) — superseded. Automatic placement into a *distant* pane
  displaces parked content; a *local* split beside the source does not. This deletes the fixed group count, "the other
  group", per-group reveal targeting, and the conversation-as-a-tab treatment.
- **Type-gated drop discrimination** (accept only a recognized custom MIME type at `dragover`) — rejected. Custom types
  can be stripped on a cross-window OS drag, breaking drop-target recognition, not just the payload. Accept-any-drag +
  validate-shape-at-`drop` is robust to that and affords external-file ingestion.
- **Pop-out with ownership transferred to a child** (survives main close) — rejected: split-brain and a fragile
  hand-off; the thin mirror is simpler and main-survival is not required.
- **Separate renderer process per window** (`noopener` / COOP) — deferred. Buys compute and crash isolation but loses
  the opener handles (`focus`/`close`) and needs a heartbeat + raise-window story, for isolation the workload does not
  yet need.
- **Drag-to-desktop tear-off** — rejected: no drop fires outside a document; `dragend` screen coords are zeroed in
  Firefox/Safari; tear-off is privileged chrome. Window creation is a menu item.
- **Restore saved window positions on reload** — rejected. Needs a user gesture (no silent respawn) and misfires on
  monitor/config changes (off-screen windows, popup pile-up); consolidating to one window is predictable and the
  open-paper set is preserved.
- **Client-side naive substring matching for markdown** (instant, no round-trip) — rejected. A `:quote` carries the
  quote text but not the representation it was drawn from; against a different rendering, exact substring **fails on a
  valid citation**. Server-side matching costs a round-trip per markdown highlight but is robust and shares one matcher
  with `Validate`.
- **`dnd-kit` (pointer-based) for tab drag** — rejected; **native HTML5 DnD** chosen. A pointer-based drag cannot cross
  a `window.open` boundary: while a button is held the browser delivers the pointer stream only to the origin window
  (implicit capture is per-document), so the other window is blind until release. Native HTML5 DnD rides the OS drag
  session, which delivers `dragover`/`drop` to the other same-origin window. The cost — weaker ergonomics, no
  touch/keyboard — is borne by the "Move" context menu, which is the accessible/touch/cross-window path anyway.
- **BFF reads litcache from GCS directly** — rejected. Reimplements manifest/lazy-fetch logic already in Python;
  service-resolves / BFF-streams keeps that logic single-language.
- **BFF subprocess-spawns Python anchorite** — rejected. Needs Python in the Bun image, per-request process spawn, no
  proto contract, no fixture seam.
- **PDF quote→bbox precomputed and stored** — rejected; the directive carries only text, so location resolves live.
- **`grpc-js` + `ts-proto` for the live BFF adapter** (instead of `connect-node`) — rejected. ts-proto's draw is DX, not
  a goal here; it adds a second, redundant codegen emitting an incompatible copy of messages `regen` already produces as
  `protobuf-es`. connect-node reuses those exact messages (one toolchain), and the four unary RPCs need none of
  grpc-js's lower-level streaming/deadline control.

## Implementation state

This section describes the endpoint the three stacked frontend changes reach together (the workspace logic core, the BFF
paper surface, the windowed UI). At an intermediate commit only the layers below the reader's have landed — the
reducer/drag/highlight core carries this doc, and the routes and mirror controller it names land above it.

**Shipped** on the fixture evidence adapter (the BFF's own seeded corpus — no Python service, no gRPC transport, so the
whole frontend builds and tests offline): the evidence-service proto + a fail-loud fixture backend; the BFF evidence
port + IAP-only paper routes; the conversation region + two-pane tab area with the four-edge dock selector; reveal
beside the source with surface-if-already-open and the loading placeholder; split / move / close / swap and within- and
cross-window tab drag; the N-window main-authoritative mirror with move-to-window and per-window working-document fetch;
and consolidate-on-reload persistence (main-window layout + open-paper set + reopen stack).

**Deployed to dev** behind the unchanged proto contract: the live evidence adapter (the BFF's first gRPC client — a
Connect gRPC transport over the `protobuf-es` messages, ID-token interceptor) and the evidence Cloud Run service reading
real litcache — content resolution and the normalization-aware markdown matcher. Markdown papers reveal and highlight
end to end there.

**Remaining:**

- **PDF quote highlighting** — the anchorite PDF matcher behind `Locate(…, PDF)` is not wired; until it is, that arm
  fails loud (gRPC `UNIMPLEMENTED`) and the BFF surfaces it as the pane's not-located warning rather than erroring the
  reveal. Blocked on the anchorite coordinate contract (see Open questions).
- **Lazy fetch of an associated file** — content resolution raises `MissingContentError` for a file the manifest lists
  without a `path` (the seed corpus has every file fetched, so nothing exercises it yet); fetch-on-demand + write-back
  is the remaining piece.
- **Highlight-API fallback** — a browser without the CSS Custom Highlight API renders a *located* quote as the
  "unlocatable" warning chip; a standalone fix, independent of the workspace model.

## Open questions

- **Anchorite coordinate contract** — units, origin corner, and page indexing of the PDF `rects[]` are a cross-repo
  seam; the `Locate` response is the boundary and needs pinning with anchorite's owner before the PDF highlight path is
  real.
- **Quote source representation** — a `:quote` does not record which rendering its text came from, so a valid quote can
  be a `Locate` miss in the shown representation (mitigated by the toggle + warning chip). Recording the source
  representation in the directive would remove the miss but is an upstream authoring change, out of scope here.
- **Markdown offsets: apply, don't re-find** — the client re-derives the markdown highlight position with `indexOf`
  rather than applying the offsets `Locate` already returns (see Highlight resolution), so a normalization-only server
  match can still miss client-side. Applying the returned offsets removes the second matcher; deferred, no consumer of
  the offsets exists yet.
