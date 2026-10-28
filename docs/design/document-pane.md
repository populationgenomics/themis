# Design: workbench document pane

**Related:** [`workbench-navigation.md`](workbench-navigation.md) (Analysis page chrome and arrangement persistence);
[`conversation-view.md`](conversation-view.md) (what the conversation region renders);
[`workbench-workspace.md`](workbench-workspace.md) (the browser's copy of the workspace repository from which working
document revisions are read); [`document-widgets.md`](document-widgets.md) (typed blocks embedded in the working
document); [`literature-evidence-layer.md`](literature-evidence-layer.md) (the evidence service that resolves paper
content and locates quotes); [`litcache-manifest.md`](litcache-manifest.md) (`Manifest`, `Rendering`, and
`AssociatedFile` records in litcache); [`frontend-framework.md`](frontend-framework.md) (Next.js BFF and code-point
offset convention).

## Overview

During an Analysis, a curator reads the working document, steers the agent in the conversation stream, and verifies
claims against cited papers and their figures. A workspace layout for this loop has to keep a claim and its cited source
visible side by side without losing the conversation stream or forcing the curator to manage a complex tiling grid.

The workbench (`apps/web/src/components/workbench/`) splits the main window into two regions: a dedicated conversation
region docked to any of the window's four edges, and a tab area beside it. Every document surface — the working
document, an opened paper, or a supplementary file — is a tab in the tab area. The conversation is not a tab. The tab
area holds one tab pane by default and splits left/right into at most two panes, each with its own tab strip and active
tab. Each tab lives in at most one pane across all open windows.

When a curator clicks a citation, the workbench opens the cited paper beside the surface that cited it. Clicking a
citation inside a document splits the tab area so the source document and the cited paper sit side by side. Clicking a
citation inside the conversation opens the paper in the tab area while the conversation stays visible in its dock. If
the paper is already open in any pane or window, the workbench surfaces that existing tab rather than opening a second
copy. When a curator wants three or more document surfaces visible at once, they pop tabs out into additional browser
windows. Exactly one main window holds the conversation and owns the workspace state; every child window is a thin
mirror that renders only a tab area.

Every layout curators have asked for reduces to at most two documents side by side with the conversation spanning an
edge. A two-pane tab area paired with a docked conversation region covers those pairings with a bounded depth-2 layout,
while child windows handle multi-monitor density without a recursive split tree inside a single window.

## Background

The agent authors citations in both the conversation and the working document using two markdown directives:
`:paper[id]` and `:quote[id, text]`. That directive syntax originated in litmanager, which is a reference system outside
this runtime loop. The `id` in both directives is the canonical litcache document UUID (`doc_id`).

In litcache on Google Cloud Storage (GCS), each paper is stored as a `Manifest`
([`litcache-manifest.md`](litcache-manifest.md)) with one or more markdown `Rendering`s derived from publisher XML or
PDF OCR, PDF revisions with an optional text layer, and `Manifest.files` entries (`AssociatedFile` records for figures
and supplementary attachments, distinguished by `role`). Resolving a `:quote[id, text]` directive to a visual highlight
depends on which representation the curator is viewing: in markdown, a quote resolves to code-point offsets, whereas in
a PDF it resolves to page bounding boxes via anchorite inside the Python evidence service
([`literature-evidence-layer.md`](literature-evidence-layer.md)).

An earlier workbench layout organized the screen into two identical tab groups, placed the conversation inside one of
those groups as an ordinary tab, and targeted every citation reveal at "the other group". That model broke down in two
ways. First, revealing a citation from one group teleported the paper into the distant group and displaced whatever
document the curator had parked there. Second, treating the conversation as a document tab meant that opening papers in
the same group hid the conversation behind a tab strip, even though curators use the conversation continuously alongside
documents.

## Non-goals

- **More than two panes in one window or recursive grids.** The tab area splits left/right into at most two panes. A
  split half cannot split again; the window is a bounded depth-2 arrangement (conversation region beside the tab area,
  with the tab area optionally split in two). Showing three or more document surfaces at once uses additional browser
  windows.
- **Placing the conversation in a tab or standalone child window.** The conversation has no tab strip and no tab icon,
  and it never sits in a tab pane. It lives only in the main window's docked region. A curator who wants the
  conversation on a second monitor moves the main window there and pops documents into child windows.
- **Tearing a tab off to the desktop by dragging.** HTML5 drag-and-drop fires no `drop` event outside a browser
  document, and Firefox and Safari zero out `dragend` screen coordinates. Creating a new window is a menu action.
- **Restoring open paper tabs or multi-window layouts on reload.** Reloading an Analysis opens a single main window
  containing only the working document in the curator's saved arrangement
  ([`workbench-navigation.md`](workbench-navigation.md)). Open papers and child windows are not restored.
- **Rendering arbitrary supplementary files inline.** Supplementary files (spreadsheets, archives, arbitrary MIME types)
  open as a download / open-externally fallback tab. Figures (images) render inline inside markdown.
- **Parsing citation directives inside paper content.** Paper content is a read-only display target and carries no
  `:paper` or `:quote` directives.
- **Syncing layout preferences across devices.** Layout arrangement is stored per browser in IndexedDB, not on the
  server.
- **Client-side quote matching.** Quote-to-location resolution belongs on the evidence service so authoring validation
  and UI reveal share one matcher; the browser only applies a returned location (with one transitional exception in the
  markdown arm, covered in [Highlight resolution](#highlight-resolution-server-side-one-matcher)).

## Design

### Layout: a conversation region beside a two-pane tab area

An obvious way to let curators arrange a conversation, a working document, and several papers is a general tiling tree
where any pane can hold any surface and split horizontally or vertically. In practice, arbitrary nesting makes it easy
to lose track of where a revealed paper will land and forces the conversation to compete with documents for tab-strip
space.

Instead, the main window (`apps/web/src/components/workbench/`) is a bounded depth-2 arrangement built from two nested
`react-resizable-panels` `PaneGroup`s:

```
Main window (conversation docked left, tab area split into two panes)
+------------------+-----------------------------+-----------------------------+
| Conversation     | [Working doc] [Paper A]  ⋯  | [Paper B]                ⋯  |
| (main-only dock: |-----------------------------|-----------------------------|
|  left / right /  |                             |                             |
|  top / bottom)   |  Working document           |  Paper B (Markdown | PDF)   |
|                  |                             |                             |
|                  |  ...claim citing :quote[B]  |  ...highlighted passage...  |
|                  |                             |                             |
| +--------------+ |                             |                             |
| | Steer...     | |                             |                             |
| +--------------+ |                             |                             |
+------------------+-----------------------------+-----------------------------+

Child window (thin mirror: tab area only, 1 or 2 panes)
+------------------------------------------------+-----------------------------+
| [Paper C]                                   ⋯  | [Paper D]                ⋯  |
|------------------------------------------------|-----------------------------|
|  Paper C                                       |  Paper D                    |
+------------------------------------------------+-----------------------------+
```

The outer split places the **conversation region** (`conversation-dock.tsx`) beside the **tab area** (`tab-area.tsx`). A
four-edge selector in the Analysis page header docks the conversation to the left, right, top, or bottom edge of the
main window (defaulting to the left edge, with the working document as the sole tab of an unsplit tab area). The steer
composer lives inside the conversation region below the message stream ([`conversation-view.md`](conversation-view.md)),
so it travels with the conversation across dock edges. Because flipping the dock from left/right to top/bottom turns a
width split into a height split, the workbench stores separate outer split ratios for horizontal and vertical
orientations. Nothing else shares the window; creating an Analysis belongs to the Project page
([`workbench-navigation.md`](workbench-navigation.md)).

The inner split divides the tab area into one or two **tab panes** (`pane.tsx`) separated by a single left/right
draggable divider. Each pane has its own tab strip and its own active tab. A tab belongs to at most one pane across all
windows, so moving a tab removes it from its previous pane. When the last tab in one of two panes closes or moves away,
the two-pane split collapses back to a single pane. If the sole pane in the main window has zero tabs — for example,
after the curator pops the working document out into a child window — the conversation expands to fill the main window,
while the reducer retains the empty pane as the target for future reveals or reparented tabs.

Each pane's header collapses all strip and pane actions into a single overflow menu (`⋯`): toggling strip labels between
compact icons and full titles, splitting, swapping panes, moving the pane to a window, reopening the last closed tab (in
the main window), and moving the pane back to the main window (in a child window).

### Tabs: kinds, pinning, and movement

Every tab in a pane (`workspace-model.ts`) has one of three content kinds:

- `working-doc` — the Analysis deliverable. Exactly one exists, and it is **pinned** (non-closable) but movable between
  panes and windows.
- `paper{id, representation}` — a cited paper, closable, with `representation` toggling between markdown and PDF. At
  most one tab per paper `id` exists across all windows.
- `supplementary{id, name}` — a supplementary file on paper `id`, closable, rendering a download / open-externally card.

Curators rearrange tabs using four operations:

- **Split** splits a single-pane tab area left/right and moves the target tab into the new right-hand pane. Splitting
  when two panes already exist is a no-op.
- **Move** relocates a single tab (from the per-tab context menu or by drag) or every tab in a pane (from the pane
  header's `⋯` menu) to the other pane in the same window, to a new split pane, or to a new or existing child window.
  Moving to another window is offered only when the move leaves something behind in the source window. The main window
  always retains its conversation region, so any tab in the main window — including a lone working document — can pop
  out into a child window. A child window has only a tab area, so a child window with a single remaining tab does not
  offer "Move to new window", which would otherwise close the source child window just to open an identical one.
- **Swap** trades the tab lists and active tabs of the left and right panes while keeping each pane slot's identity and
  divider width in place. Swapping twice returns to the starting state.
- **Close** removes a closable tab. Closing the last tab in one half of a split collapses the tab area to a single pane.
  Because `working-doc` is pinned, a pane holding it never collapses via close.

For mouse users, dragging a tab across strips or across windows is the direct gesture; for keyboard and touch users, the
per-tab context menu (`Shift+F10` or the Menu key, arrow-navigable, restoring focus on close) is the primary path and
the only way to reach a hidden window or spawn a new one.

Cross-window drag cannot use a pointer-event library such as `dnd-kit` because the browser captures pointer events to
the origin document while the mouse button is held, leaving other `window.open` windows blind until release. The tab
strip therefore uses native HTML5 drag-and-drop (`tab-dnd.ts`), which rides the OS drag session across same-origin
windows. Because OS drag sessions can strip custom MIME types across windows and do not expose payload contents during
`dragover`, every tab strip accepts any drag at `dragover` (`event.preventDefault()`) and validates the payload shape at
`drop`. On `dragstart`, the source window announces the dragged tab on the workspace `BroadcastChannel` keyed by an
opaque drag-session ID placed in a standard drag data type; on `drop`, the target strip checks that the session ID
matches a live session before moving the tab. Accepting any drag at `dragover` and discriminating at `drop` also keeps
the strip open to external file drops for ingestion. Dragging still cannot create a new window by dropping on the
desktop and requires both windows to be visible on screen, which is why the context menu provides the complete fallback.

### Reveal: open beside the source, surface if already open

When a curator clicks a `:paper[id]` or `:quote[id, text]` chip — whether rendered by the shared `Markdown` component
(`markdown.tsx`) in the conversation or working document, or raised from inside a document widget
([`document-widgets.md`](document-widgets.md)) — the workbench reveals paper `id` (`reveal.ts`).

The naive rule is to open the paper in whichever pane is currently active, or always in "the other pane". Opening in the
active pane covers up the document the curator was just reading; always targeting a fixed second group fails when the
citation came from the conversation or when the working document has been moved to a child window. Instead, reveal
computes a target pane that keeps the citation's source visible beside the newly opened paper:

```
Citation clicked
  │
  ├─ From a document tab in pane X ──► Target is the pane beside X in the same window
  │                                    (splitting the tab area first if unsplit)
  │
  └─ From the conversation (main) ───► Where is the working document?
                                         ├─ In main, tab area unsplit ─► Working doc's pane
                                         ├─ In main, tab area split ───► The pane NOT holding working doc
                                         └─ In a child window ─────────► Main's active pane
```

Before opening a new tab, the workbench checks whether paper `id` is already open anywhere in the workspace:

- **Already open in the same window:** the existing tab moves to the computed target pane and activates there, so a
  citation clicked in a document still places the paper beside the citing document.
- **Already open in another window:** the workbench calls `focus()` on that window and activates the tab there without
  pulling it out of the window where the curator parked it. If the browser blocks programmatic `focus()`, the tab moves
  into the current window's computed target pane instead.
- **Not yet open:** a new paper tab opens and activates in the computed target pane.

The asymmetry between document-sourced and conversation-sourced reveals is deliberate. Verifying a claim in the working
document against a paper requires reading two documents side by side, so a document citation always splits the tab area.
When a curator clicks a citation in the conversation while the tab area is unsplit, the conversation remains visible in
its dock, so tabbing the paper into the single pane keeps both the conversation citation and the paper visible without
forcing a three-column layout (`[conversation | working doc | paper]`). Once the curator splits the tab area, subsequent
conversation citations preserve the working document in its pane and route papers to the other pane.

If a `:paper[id]` directive carries an `id` that is not a well-formed canonical UUID, `Markdown` renders a visibly
broken, non-navigable citation showing the raw ID string rather than guessing a match. When a valid citation is clicked,
the paper tab appears in the target pane immediately in a loading state (a spinner and a `"Loading…"` title) before
`DescribePaper` returns. If the metadata fetch fails — for example, when the paper UUID is not in litcache — the tab
stays open in an error state (`"Unavailable"`, displaying the error reason in the tab body) and is omitted from the
reopen-last-closed stack, so a missing paper fails visibly rather than silently disappearing.

Each paper tab retains at most one active quote highlight at a time, corresponding to the most recent `:quote` reveal
for that paper. Switching away from a paper tab and back preserves its highlight. The active quote string for each paper
lives in the shared workspace state, while the window actually rendering that paper tab resolves the quote to offsets or
bounding boxes locally.

### Representation: markdown vs PDF

When a paper tab first opens, it defaults to **markdown** if the paper has an XML-derived markdown rendering in litcache
(whose source is the publisher `xml` lineage), because XML-derived markdown preserves clean text and inline figures. If
the only available markdown rendering comes from LLM OCR of a PDF, the tab defaults to **PDF** instead, because OCR
markdown is lossy. The curator can toggle between markdown and PDF at any time in the tab header.

A citation carries `(id, quote)` rather than precomputed coordinates. When the curator toggles a paper tab between
markdown and PDF, the tab recomputes the highlight for the newly selected representation, because XML-derived markdown
and PDF text layers differ in whitespace, hyphenation, and ligatures.

### Highlight resolution: server-side, one matcher

An appealing shortcut for markdown highlights is to run a substring search in the browser with
`String.prototype.indexOf` and skip a server round-trip. That breaks whenever the agent's quote came from a different
representation or differs in whitespace, punctuation normalization, or soft hyphens from the rendered markdown: a
citation that the evidence service accepted at authoring time via `Validate` would fail to highlight in the browser.

To prevent authoring validation and UI reveal from drifting apart, quote matching lives in the Python evidence service
(`themis/services/evidence/literature/`), where `Validate` and `Locate` share one normalization-aware matcher. On
reveal, the browser calls `Locate(id, quote, representation)` (`schema/proto/themis/rpc/literature.proto`), which
returns one of three outcomes:

- **Markdown code-point offsets (`TextOffsets`):** the browser maps `[start, end)` code-point offsets (using the offset
  convention from [`frontend-framework.md`](frontend-framework.md)) to a DOM `Range` (`highlight.ts`) and registers it
  with the CSS Custom Highlight API (`CSS.highlights`), scrolling the range into view without mutating the DOM. Clearing
  or replacing the highlight drops the registered range.
- **PDF bounding boxes (`PdfRegion` with `{page, rects[]}`):** the browser renders absolutely positioned overlay `<div>`
  elements over the `react-pdf` (`pdf.js`) page, transforming normalized page coordinates through the page viewport.
- **Not located (`QuoteNotLocated`):** the tab clears any previous highlight and docks a persistent warning chip at the
  top of the tab's content area showing the unlocated quote text. The tab does not automatically switch representations.

Two transitional gaps remain in the current highlight path (`tab-views.tsx`, `highlight.ts`):

1. **Markdown offset application:** although `Locate(..., MARKDOWN)` runs the normalization-aware matcher on the server
   and returns `TextOffsets`, the browser currently consumes that response as a boolean match check and re-locates the
   quote in the rendered DOM text runs with `indexOf(quote)` (`locateQuoteInRuns` in `highlight.ts`). When a quote
   matches on the server only after whitespace or character normalization, `indexOf` misses in the browser and falls
   back to the warning chip. In browsers without `CSS.highlights`, a located quote also falls back to the warning chip.
1. **PDF matcher wiring:** the anchorite PDF matcher behind `Locate(..., PDF)` currently raises
   `PdfLocationUnavailableError` (gRPC `UNIMPLEMENTED`) until the cross-repo coordinate contract is finalized (see
   [Open questions](#open-questions)); the BFF catches `UNIMPLEMENTED` and surfaces the warning chip rather than failing
   the paper reveal.

### Windows: one source of truth, N thin mirrors

When a curator pops tabs out onto a second monitor, the multi-window state has to stay consistent without split-brain
races. Handing state ownership to child windows or running an election when the main window closes complicates every tab
move and close event.

Instead, the **main** window is the single source of truth (`workspace-sync.ts`), and every child window opened via
`window.open` is a **thin mirror** that renders only its own two-pane tab area:

- **Snapshot broadcast and command forwarding:** a `BroadcastChannel` carries a `WorkspaceSnapshot` from the main window
  to every child window and carries `WorkspaceCommand` messages from children back to the main window. Child windows do
  not mutate state locally; they send commands to the main window, which updates the reducer and broadcasts the new
  snapshot. The snapshot carries only layout structure and small signals: window, pane, and tab descriptors, split
  ratios, active tab IDs, the per-paper highlight quote map, the strip label mode, and the working document's current
  `(analysisId, tipCommit)` pair.
- **Large document bodies stay off the channel:** the conversation transcript is rendered only in the main window, and
  each window that holds the `working-doc` tab reads the working document revision directly from the browser's copy of
  the workspace git repository (`working-document.ts`, owned by a `SharedWorker` shared across same-origin windows as
  described in [`workbench-workspace.md`](workbench-workspace.md)). When the tip commit advances in the broadcast
  snapshot, any window displaying the tip re-reads the updated revision from the repository worker.
- **Working document version pin:** the `working-doc` tab payload carries an optional view-only pin
  `{analysisId, commit}` (`DocumentPin` in `workspace-model.ts`) set by the version picker in the tab header, which
  lists commits from the branch history in the browser's copy of the repository
  ([`workbench-workspace.md`](workbench-workspace.md); earlier pre-repository slices used integer versions over a
  fetched markdown body). A window rendering `working-doc` reads the pinned commit when present, or follows `tipCommit`
  when the pin is absent, ignoring any stale pin whose `analysisId` does not match the current Analysis. Storing the pin
  on the tab payload means it travels with the tab across windows over the existing snapshot channel and resets on page
  reload.
- **Shared renderer process:** child windows are opened via same-origin `window.open` without `noopener`, so they share
  the main window's renderer process and event loop. That lets the main window retain child `WindowProxy` handles to
  call `child.focus()` and `child.close()` directly, and ensures a renderer crash takes all windows down together rather
  than leaving orphaned mirrors. The cost is that heavy main-thread work (such as `pdf.js` canvas rasterization, which
  stays on the main thread even though PDF parsing runs in a worker) is shared across all open windows, mitigated by
  lazy per-page rendering.
- **Local divider gestures:** dragging a split divider updates panel sizes locally in the acting window and commits a
  single command on pointer release, avoiding a `BroadcastChannel` round-trip on every animation frame.
- **Window lifecycle and pinned tab safety:** closing the main window closes every mirror (`main-closing`), while
  closing a child window reparents any pinned tab (`working-doc`) into the main window's active pane and closes the
  child's closable tabs. If removing or moving tabs leaves a child window with zero tabs, the child window closes
  itself. If the main window closes — or if closing a child window via OS window chrome does not deliver `beforeunload`
  or `pagehide` — `working-doc` reconstitutes on relaunch (a singleton, server-backed) so it can never be permanently
  lost. Runtime bookkeeping scales as $O(N)$ in the number of child windows for window handles, close routing, and
  re-registering a child window that the curator reloads manually.
- **Persistence and restore:** on reload, the workbench restores how the curator arranges their workspace, not which
  papers they had open ([`workbench-navigation.md`](workbench-navigation.md)). IndexedDB (`lib/browser-store.ts`)
  persists four arrangement settings: the conversation dock edge, the horizontal and vertical outer split ratios, the
  strip label mode, and the inner two-pane split ratio. Open paper tabs, active highlights, the reopen-last-closed
  stack, and child windows are not persisted: which papers are open is a property of one Analysis, restoring them saves
  only a click on a citation, and because one route per Analysis remounts this workbench across a switch, a restored set
  would arrive under the wrong Analysis; `window.open` requires a user gesture and saved monitor coordinates break when
  a laptop is undocked.

### Accessibility

Every layout and tab operation has a keyboard-operable path (`pane.tsx`, `tab-area.tsx`):

- The per-tab and pane-header context menus (`role="menu"`) provide keyboard and touch access to split, swap,
  move-to-pane, and move-to-window operations; HTML5 drag-and-drop is a mouse-only convenience.
- Split dividers render as `role="separator"` with descriptive `aria-label` attributes (such as
  `"Resize conversation and documents"`) and support arrow-key resizing. The four-edge conversation dock selector uses
  text labels.
- Because child windows do not have user-assigned titles, the "Move to window" menu labels each destination window by
  its active or pinned tab title. When a tab moves to another window or a split pane collapses, keyboard focus moves to
  the source pane's new active tab.

### Backend seam: structured over Connect, bytes over presigned redirects

Litcache manifest resolution — choosing a rendering hash for a `doc_id` and mapping a figure or supplementary filename
to its `AssociatedFile` entry and content-addressed GCS object path — already lives in the Python evidence service
(`themis/services/evidence/literature/`). Reimplementing manifest parsing in the TypeScript BFF would duplicate that
logic across two languages. Instead, the evidence service exposes `DescribePaper`, `ResolveContent`, `Locate`, and
`Validate` (`schema/proto/themis/rpc/literature.proto`), and the BFF calls it through a TypeScript `LiteraturePort`
(`apps/web/src/server/ports.ts`), backed either by the live gRPC adapter (sending an IAM ID token for the evidence
service audience) or by an offline fixture adapter serving a seeded local corpus. (Currently, resolving an
`AssociatedFile` listed in `Manifest.files` without a cached `path` raises `MissingContentError` until on-demand fetch
and manifest write-back are wired in the evidence service.)

Between the browser and the BFF, requests split across two seams according to payload shape:

```
Browser                           Next.js BFF                           Python Evidence Service / GCS
  │                                    │                                              │
  ├─ 1. Connect RPC (JSON/proto) ─────►│ Workbench.DescribePaper / Locate             │
  │     DescribePaper(doc_id)          ├──────────── gRPC (LiteraturePort) ──────────►│ Evidence service
  │     Locate(doc_id, quote, repr)    │                                              │
  │                                    │                                              │
  ├─ 2. HTTP GET /api/papers/[id]/... ►│ LiteraturePort.resolve* ────────────────────►│ Evidence service
  │     (markdown, pdf, files/[name])  │   returns gs://corpus-bucket/key             │   ResolveContent
  │                                    │ ContentPort Verify bucket pin + IAM signBlob │
  │◄─── 302 Location: https://storage.googleapis.com/... (signed V4 URL) ─────────────┤
  │                                    │                                              │
  └──── GET signed URL (Range / fetch / <img>) ──────────────────────────────────────►│ GCS corpus bucket
```

**Structured metadata and quote locations ride Connect.** `Workbench.DescribePaper` and `Workbench.Locate`
(`schema/proto/themis/workbench/rpc/workbench.proto`) are unary methods on the browser-to-BFF `Workbench` Connect
service, reusing `PaperInfo` and `LocateResponse` from `literature.proto`. Riding the Connect service gives both calls
`protovalidate` request validation, one error model, and default-on caller authentication (`proxy.ts` and `context.ts`)
rather than bespoke validation and error handling on custom REST routes.

**Paper content bytes ride HTTP GET routes that redirect to presigned GCS URLs.** Sending multi-megabyte PDFs, markdown
bodies, and figure images through Connect RPC responses would base64-inflate and buffer them in Node/Bun memory, and
browser consumers (`react-pdf`'s `<Document file={url}>` and `<img src={url}>`) expect a URL rather than an RPC payload.
Instead, `GET /api/papers/[id]/{markdown,pdf}` and `GET /api/papers/[id]/files/[name]` (`apps/web/src/app/api/papers/`)
call `LiteraturePort` to resolve the target object (`ResolveContent`) and hand the resulting `ContentObject` to
`ContentPort` (`apps/web/src/server/content-egress.ts`). On the live adapter, `ContentPort` responds with an HTTP `302`
redirect to a short-lived V4 presigned GCS URL; on the offline fixture adapter, where no GCS bucket exists,
`ContentPort` streams the seeded bytes directly. Redirecting to GCS lets `react-pdf` issue HTTP `Range` requests
directly to GCS for progressive PDF paging without buffering bytes through Cloud Run.

`ContentPort` is the generic BFF primitive for serving a bucket object to the browser, while bucket selection stays in
the domain adapter (`apps/web/src/server/adapters/live/literature.ts`). Unlike litcache papers, the working document is
not served through `ContentPort`: it lives in git packfiles inside sheaf's bucket, whose signed URLs are minted by the
sheaf service ([`workbench-workspace.md`](workbench-workspace.md)). Presigned paper delivery enforces five invariants:

- **Keyless V4 signing:** the web service account signs V4 GCS URLs via IAM `signBlob` (`serviceAccountTokenCreator` on
  itself), so the BFF holds no long-lived service account key.
- **Corpus bucket pinning:** the live literature adapter verifies that the `gs://` bucket returned by `ResolveContent`
  matches the configured litcache corpus bucket before passing the object to `ContentPort`. A bug or misconfiguration in
  the evidence service cannot trick the BFF into signing a read URL for any other bucket the web service account can
  read.
- **Safe egress typing ([`content-egress.ts`](../../apps/web/src/server/content-egress.ts)):** because publisher files
  are third-party content, only media types on the explicit inline allowlist in
  [`content-egress.ts`](../../apps/web/src/server/content-egress.ts) are served inline. Everything else — including
  `image/svg+xml` and `text/html` — is forced to `Content-Disposition: attachment` with an RFC 5987 `filename*` carrying
  the file's declared name rather than its content-addressed object hash. On the live path, GCS sets those headers via
  the signed URL's `response-content-disposition` and `response-content-type` query parameters. GCS does not emit
  `X-Content-Type-Options: nosniff` on signed URL responses, which is safe because `storage.googleapis.com` is
  cross-origin to the workbench and cannot execute script in the workbench origin; the same-origin fixture byte stream
  still sets `nosniff`.
- **CORS on the corpus bucket:** the litcache bucket configures CORS for the workbench origin so `pdf.js` and
  `fetch().text()` can read redirected PDF and markdown bodies cross-origin.
- **Redirect cache lifetime below signature lifetime:** the `302` response sets `Cache-Control: private, max-age=...`
  strictly shorter than the V4 signature's expiration window to leave a clock-skew and network-latency margin. Because
  the browser starts its cache age timer when it receives the `302` rather than when the BFF minted the signature, a
  cached redirect reused near the end of its `max-age` still points to a valid GCS signature. The TTL itself is chosen
  for the bearer-capability window, not freshness — the corpus is immutable.

**Markdown text is never rewritten before rendering.** `Locate` computes code-point offsets against the exact markdown
stored in litcache. Rather than rewriting relative figure references (`![](figure-1.png)`) into signed URLs inside the
markdown string — which would shift every subsequent code-point offset and misalign highlights — the markdown renderer
resolves image URLs at render time to `/api/papers/[id]/files/[name]`. Both `Locate` and the browser DOM therefore see
identical markdown text.

**Authorization requires admission only.** Litcache is a shared corpus and paper-level entitlement is a deferred
non-goal of the current evidence layer ([`literature-evidence-layer.md`](literature-evidence-layer.md)), so both the
Connect RPCs and the `/api/papers/...` routes admit any caller whom the ACL admits to the deployment, without checking
which Projects they belong to ([`access-control.md`](access-control.md)). A real entitlement model lands at the evidence
service later.

## Alternatives considered

- **N-up resizable pane row or recursive split tree in one window.** A recursive split tree or N-column row lets a
  single window show three or more documents at once, or mix horizontal and vertical document splits. We rejected it
  because reading surfaces rarely fit three readable documents on one monitor, and recursive splits add divider
  management and make reveal target placement hard to predict. Multi-window pop-out handles three-or-more documents
  across monitors without complicating single-window layout.
- **Single tab pane per window (relying on OS window tiling for side-by-side reading).** Dropping the in-window split
  makes each window a single tab strip. We rejected it because reading a claim beside its cited paper is the core
  verification loop, and an in-app two-pane split tiles two documents on one screen much more cleanly than OS window
  management.
- **Conversation as a tab or co-equal tile.** Treating the conversation as a tab reuses the tab strip and pane reducer
  for every surface. We rejected it because hiding the conversation behind document tabs interrupts steering, and
  docking the conversation in its own region lets document reveals split the tab area without displacing the
  conversation.
- **Reveal in place (switching tabs to compare) or reveal into "the other group".** Revealing a paper in the same pane
  as the citing document forces the curator to flip back and forth between tabs to compare a claim with its quote.
  Revealing into a fixed "other group" (the earlier two-group design) displaced whatever document was parked in that
  group. Splitting locally beside the source pane keeps both visible without disturbing distant panes.
- **Type-gated `dragover` discrimination.** Accepting a drag at `dragover` only when `dataTransfer.types` contains a
  custom MIME type is a common HTML5 DnD pattern, but OS drag sessions across browser windows can strip custom types,
  preventing drop targets from activating. Accepting any drag at `dragover` and validating the `BroadcastChannel` drag
  session at `drop` works reliably across windows and allows external file drops.
- **Transferring state ownership to a child window on main close, or running separate renderer processes (`noopener`).**
  Letting a child window take over when the main window closes avoids closing child mirrors, and opening children with
  `noopener` isolates renderer processes so a heavy PDF rasterization in one window cannot jank another. We rejected
  ownership hand-off because of split-brain hazards and a fragile hand-off — the thin mirror is simpler and main-window
  survival is not required — and deferred `noopener` because losing `WindowProxy` handles gives up direct
  `child.focus()` and `child.close()` calls for process isolation the workload does not yet need.
- **BFF reading litcache from GCS directly or spawning Python anchorite as a subprocess.** Reading manifests in
  TypeScript would duplicate Python resolution logic; spawning Python from the Bun BFF container would bloat the runtime
  image and bypass the gRPC service and fixture seams.
- **Precomputing PDF quote bounding boxes at ingestion.** Precomputation is impossible because `:quote[id, text]`
  directives are authored dynamically by the agent with arbitrary quote substrings, so `Locate` resolves them on demand.
- **`grpc-js` + `ts-proto` instead of `@connectrpc/connect-node` for the BFF's gRPC client.** Using `ts-proto` would add
  a second protobuf codegen pipeline producing message types incompatible with `@bufbuild/protobuf`, whereas
  `connect-node` reuses the existing generated messages across both Connect and gRPC transports.

## Open questions

- **Anchorite PDF coordinate contract.** What page indexing (0-based vs 1-based), origin corner, and coordinate units
  will anchorite's PDF matcher emit in `PdfRegion` (`schema/proto/themis/rpc/literature.proto` currently reserves
  normalized `[0, 1]` coordinates from the top-left corner)? Pinning that contract with anchorite's owner unblocks
  wiring `Locate(..., PDF)`.
- **Recording source representation on `:quote` directives.** A `:quote[id, text]` directive does not record whether the
  agent extracted `text` from the XML-derived markdown or the PDF text layer, so a valid quote from one representation
  can miss in `Locate` when the curator views the other. Recording the source representation in the directive would
  avoid false misses, but requires an upstream agent-authoring change.
- **Applying server-returned markdown offsets in the browser.** Switching `tab-views.tsx` and `highlight.ts` from
  client-side `indexOf(quote)` to applying the `[start, end)` code-point offsets already returned by
  `Locate(..., MARKDOWN)` will allow normalization-only server matches to highlight in the DOM.
