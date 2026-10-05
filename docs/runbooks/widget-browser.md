# Runbook: the widget browser

The widget browser is a page of the web app, run on your own machine, that lists example payloads for every widget type
and opens each one in the real workbench: the widget is drawn by its own component, in a working document, and its ticks
and notes work. Nothing leaves the machine, so it is the place to try a change to how a widget looks or behaves before
it becomes a pull request. How widgets work is in [`document-widgets.md`](../design/document-widgets.md).

## Prerequisites

- A checkout of this repository, with [Bun](https://bun.sh) and `git` installed.
- [uv](https://docs.astral.sh/uv/), which runs the Python check on the examples and installs the Python version it
  needs; `uv sync --all-groups` at the repository root sets it up once.
- Google Chrome (or Chromium), if you want the window Claude can look at (§"Seeing the page with Claude").

## Start it

```sh
cd apps/web
bun install
THEMIS_BACKEND=fixture bun run dev
```

Then open <http://localhost:3000/dev/widgets>. `THEMIS_BACKEND=fixture` runs the app against its offline fixture
backend, which signs you in as a local dev user and keeps everything in memory; the page does not exist on any other
backend. The dev server reloads a changed component by itself. To stop it, press Ctrl-C in the terminal it runs in; if
Claude started it in the background, ask Claude to stop it.

Each example is a card: click it to open that example. Each widget type also has an **Open all** button. Opening makes a
new Analysis in the Fixture Project whose working document holds the examples, and takes you to it. Ticks and notes you
make there are published to that Analysis's repository as they would be on a real one, but the repository lives in the
server's memory and is gone when the server stops. To start again from a clean copy, open the example again.

## The examples

Examples are files named `apps/web/src/widgets/examples/<widget>/<name>.txtpb`, in protobuf's text format: one
`field: value` per line, and a message's fields inside `{ }`. Every file starts with two header lines naming its schema,
then a short description as comments, which the browser shows beside the example:

```text
# proto-file: themis/widgets/models/checklist.proto
# proto-message: themis.widgets.models.Checklist
#
# The smallest checklist: one item, not ticked.

items {
  id: "segregation"
  label: "The family segregation is counted once"
}
```

The browser reads the files each time you open an example, so an edited example shows the next time you open it. It
reads only files one folder deep with the `.txtpb` extension; a file anywhere else is not listed. A name uses letters,
digits, `-`, `_` and `.`, since it becomes the path the working document embeds. A file that does not read, a misspelt
field say, is listed with the line and the reason; one that reads but breaks its widget's rules opens, and its widget
shows the error a real document would. The payload's schema, with a comment on every field, is in
`schema/proto/themis/widgets/models/`.

To add an example, copy a file of the same widget, change it, and check it:

```sh
bun test src/server/widget-examples.test.tsx                 # in apps/web
uv run pytest themis/widgets/tests/test_examples.py          # from the repository root
```

The first reads every example as the browser does and draws it; the second reads it with protobuf's own text-format
parser, so the files stay in the standard format. The repository is mirrored publicly, so an example holds invented text
and never content from a real case or a real session.

## Seeing the page with Claude

`bun run browser` opens a Chrome window you work in and Claude can look at:

```sh
bun run browser open http://localhost:3000/dev/widgets        # opens the window, or points it at the URL
bun run browser click '[data-example="svcv4-classification/myh7-crowded"]'
bun run browser wait '[data-widget="svcv4-classification"]'  # until the widget is drawn
bun run browser shot /tmp/alternatives.png --selector '[data-section="alternatives"]'
bun run browser shot /tmp/page.png                            # the window's whole page
bun run browser js 'document.title'
bun run browser close
```

`click` waits after clicking until the page has made no request for half a second, so opening an example needs no pause
before the next command; `wait` waits until an element is on screen. Both give up after 60 seconds, or `--timeout <ms>`:
the first visit to a page after the dev server starts compiles it, which can take tens of seconds. `shot --selector`
captures the whole element, even one taller than the pane it scrolls in, by briefly enlarging the page, and says so when
it cannot.

### Finding elements

The commands take [Playwright selectors](https://playwright.dev/docs/other-locators): CSS, as above, or
`text=Open all 3` and `role=checkbox[name="Reviewed: the routing"]`. Prefer the handles the pages carry for this:

- a card on the widget browser: `[data-example="<widget>/<name>"]`, and a widget's **Open all**:
  `[data-open-all="<widget>"]`;
- a widget: `[data-widget="checklist"]`, `[data-widget="svcv4-classification"]`;
- a part of the SVCv4 classification: `[data-section="…"]`, one of `summary`, `ruler`, `routing`, `codes` and
  `alternatives`.

Do not select by an `id` that looks generated, such as `_r_k_`: React makes those, and they change between renders and
builds. Text changes when the copy does, so a handle is the steadier choice.

The window stays open for you between commands, and each command acts on the tab you were last on. It runs with a
DevTools port on `127.0.0.1`, which gives any program on your machine control of that browser, so each window gets a new
profile in the temporary directory: do not sign in to anything in it. `bun run browser close` quits the window and
deletes its profile. To quit it yourself, use ⌘Q (Quit Google Chrome): closing its last window leaves that Chrome
running on macOS, and `open` then gives it a new tab. After you quit it, the next command deletes the old profile.

What `open` started is recorded in `~/.local/state/themis/widget-browser/port-9333.json` (under `$XDG_STATE_HOME` when
that is set, and named for the port). A command drives only the browser that file names, and only from the checkout that
opened it: if another browser answers on the port, or the window was opened from another checkout, it stops and says so,
and `THEMIS_BROWSER_PORT` picks another port.

Without a display (on a server, say), start the dev server as above and open the window headless:
`bun run browser open --headless http://localhost:3000/dev/widgets`. The other commands then work as they do with a
window, and `close` ends it the same way.

## Did anything break

After a change, run these in `apps/web`:

```sh
bun run typecheck   # the TypeScript compiler
bunx biome check    # lint and formatting
bun test            # the whole suite, about a minute
```

For a changed example, also run `uv run pytest themis/widgets/tests/test_examples.py` from the repository root. Then
open every example of the widget you changed, with its **Open all** button (`[data-open-all="<widget>"]`), and look at
each one: the tests check that a widget draws, not that it looks right.

## From an experiment to a pull request

Keep the change on a branch of its own. A change to how a widget draws is a change to a rendered surface, so its pull
request carries screenshots ([`CLAUDE.md`](../../CLAUDE.md), "CI and review"):

```sh
uv run --group screenshot python -m tools.screenshot.upload /tmp/alternatives.png
```

That uploads the image and prints the Markdown line to paste into the pull request's description. Open the pull request
as a draft; the repository's rules for committing, testing and review are in [`CLAUDE.md`](../../CLAUDE.md). When you
work with Claude Code, it follows the repository's `exploring-widgets` skill for all of the above.
