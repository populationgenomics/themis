Scheduled documentation-gardening run. The `doc garden` workflow audits the tracked docs against the code and tree and opens this PR with the drift it could fix; the agent's per-run notes follow below the line.

The diff may mix two kinds of edit: confident fixes, and **best-guess** fixes where the drift is real but the exact wording was a judgement call. The best guesses, and any drift the agent found but could *not* fix (e.g. a link whose target exists nowhere), are listed in the notes below — those want your input.

To resolve: point an agent at this PR, answer the open questions, let it update the branch, and merge. The branch `doc-garden/rolling` is reset from `main` and force-updated each run, so review the **current** diff — earlier states are not preserved.

Screened and reviewed like any other PR: the regex and LLM screens plus CODEOWNERS approval gate it before it can merge and mirror to public.
