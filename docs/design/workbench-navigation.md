# Design: workbench navigation

**Status:** draft **Related:** [`frontend-framework.md`](frontend-framework.md) (web tier, BFF, the data-fetching path);
[`document-pane.md`](document-pane.md) (the Analysis page's interior — conversation region, tab area, mirror windows);
[`workspace-model.md`](workspace-model.md) (Project / Analysis / working document);
[`analysis-scenarios.md`](analysis-scenarios.md) (what an Analysis is created from, and how every surface here names
it).

## Overview

How the web surface divides into pages: a **Projects** page, a **Project** page that creates and lists Analyses, and the
**Analysis** page — the workbench, which shows one Analysis and nothing else. Each is a route, so moving between them is
browser navigation.

## Background

The whole surface is one route (`/`) with `?project=` and `?analysis=` query params (`searchParams` in Next's API), so
the workbench page also carries the Project selector, the "New analysis" composer, and the prior-analyses dropdown.
Three consequences:

- The Analysis under review shares its page with the controls that switch away from it, under a composer used once per
  Analysis and never again.
- The two params have to be held consistent — an `analysis` from one Project alongside a `project` naming another —
  which the workbench does with resolve-and-repair rules that exist only because the URL is flat.
- Reaching a prior Analysis means a 340px dropdown of prompt excerpts. That switches between two runs you already know
  about; it does not find one among many.

## Non-goals

- Search, filter, rename, archive/delete, pin. The list is read-and-create.
- Cross-Project browsing. The Project is the access boundary, and every Analysis *listing* a curator navigates is scoped
  to one; the Projects page's counts read across them (§Projects) but surface no Analysis.
- Creating an Analysis on the Analysis page. The create composer belongs to the Project page; the Analysis page's
  composer steers the run it is already showing ([`conversation-view.md`](conversation-view.md)) and starts nothing.
- Pagination. `ListAnalyses` returns a Project's Analyses whole.
- What a card says about an Analysis. The scenario decides that ([`analysis-scenarios.md`](analysis-scenarios.md)); this
  design places the cards.

## Design

### Routes

| Route                    | Renders                                                                                |
| ------------------------ | -------------------------------------------------------------------------------------- |
| `/`                      | The default landing page: the Projects the caller belongs to, then the curation panel. |
| `/project/[projectId]`   | One Project: a "New analysis" composer, then that Project's Analyses newest first.     |
| `/analysis/[analysisId]` | The workbench for one Analysis.                                                        |
| `/pane`                  | Unchanged — a mirror window's tab area ([`document-pane.md`](document-pane.md)).       |

A Project id never appears in an Analysis URL: the Analysis names its Project, so `/analysis/<id>` is complete on its
own and the cross-param pairing rules go with the flat URL.

The routes under `/curation` belong to the curation surface and are designed there
([`curation-surface.md`](curation-surface.md)). `/` composes that surface's panel beside the Projects one and owns
neither: each panel resolves its own caller and its own data.

### Projects (`/`)

One card per Project: its name and id, how many Analyses it holds, and when the most recent was created — a Project is
otherwise as opaque as a bare Analysis id. A card links to the Project page. A caller who belongs to none gets the
default-deny statement, not an empty grid.

The counts come from a single `listAllAnalyses()` — the chokepoint's batched read, which the port serves with
`listAnalysesIn(projectsOf(caller))` — so one query replaces one per Project. The membership set is still read twice:
the names and ids above come from `listProjects()`, and the page cannot hand its own scope to the batched read without
giving that method the parameter its authorization-by-construction exists to refuse
([`workspace-model.md`](workspace-model.md) §Authorization). One indexed read per render is what that refusal costs.

It fetches whole rows to count them, and a row carries the scenario inputs — up to 10 000 characters of prose this page
discards. Counting in SQL is therefore a new port method rather than a narrowing of this one: `listAllAnalyses()` is
defined by the `Analysis` rows it returns.

### Project (`/project/[projectId]`)

The Project name, then the composer (a scenario picker and that scenario's fields), then the Analyses as cards, newest
first. Creating navigates to `/analysis/<new id>` — the composer's only outcome is the Analysis it made, so the Project
page never has to show a run in progress. A Project with no Analyses shows the composer and says so.

A card is rendered by the scenario the Analysis was created from, and links to `/analysis/<id>`: a classification leads
with `NM_001382309.1:c.332del` over its clinical context, a free-form run shows its instruction
([`analysis-scenarios.md`](analysis-scenarios.md)). This page chooses the ordering and the grid; what fills a card is
never its decision, so a new scenario changes no code here.

### Analysis (`/analysis/[analysisId]`)

The workbench — conversation region plus tab area, unchanged ([`document-pane.md`](document-pane.md)). Its chrome
carries a back link to the Analysis's Project page, the Analysis's identity as its scenario renders it — the identifying
line, over the scenario's label and the creation time ([`analysis-scenarios.md`](analysis-scenarios.md) §"Identity is
derived") — and the conversation-dock control. Nothing on it switches Project or Analysis. The conversation region ends
in the steer composer, which carries the page's two mutations — a curator turn, and halting the run's current step
([`conversation-view.md`](conversation-view.md)).

### Chrome

`AppBar` keeps what every page shares — the logo and wordmark, linking to `/`, and the verified caller — and takes a
`left` and a `right` slot that each page fills:

| Page                     | left                                       | right                     |
| ------------------------ | ------------------------------------------ | ------------------------- |
| `/`                      | —                                          | —                         |
| `/project/[projectId]`   | back to Projects; the Project name         | —                         |
| `/analysis/[analysisId]` | back to the Project; the Analysis identity | conversation-dock control |

The bar is one row justified to both edges: the left group begins at the logo, the right group ends at the verified
caller. The slots are not interchangeable. `left` names where the curator is — the back link, and the name of the thing
on screen — and is the side that yields: it is the only part of the row allowed to shrink, so a long Project or Analysis
name truncates rather than pushing the row. `right` holds controls acting on the page in view, sits inboard of the
caller, and never shrinks, so a control is either wholly present or absent. An empty slot takes its divider with it.

The dock control moves to the Analysis page because it configures the conversation region, which exists only there. The
Project selector is deleted rather than moved: the Projects page is the selector.

Every back affordance is a link to a known route, never `history.back()` — a shared or bookmarked link arrives with no
history, and the target must not depend on how the page was reached.

### Where each page's data comes from

Live state — the poll and the working document — stays on the Connect client with TanStack Query. Everything else these
pages render is fixed for the life of the page (the caller's Projects, a Project's Analyses, an Analysis's identity), so
each page's server component reads it from the authorized backend through `userContext` and renders it. The client
islands are the composer, which calls `CreateAnalysis` through Connect and then navigates, the workbench itself, and
every rendered time (below) — which on `/` is the only one.

A time is shown on the **reader's** clock, which only the browser can resolve, and an elapsed form has to keep moving
after first paint. Both are one client component (`components/reader-time.tsx`): it renders the instant the page passed
it, then reformats in the host's zone and locale on mount and refreshes on a timer. The pinned render — UTC, a fixed
locale, its zone label — is computed once on the server and reaches the component as props, so the first client render
reproduces it by carrying it rather than by re-deriving it, and hydration reconciles nothing. Carrying it is what makes
the markup deterministic; a fixed locale would not, since `Intl` output tracks the host's ICU version and the server's
and the browser's are versioned apart. A time that is briefly the server's says which zone it is in, and the
machine-readable instant stays in the markup as `<time dateTime>` whatever the label reads.

Moving the lists off TanStack Query removes the invalidation a mutation used to trigger, and a server-rendered list has
no equivalent: Next's client Router Cache would serve the pre-create payload when a curator navigates back to the
Project page they just created in. The create composer therefore refreshes the router cache before it navigates, which
is the whole of the freshness rule for the curator's own create: the only other mutation is a steer, which changes
nothing any page rendered on the server and so needs no refresh — its freshness path is the poll.

A co-member's create is not covered, and is accepted rather than solved. A Project is M:N to users
([`GLOSSARY.md`](../../GLOSSARY.md)), so an Analysis another curator starts changes what `/project/[projectId]` lists
and what `/` counts, with nothing to invalidate a cache the creating browser does not hold. Those pages are therefore
current as of the navigation that rendered them; a curator who wants a colleague's newest run navigates or reloads.
Closing that would mean polling every listing, or a shared invalidation channel, for a page a curator arrives at fresh
and leaves — a cost the staleness does not justify.

Each page therefore resolves its subject before it renders. `/analysis/[analysisId]` is `notFound()` for an unknown id
or one outside the caller's Projects, rather than a workbench polling a dead id behind chrome that names nothing.
`/project/[projectId]` resolves the same way and needs it more, since it renders a composer: a Project outside the
membership must not reach a page that offers to create in it. Its name and that check are one read — the Project is
found in `listProjects()` or it is `notFound()`, for the same existence-hiding reason a point access is.

Two additions to the backend surface carry it. `getAnalysis(analysisId)` replaces `projectOfAnalysis` on
`AnalysisDataPlane` and `AuthorizedBackend`: an Analysis row carries its `project_id`, so point-access authorization
reads the Project from the row it just fetched rather than from a second query for that one column, and a non-member is
answered with the same not-found as any other point access. The check *returns* that row, and `pollEvents` takes it
instead of an id — otherwise the poll would read it twice every 2.5s tick, once to authorize and once inside the method
it authorized. And `listAllAnalyses()` reads every Analysis the caller can reach in one go, which `AuthorizedBackend`
did not expose: `listAnalyses(projectId)` is per-Project, so the batched form is its own method, authorized by
construction because the membership set *is* its scope rather than something to check against.

The live SQL adapter already implements the row read; the fixture returns its held entry. The workbench proto gains
nothing: the browser never calls these.

### The arrangement persists; the open papers do not

A `workbench:layout` record used to hold the main window's panes and their tabs, the tabs open in popped-out windows,
and the reopen stack. A switch between Analyses would rehydrate one Analysis's papers into the next: a paper is open
because *this* Analysis cited it (`paper:<docId>`, `supp:<docId>:<name>`).

The switch remounts the workbench because its root is keyed on the Analysis id. The route change alone does not: the App
Router re-renders `/analysis/[analysisId]` with new `params` and reconciles the client subtree by position, so without
the key one Analysis's panes, tabs and channel would carry into the next, and the unmount teardown below would never
fire for the navigation it exists for.

What persists is the arrangement — on a global key, alongside the conversation edge, the outer split ratios, and the
tab-label mode. The line is the principle, not a consequence of the routing split: **the workbench persists how a
curator works, never what they were working on.** How the panes are divided is a preference a curator sets once and
wants everywhere; which papers were open is a property of one Analysis, and restoring it earns nothing — a returning
curator is reading the conversation, from which any paper is one click away. Anything scoped to an Analysis therefore
belongs to that Analysis's session, not to the browser.

In practice that is the inner two-pane ratio and those existing preferences, and nothing else: with no papers to
restore, a load holds exactly one tab — the working document — so there is no split to re-establish, no second pane to
make active, and no side for the document to sit on. The ratio is what the next reveal splits at. The working-document
tab belongs to this half regardless: its id names no corpus document, so it means "the current Analysis's working
document" wherever it opens.

Open papers persist nowhere. An Analysis opens into the arrangement its curator last left, showing that Analysis's
working document; papers arrive as citations are revealed, and are gone once the page unloads. A reveal is one click
from the conversation or the document, which is not worth a per-Analysis record set that nothing ever deletes — it would
grow for the life of a browser profile, and storage the browser evicts under pressure is evicted per origin, taking the
arrangement with it.

This shrinks what is stored rather than adding to it. Paper and supplementary descriptors leave the payload, the
popped-out-window tab list and the reopen stack go with them, and the asynchronous rehydration that re-fetched them —
along with the reducer action it fed — goes too: there is no content left to restore.

### Leaving the Analysis page tears down its windows

Mirror windows close on a `main-closing` broadcast the main window posts from `beforeunload`. A route change is not an
unload, so without a second signal a popped-out window outlives the workbench that fed it and mirrors a channel nobody
publishes to. Teardown is therefore the workbench's unmount as well: post `main-closing`, close the child handles. That
is sound only while the effect installing the channel stays mount-scoped, which it is by invariant rather than by a
guard: every dep is identity-stable — the channel id is a `useState` initialiser, the handlers are `useCallback`s over
stable deps, and the values they need arrive through refs — so the cleanup runs at unmount and nowhere else. A dep that
changed identity mid-session would close the channel and tear the windows down under a live workbench, which is what to
check when touching that effect. Teardown belongs in *that* cleanup rather than a second effect: React runs cleanups in
declaration order, so a later effect would post into a channel the earlier one had already closed.

## Alternatives considered

- **Keep the flat URL; render the navigator when `?analysis=` is absent.** A smaller diff, but the landing page stays an
  absence of state rather than a page, and the project/analysis pairing rules stay alive with it.
- **One cross-Project list of Analyses, each card tagged with its Project.** Fewer clicks for a curator working across
  Projects, and `listAnalysesIn` already accepts a set. Rejected: the Project is the access boundary, and a navigation
  level that matches the boundary keeps it visible — a flat list demotes it to a chip.
- **Deriving a preview from the run — the working document's first heading, a running/produced state, last activity.**
  Rejected for the identifying line: it makes what an Analysis is *called* depend on how far it has got, so a card
  changes its name mid-run and an unstarted Analysis has none. Typed scenario inputs name it from what it was asked
  ([`analysis-scenarios.md`](analysis-scenarios.md)). Run state is still worth surfacing on a card, and is not designed
  here — it needs a listing that carries it, which today's `ListAnalyses` does not.
- **A navigator sidebar on the Analysis page.** Restores the global chrome this design removes, and spends horizontal
  space a two-pane workbench needs.
- **Nesting the Analysis under its Project: `/project/<projectId>/<analysisId>`.** The URL then reads as the hierarchy,
  and dropping a path segment navigates up — by hand, or as a breadcrumb. Rejected because the pairing it encodes is one
  the Analysis already carries: a correct Analysis id under the wrong Project id is a state the page has to detect and
  answer for, which is the resolve-and-repair class this design set out to delete. Navigating up is what the back link
  is, and it needs no second id to be right.
- **Redirecting `/` to the sole Project of a caller who belongs to one.** Saves a click on every visit, but the Project
  page's back link then lands on `/` and is bounced straight back — a link that does nothing — so the chrome would have
  to become conditional on membership size. `/` always renders the Projects page.

## Implementation state

Built on the `wb-nav-build` branch, unmerged; this doc is the PR beneath it.

The build slice covers the three pages and the `AppBar` slots, the arrangement-only layout store, unmount teardown, and
`getAnalysis` in place of `projectOfAnalysis` on the port; it deletes the Project selector, the analyses dropdown, the
Analysis-page composer, and the param resolve/repair rules. The fixture data plane seeds several Analyses across both
fixture Projects — it holds only what the running process created, so an offline navigator is otherwise empty on first
load and the surface cannot be reviewed (or screenshotted) without one.
