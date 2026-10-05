---
name: exploring-widgets
description: Run the local widget browser and change how a working-document widget looks or behaves, checking each change in a browser window the user can see, and turn the result into a draft pull request. Use when someone wants to try, tweak or review a widget's UI (the SVCv4 classification, the checklist, or a new one), add or edit a widget example, or asks to "play with" or "experiment with" a widget.
---

# Exploring widgets

The person asking may not write code. Make the change, show it, and explain what you changed in plain words. Ask before
anything leaves the machine: a push, a pull request, an upload.

Running it, the example format and the browser commands are in `docs/runbooks/widget-browser.md`; follow it.

- An example holds invented content only, never a real case's or session's: the repository mirrors publicly.
- For an SVCv4 example, compute the numbers with `themis.svcv4.widget.build`; never write totals by hand.
- Select by the pages' handles, never by generated ids or guessed sleeps: click `[data-example="<widget>/<name>"]` or
  `[data-open-all="<widget>"]`, then `wait '[data-widget="<widget>"]'`; shoot a part with
  `--selector '[data-section="<part>"]'`.
- After a change, before calling it done: `bun run typecheck`, `bunx biome check`, `bun test` in `apps/web`, the Python
  example test for a changed example, and open every example of the widget and look at each.
- Read every screenshot you take before describing it.
- The browser window is the user's too: drive it only while they are not working in it.
