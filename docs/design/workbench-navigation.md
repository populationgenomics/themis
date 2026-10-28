# Design: workbench navigation

**Related:** [`frontend-framework.md`](frontend-framework.md) (web tier, BFF, and data-fetching path);
[`document-pane.md`](document-pane.md) (the Analysis page interior: conversation region, tab area, and mirror windows);
[`workspace-model.md`](workspace-model.md) (Projects, Analyses, and the working document);
[`analysis-scenarios.md`](analysis-scenarios.md) (scenario inputs and derived Analysis identity);
[`conversation-view.md`](conversation-view.md) (the conversation stream and steer composer);
[`curation-surface.md`](curation-surface.md) (the curation pages under each Project).

## Overview

Putting Project selection, Analysis creation, prior-Analysis browsing, and the active Analysis workbench on a single
flat route (`/?project=...&analysis=...`) crowds the workbench with navigation and creation chrome and requires URL
resolve-and-repair logic whenever the two query parameters disagree. Instead, the web surface divides into three routes
that match the workspace hierarchy: a **Projects** page (`/`), a **Project** page (`/project/[projectId]`) that creates
and lists Analyses, and an **Analysis** page (`/analysis/[analysisId]`) that renders the workbench for one Analysis and
nothing that switches away from it.

Making each level its own route turns navigation into standard browser history and lets Next.js server components
authorize and render page-lifetime state (Project lists, Analysis lists, and Analysis identity) before first paint, so
unreachable IDs return `404` immediately. Live state (the event poll and the working document) stays on the Connect
client. Moving between Analyses remounts the workbench, persisting only the curator's layout preferences in IndexedDB
while discarding open corpus papers and closing popped-out mirror windows from the previous Analysis.

## Background

In the workspace hierarchy ([`workspace-model.md`](workspace-model.md)), a curator belongs to one or more **Projects**,
each Project scopes a set of **Analyses**, and each Analysis runs an agent session alongside a collaborative working
document inside the workbench ([`document-pane.md`](document-pane.md)).

Previously, the entire surface lived on one route (`/`) parameterized by `?project=` and `?analysis=` query strings
(`searchParams` in Next.js). Combining all three levels onto one route caused three problems:

- **The workbench shared screen space with navigation and creation controls.** The Analysis under review sat beneath a
  Project selector, a prior-Analyses dropdown, and a "New analysis" composer that is used once to start a run and never
  used again during that run.
- **Flat query parameters could contradict each other.** A URL could pair an `analysis` ID from one Project with a
  `project` ID naming another, forcing the page to carry resolve-and-repair rules that exist only because both IDs sit
  in the URL at once.
- **Prior Analyses were crammed into a narrow dropdown.** Reaching an earlier run meant scanning a 340px menu of prompt
  excerpts, which works for toggling between two recent runs you already know by sight but fails when browsing a
  Project's history.

## Non-goals

- **Searching, filtering, renaming, archiving, deleting, or pinning Analyses.** The Project page is a read-and-create
  list ordered newest first.
- **Cross-Project Analysis browsing.** The Project is the access boundary, and every Analysis listing a curator
  navigates is scoped to a single Project. The Projects page aggregates counts across the caller's Projects but surfaces
  no individual Analysis rows.
- **Creating an Analysis from the Analysis page.** Starting a new Analysis belongs to the Project page; the composer on
  the Analysis page only steers or interrupts the run already on screen
  ([`conversation-view.md`](conversation-view.md)).
- **Pagination.** `ListAnalyses` returns all of a Project's Analyses in one response.
- **Defining what an Analysis card displays.** Each scenario formats its own card content and identifying header
  ([`analysis-scenarios.md`](analysis-scenarios.md)); this design only places those cards into the page grid and chrome.

## Design

### Routes and page hierarchy

Each level of the workspace hierarchy maps to its own route under [`apps/web/src/app/`](../../apps/web/src/app):

| Route                    | Renders                                                                             |
| ------------------------ | ----------------------------------------------------------------------------------- |
| `/`                      | The landing page: the Projects panel.                                               |
| `/project/[projectId]`   | One Project: a "New analysis" composer above that Project's Analyses, newest first. |
| `/analysis/[analysisId]` | The workbench for one Analysis.                                                     |
| `/pane`                  | A mirror window's tab area ([`document-pane.md`](document-pane.md)).                |

```
/ (LandingPage)
├── AppBar: [Logo "Themis"] ─────────────────────────────────────── [user@example.org]
└── ProjectsPanel (apps/web/src/workbench/projects-panel.tsx)
    └── ProjectCard ──links to──► /project/[projectId]

/project/[projectId] (ProjectPage)
├── AppBar: [Logo "Themis" | ← Projects  ● Project ID] ──────────── [user@example.org]
├── Curation ("Open curation", for a role holder) ──links to──► /project/[projectId]/curation
├── Composer ("New analysis") ──on create──► router.refresh() + router.push("/analysis/<id>")
└── AnalysisCard grid (newest first) ──links to──► /analysis/[analysisId]

/analysis/[analysisId] (AnalysisPage -> Workbench key={analysis.id})
├── AppBar: [Logo "Themis" | ← Project ID  Scenario · Time / Title] ─── [Dock | user@example.org]
└── ConversationSplit: ConversationPane (+ SteerComposer)  |  TabArea (+ /pane mirror windows)
```

A Project ID never appears in an Analysis URL. Because every `Analysis` record carries its owning `project_id`,
`/analysis/<analysisId>` is self-contained and the cross-parameter pairing rules disappear with the flat URL.

Routes under `/project/[projectId]/curation` belong to the curation surface
([`curation-surface.md`](curation-surface.md)), and the Project page links to them. The landing page
([`apps/web/src/app/page.tsx`](../../apps/web/src/app/page.tsx)) renders the Projects panel
([`apps/web/src/workbench/projects-panel.tsx`](../../apps/web/src/workbench/projects-panel.tsx)), which resolves its own
caller context and fetches its own data.

### Projects page (`/`)

The Projects panel renders one card per Project the caller belongs to, linking to `/project/<projectId>`. Each card
shows the Project's ID, how many Analyses it holds, and when the most recent Analysis was created — without the count
and latest timestamp, a Project card would be as opaque as a bare identifier. A caller who belongs to no Projects sees
an explicit default-deny notice (`"You are not a member of any Project."`) rather than an empty grid. A Project's ID is
the name of its dataset, and datasets have no display names, so every page shows a Project by its ID
([`access-control.md`](access-control.md)).

To populate the counts and latest timestamps without issuing $N$ queries for $N$ Projects, the panel calls
`listAllAnalyses()` on [`AuthorizedBackend`](../../apps/web/src/server/authorized-backend.ts), which delegates to
`listAnalysesIn(projectsOf(caller))` on [`AnalysisDataPlane`](../../apps/web/src/server/ports.ts). Even though the panel
also calls `listProjects()` to get the Project IDs, it does not pass that Project list into `listAllAnalyses()`.
Accepting a caller-supplied scope parameter on `listAllAnalyses()` would weaken the chokepoint rule that every method on
`AuthorizedBackend` derives its own scope from `ProjectMembership` ([`workspace-model.md`](workspace-model.md)). Keeping
`listAllAnalyses()` parameterless costs one extra indexed membership lookup per render of `/`.

`listAllAnalyses()` also fetches full `Analysis` rows — including scenario inputs of up to 10,000 characters of clinical
context or prompt prose — only for `/` to count them and take `max(created_at)`. Pushing that aggregation into SQL will
require a dedicated summary method on `AnalysisDataPlane` rather than narrowing `listAllAnalyses()`, which is defined by
the `Analysis` rows it returns.

### Project page (`/project/[projectId]`)

The Project page (under [`apps/web/src/app/project/`](../../apps/web/src/app/project)) renders the Project ID in the app
bar, the "New analysis" composer, and the Project's Analyses as a grid of cards ordered newest first. Submitting the
composer creates the Analysis and immediately navigates to `/analysis/<newId>`; because the new Analysis is the
composer's only outcome, the Project page never has to display a run in progress. A Project with no Analyses yet renders
the composer above an explicit empty state.

Each card links to `/analysis/<id>` and delegates its interior content to the scenario the Analysis was created from
([`analysis-scenarios.md`](analysis-scenarios.md)): for example, a variant-classification card leads with the variant
identifier (`NM_001382309.1:c.332del`) in monospace above its clinical context, whereas a free-form card renders its
prompt instruction. Because the Project page owns only the chronological ordering and grid layout, adding a new scenario
requires no changes to the Project page.

### Analysis page (`/analysis/[analysisId]`)

The Analysis page (under [`apps/web/src/app/analysis/`](../../apps/web/src/app/analysis)) renders the workbench: the
conversation region and the tab area ([`document-pane.md`](document-pane.md)). Nothing on the page switches to another
Project or Analysis.

Its app bar carries a back link to `/project/<projectId>`, the Analysis's scenario-derived identity (the scenario label
and creation time above the one-line identifying title, with the full clinical context or prompt in a hover tooltip; see
[`analysis-scenarios.md`](analysis-scenarios.md)), and the conversation-dock control. At the bottom of the conversation
region, the steer composer ([`conversation-view.md`](conversation-view.md)) provides the page's only two mutations:
appending a curator turn (`Steer`) and halting the agent's current step (`Interrupt`).

### Shared chrome (`AppBar`)

[`AppBar`](../../apps/web/src/components/app-bar.tsx) renders the elements shared across every route — the logo and
wordmark linking to `/` on the far left, and the verified caller's email on the far right — and exposes `left` and
`right` slots that each page populates:

| Page                     | `left` slot                                        | `right` slot              |
| ------------------------ | -------------------------------------------------- | ------------------------- |
| `/`                      | —                                                  | —                         |
| `/project/[projectId]`   | Back link to `/`; Project ID                       | —                         |
| `/analysis/[analysisId]` | Back link to `/project/[projectId]`; Analysis name | Conversation-dock control |

The two slots behave differently under horizontal compression:

- **`left` names where the curator is** (the back link and the title of the current Project or Analysis) and is the only
  flex child allowed to shrink (`min-w-0`), so a long Project ID or variant identifier truncates with an ellipsis rather
  than pushing the bar wider than the viewport.
- **`right` holds controls that act on the current view** (sitting just inboard of the verified caller) and never
  shrinks (`shrink-0`), ensuring interactive controls are either wholly visible or absent. When a slot is empty, its
  vertical divider is omitted with it.

The conversation-dock control lives only on `/analysis/[analysisId]` because the conversation region exists only there.
The old app-bar Project selector is removed rather than relocated: `/` is the Project selector.

Every back affordance (`BackLink`) is an `<a href="...">` to a known route, never `history.back()`. A curator opening a
shared or bookmarked `/analysis/<id>` link arrives with no prior history entry, and the back link's destination must be
`/project/<projectId>` regardless of how the page was reached.

### Server-rendered page data and client freshness

Data that changes while a page is open — the 2.5-second event poll and the working document — stays on the Connect
client with TanStack Query ([`apps/web/src/lib/queries.ts`](../../apps/web/src/lib/queries.ts); see
[`frontend-framework.md`](frontend-framework.md)). Everything else these pages render is fixed for the lifetime of the
page visit (the caller's Projects on `/`, the Project metadata and Analysis list on `/project/[projectId]`, and the
Analysis identity and owning Project ID on `/analysis/[analysisId]`). Each route's async server component reads that
state directly from [`AuthorizedBackend`](../../apps/web/src/server/authorized-backend.ts) via `userContext`. Only three
client islands mount across the three pages:

1. The create composer on `/project/[projectId]`, which calls `CreateAnalysis` over Connect and navigates to the new
   Analysis.
1. The workbench on `/analysis/[analysisId]`.
1. Every rendered timestamp, via [`ReaderTime`](../../apps/web/src/components/reader-time.tsx) (which is the only client
   component on `/`).

#### Deterministic timestamp hydration (`ReaderTime`)

Timestamps must be formatted in the reader's local timezone and locale, which only the browser knows, and relative
elapsed labels (`"4 min ago"`) must tick forward while the page remains open. Both behaviours live in
[`ReaderTime`](../../apps/web/src/components/reader-time.tsx) (backed by
[`apps/web/src/lib/format.ts`](../../apps/web/src/lib/format.ts)).

If the server and the browser both ran `Intl.DateTimeFormat` on the same ISO timestamp — even with a fixed locale and
`UTC` timezone — hydration could still mismatch because `Intl` output tracks the host's ICU data version, and the
Node.js runtime and the curator's browser are versioned independently. Instead, the server formats the pinned UTC label
once and passes that string to `ReaderTime` as `pinnedLabel`. `ReaderTime` uses `pinnedLabel` verbatim for its initial
render and then re-derives the relative label and hover tooltip on the browser's clock inside `useEffect` (updating
every 20 seconds). Hydration therefore compares identical strings without re-running `Intl`. The absolute instant
appears only once mounted on the reader's clock, and `<time dateTime={iso}>` always carries the machine-readable
instant.

#### Cache invalidation on create and co-member staleness

Moving Project and Analysis listings from TanStack Query to server components removes the client-side query invalidation
that `CreateAnalysis` previously triggered. Without an explicit cache refresh, Next.js's client-side Router Cache would
serve the pre-create RSC payload when a curator creates an Analysis and then clicks the back link to
`/project/[projectId]`. To keep the curator's own navigation fresh, the composer calls `router.refresh()` immediately
before `router.push('/analysis/<newId>')`. The only other workbench mutations (`Steer` and `Interrupt`) modify no
server-rendered listing data and get their freshness from the live event poll.

A co-member's create is not covered by client-side invalidation, and that staleness is accepted rather than solved.
Because a Project has several members ([`GLOSSARY.md`](../../GLOSSARY.md)), a colleague starting a run changes what
`/project/[projectId]` lists and what `/` counts with no signal to other browsers. Those pages are therefore current as
of the navigation that rendered them; a curator who wants a colleague's newest run navigates or reloads. Closing that
would mean polling every listing page, or a shared invalidation channel, for a page a curator arrives at fresh and
leaves — a cost the staleness does not justify.

#### Pre-render authorization and backend port methods

Because each server component resolves its subject before rendering, an unknown ID or an ID outside the caller's
Projects triggers Next.js `notFound()` (`404`) before any page shell mounts. Visiting `/analysis/[analysisId]` for an
unreachable Analysis returns `404` immediately instead of mounting a workbench that polls a dead ID behind blank chrome.
Visiting `/project/[projectId]` for an unreachable Project likewise returns `404` before rendering a composer that would
offer to create inside a forbidden Project; looking up `projectId` in `backend.listProjects()` checks membership and
retrieves the Project in a single read.

Two methods on [`AnalysisDataPlane`](../../apps/web/src/server/ports.ts) and
[`AuthorizedBackend`](../../apps/web/src/server/authorized-backend.ts) support these server reads without adding RPCs to
[`workbench.proto`](../../schema/proto/themis/workbench/rpc/workbench.proto):

- **`getAnalysis(analysisId)` replaces `projectOfAnalysis(analysisId)`.** An `Analysis` row already carries its
  `project_id`, so point-access authorization fetches the full row once, verifies that the caller belongs to
  `analysis.projectId` (raising `ResourceNotFoundError` on failure to preserve existence-hiding), and returns the row.
  Downstream methods such as `pollEvents(analysis)` accept that `Analysis` row rather than re-querying by ID; otherwise
  every 2.5-second poll tick would read the row twice (once to authorize and once to read `session_id`).
- **`listAllAnalyses()`** exposes the batched cross-Project read (`listAnalysesIn(projectsOf(caller))`) on
  `AuthorizedBackend`, authorized by construction because the caller's membership set defines the query scope.

### Persisting layout arrangement, not open papers

Previously, a single `workbench:layout` browser-store record persisted the main window's panes and tabs, the tabs open
in popped-out mirror windows, and the reopen stack. Switching from one Analysis to another would rehydrate the previous
Analysis's cited papers (`paper:<docId>` and `supp:<docId>:<name>`) into the new Analysis's workbench.

On `/analysis/[analysisId]`, the server page renders `<Workbench key={analysis.id} ... />`. Keying on `analysis.id` is
required because the Next.js App Router reconciles the client subtree by position when route parameters change; without
the key, navigating between two Analyses would carry the first Analysis's open panes, tabs, and `BroadcastChannel` into
the second, and the unmount cleanup below would never run.

Across Analysis mounts, [`useWorkspaceModelController`](../../apps/web/src/components/workbench/use-workspace-model.ts)
persists only how the curator arranges the workbench in IndexedDB
([`apps/web/src/lib/browser-store.ts`](../../apps/web/src/lib/browser-store.ts)). The line between persisted and
ephemeral state is a principle for future state, not a consequence of the routing split: **the workbench persists how a
curator works, never what they were working on.**

| Persisted across Analyses (global browser-store keys)     | Scoped to one Analysis mount (never persisted) |
| --------------------------------------------------------- | ---------------------------------------------- |
| Conversation dock edge (`left`, `right`, `top`, `bottom`) | Open corpus paper and supplementary file tabs  |
| Horizontal and vertical outer split ratios                | Popped-out mirror windows and their tab lists  |
| Per-pane-side tab-strip label mode (`a`, `b`)             | Closed-tab reopen stack                        |
| Inner two-pane split ratio                                | Active pane selection and secondary pane split |

How the panes are divided is a preference a curator sets once and wants everywhere; which papers were open is a property
of one Analysis, and restoring it earns nothing — a returning curator is reading the conversation, from which any paper
is one click away. Anything scoped to an Analysis therefore belongs to that Analysis's session, not to the browser.
Every Analysis therefore opens in the curator's saved arrangement with a single tab — that Analysis's working document
(`doc:working`, which names the current Analysis's working document rather than a specific corpus item). Because only
one tab is present on load, there is no second pane to restore; the saved inner two-pane ratio is applied the next time
a citation reveal splits the tab area.

Open papers are not persisted per Analysis either. Re-opening a cited paper is a single click from the conversation or
working document, whereas storing per-Analysis tab records in IndexedDB would accumulate unbounded entries over the life
of a browser profile — and browser storage eviction under quota pressure operates per origin, which would wipe the
curator's global layout preferences along with the tab records. Dropping tab persistence also removes asynchronous paper
rehydration on mount.

### Tearing down mirror windows on route leave

Popped-out mirror windows (`/pane`) close themselves when the main window broadcasts `{ kind: "main-closing" }` from its
`beforeunload` listener ([`document-pane.md`](document-pane.md)). However, client-side navigation away from
`/analysis/[analysisId]` (for instance, clicking the back link to `/project/[projectId]`) does not unload the browser
tab. Without a second teardown trigger, any popped-out mirror windows would outlive the workbench that fed them and stay
open on a dead `BroadcastChannel`.

To close mirror windows on route leave as well as tab close, the cleanup function of the channel-installation effect in
[`useWorkspaceWindow`](../../apps/web/src/components/workbench/use-workspace-window.ts) posts `{ kind: "main-closing" }`
and calls `handle.close()` on every tracked child window before closing the `BroadcastChannel`.

Putting window teardown in that effect's cleanup imposes two invariants on `useWorkspaceWindow`:

1. **The effect must remain strictly mount-scoped.** Every dependency of the channel effect (`channelId` initialized via
   `useState`, and `handleChildClosing` wrapped in a `useCallback` with no changing dependencies, reading latest state
   through refs) must remain referentially stable for the lifetime of the `<Workbench>` mount. If any dependency changed
   identity mid-session, React would re-run the effect cleanup and close all of the curator's popped-out windows while
   the Analysis is still open.
1. **Teardown must live in the channel effect's own cleanup, not a separate `useEffect`.** React runs effect cleanups in
   declaration order on unmount; a second effect declared after the channel effect would try to post `main-closing` into
   a `BroadcastChannel` that the first effect's cleanup had already closed.

## Alternatives considered

- **Keep the flat `/` URL and render the navigator when `?analysis=` is absent.** This requires a smaller routing
  change, but leaves the landing view as the absence of a query parameter rather than its own route and keeps the
  `project`/`analysis` parameter pairing and repair rules alive.
- **Render one cross-Project list of Analyses, tagging each card with its Project.** This saves a click for curators who
  switch frequently across Projects, and `listAnalysesIn` already accepts multiple Project IDs. We rejected it because
  the Project is the authorization and data-access boundary; giving Projects their own navigation level keeps that
  boundary prominent, whereas a flat cross-Project list demotes it to a badge on each card.
- **Derive an Analysis card's title from run progress (such as the working document's first heading, run status, or last
  activity).** Using produced output as the identifying line makes an Analysis's name change mid-run and leaves an
  unstarted Analysis unnamed. Scenario inputs name the Analysis from what it was asked to do from the moment it is
  created ([`analysis-scenarios.md`](analysis-scenarios.md)). Surfacing run status alongside that title remains useful,
  but requires `ListAnalyses` to carry run state, which it does not today.
- **Keep a navigator sidebar on the Analysis page.** A persistent sidebar allows one-click switching between Analyses,
  but restores the global navigation chrome this design removes from the workbench and consumes horizontal space needed
  by the conversation region and two-pane tab area.
- **Nest the Analysis route under its Project (`/project/[projectId]/[analysisId]`).** Nesting makes the URL path mirror
  the workspace hierarchy and lets a user navigate up by editing the URL path. We rejected it because an `Analysis`
  already records its `project_id`: putting both IDs in the path reintroduces the possibility of a valid `analysisId`
  paired with the wrong `projectId`, requiring the same resolve-and-repair logic that removing `?project=` eliminated.
  The app bar's back link already navigates up to `/project/<projectId>` without needing the second ID in the URL.
- **Redirect `/` to `/project/<id>` when the caller belongs to exactly one Project.** This saves one click for
  single-Project curators on initial entry, but breaks the Project page's back link: clicking "Projects" on
  `/project/<id>` would navigate to `/` and immediately bounce back to `/project/<id>`, forcing the app bar chrome to
  become conditional on how many Projects the caller belongs to. Rendering `/` unconditionally keeps navigation
  predictable.
