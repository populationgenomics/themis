Scheduled documentation-gardening run: fixes for documentation that has drifted from the code or the tree — broken links
and paths, stale status markers, behavioural claims the code contradicts, terminology drift.

This PR is opened and maintained by the `doc garden` workflow. Its branch, `doc-garden/rolling`, is reset from `main`
and force-updated on each run, so review the **current** diff — earlier states are not preserved. The agent's notes on
anything it found but did not fix (e.g. a referenced file that is genuinely missing) are in the run's
`claude-doc-garden-execution-output` artifact.

It is screened and reviewed like any other PR: the regex and LLM screens plus CODEOWNERS approval gate it before it can
merge and mirror to public.
