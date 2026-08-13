---
name: restacking-a-pr-stack
description: Restack one of this repo's PR stacks — rebase the chain onto a fresh main, or propagate a commit folded into a mid-stack PR up through every branch above it. Also covers combining, growing and reshaping the GitHub stack object. Use when asked to restack, to rebase a stack, to fold a fix into a lower PR, or to link PRs into a stack.
---

# Restacking a PR stack

A stack here is a chain of branches, each based on the one below, plus a GitHub-native stack object grouping the PRs
(`PullRequestStack` in GraphQL). The chain is git; the stack object is metadata. They move independently, and a restack
touches only the chain.

`gh stack` (the `github/gh-stack` extension) drives both; install it with `gh extension install github/gh-stack`. Where
`gh` cannot infer the repository from the remote URL — an SSH alias, a non-`github.com` host — export
`GH_REPO=populationgenomics/themis-internal`: `gh stack` fails to resolve it in cases plain `gh pr` handles.

## Preflight

**A worktree holding a branch blocks the whole operation.** Git refuses to update a ref checked out in another worktree,
and stack branches accumulate checkouts under `.claude/worktrees/`, so much of a stack is typically held. Detach every
holder first, then re-attach after pushing:

```bash
git worktree list --porcelain | awk '/^worktree /{w=$2} /^branch /{print $2, w}'   # who holds what
git -C <worktree> checkout --detach                                                # frees the ref, keeps the worktree
git -C <worktree> checkout <branch>                                                # after the push
```

Detaching is lossless only if the worktree is clean and its branch is pushed — check both
(`git -C <wt> status --porcelain`, local sha vs `origin/<branch>`) rather than assuming.

## With `gh stack`

`gh stack link` creates no local tracking, so adopt the stack before rebasing it. This switches the main worktree's
HEAD.

```bash
gh stack checkout <stack-number>   # fetches branches, sets up tracking
gh stack rebase                    # fetch trunk, fast-forward it, cascade every branch
gh stack rebase --no-trunk         # cascade only — propagate a mid-stack fold without adopting a newer main
gh stack rebase --upstack          # current branch to the top
gh stack push                      # per-branch --force-with-lease; not atomic, so re-run after fixing a rejection
```

With more than one remote configured, name it (`--remote <name>`): the push refuses rather than guessing.

It derives each child's fork point itself, so the drop-base mistake below cannot arise. Prefer it.

On a conflict it stops on that branch for `gh stack rebase --continue`. Two ways that continue fails:

- **Any unstaged tracked file** aborts it, including files you did not conflict on — regenerating proto stubs also
  rewrites `themis/services/sandbox_worker/_generated.py`. Stage everything.
- **`--preserve-dates` breaks it outright** (git exit 129: the flag is re-passed to `git rebase --continue`, which
  rejects it). Never pass it. If a rebase is already wedged that way, finish that one branch with
  `GIT_EDITOR=true git rebase --continue`, then resume the cascade with `gh stack rebase --no-trunk`.

## By hand

For a branch outside any stack, or without the extension: rebase each child onto its moved parent using the parent's
**pre-rebase tip** as the `--onto` drop-base.

```bash
git rebase --onto <parent-new-tip> <parent-OLD-tip> <child>
```

Passing the child's own base instead silently discards the child's commits. Force-push with lease, one branch at a time.
A branch carrying a merge commit needs `--rebase-merges`; a plain rebase flattens it and drops merge-only edits.

## Resolving

**Never hand-merge generated files.** Resolve the hand-authored proto, then regenerate — the stubs, the web `_pb.ts`,
and the sandbox hatch allowlist all fall out of it:

```bash
uv run --group codegen python -m tools.schema.regen
```

**A clean merge is not a correct one.** Git merges by adjacency, so it silently produces wrong code that has no markers
— observed: class-body methods landing *inside* a module-level function the other side added at the same offset (the
rpcs became unimplemented), a test double no longer satisfying a protocol the trunk widened, an allowlist and a skill
catalog each missing an rpc the trunk added. No marker scan finds any of these. Run the suite at the tip.

A fix belongs on the branch that **owns the file**, as a commit on that branch — then re-run the cascade to propagate
it. Landing it at the tip leaves every branch below it red on its own.

## Verifying

`git diff` between a branch and its parent is the wrong instrument: the base moves, so it reports noise. Compare each
branch's own commits by content instead, before and after:

```bash
git log --format=%H <parent>..<branch> | while read c; do git show $c | git patch-id --stable | cut -d' ' -f1; done
```

Equal multisets mean the branch came through untouched. Expect a difference only where you resolved a conflict.

**A commit dropped with nothing gained is usually correct**: trunk already merged it, so the rebase found it redundant.
Confirm before believing it lost — compare blob hashes for the paths it touched (`git rev-parse <branch>:<path>` against
`origin/main:<path>`); identical means nothing is missing.

Baseline any failure before blaming the restack: run the failing tests at the pre-rebase tip. Recording tips up front is
optional — `origin/<branch>` still holds them until the push, and the reflog after.

## The stack object

`gh stack link <bottom> … <top>` creates or grows a stack; it also sets every PR's base. Constraints that shape what is
possible:

- It **appends only**, and **rejects** an argument belonging to a different stack. Combining two stacks means
  `gh stack unstack <n>` on all but one, then one `link` over the whole chain.
- It **never removes** entries, so a closed PR wedged mid-stack can only be dropped by unstacking and re-linking from
  scratch — which mints a new stack number.
- Passing PR numbers pushes nothing and preserves draft state. **Never pass `--open`**: it un-drafts every PR it
  touches.
- `gh pr edit <n> --base` fails on a stack member; `gh stack modify` is an unusable-headless TUI.

Read membership without touching local state — `pullRequest` hangs off `repository`, never off the query root:

```bash
gh api graphql -f query='
  query { repository(owner: "populationgenomics", name: "themis-internal") {
    pullRequest(number: N) { stack { number size baseRefName entries(first: 40) { nodes { position
      pullRequest { number state isDraft baseRefName } } } } } } }'
```
