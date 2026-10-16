"""The run-review loop's harness (docs/design/run-review-loop.md).

Two halves with a line between them. `run` creates a run through the web app as a curator's browser
would, waits for it to end, and collects its repository, raw event logs and provenance; it takes a
`runner.RunRequest` and knows nothing about variants. `variant_classification` is the one scenario:
it reads the curators' reference, builds each run's inputs from it, and renders the run's record and
the curators' submissions into text for the reader, with the coordinating session's, the reader's and
the generality check's instructions under its `prompts/`. Nothing here compares a run with its
reference; a fresh-context reader does.

Everything a round produces stays in the gitignored `rounds/`.
"""
