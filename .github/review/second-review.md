### Concern: second review

Design docs, interface changes, and database schema changes get their own PR and another maintainer's review
([`docs/design/review-policy.md`](docs/design/review-policy.md)). The machinery only holds the merge for a review that
was *requested* — it cannot notice the PR the author didn't realise qualifies. You are that notice.

Second-review content is:

- a design doc: anything under `docs/design/`
- an interface change: an authored proto contract (`schema/proto/`), or a data-contract doc
- a database schema change: a migration (`themis/migrate/migrations/`)

Flag either of the following. Both are T2 by construction — once the PR merges, the review this concern exists to prompt
can no longer gate anything:

1. **Second-review content with no reviewer engaged.** The diff carries second-review content, and no review has been
   requested or given. Check `gh pr view <n> --json reviewRequests,reviews` *immediately before posting*, not at the
   start of your run — the author may request a reviewer while you work. A review counts as given when submitted by
   someone other than the PR author and other than you (`claude`); a pending request counts as engaged.
1. **Implementation mixed beyond the interface PR's bounds.** The diff carries second-review content plus implementation
   beyond what the policy's "What the interface PR can hold is bounded" section allows. The bounds are not mixing: a
   retirement's kept-in-place retiring field and `Partial<ServiceImpl<T>>` widening, an addition shipping with the
   surfaces an exhaustiveness test binds it to, a migration's `deploy.yml` substitution, generated stubs and their lint
   exclusions — the minimal coupling that keeps the split green. Substantive behaviour change riding along is.

Anchor the comment to the second-review content itself (the migration file, the proto hunk, the design-doc change).
Remediation, in preference order: request the other maintainer's review (`gh pr edit <n> --add-reviewer <login>`;
canonically done after the author's read, before the draft goes ready); split the PR along the policy's bounds; or — if
the author judges the content out of scope for a second review — resolve the thread with the reason. Thread resolution
is required to merge, so the finding holds the merge exactly until one of those happens.

Do not flag a PR whose second-review content is the *whole* PR and which already has a reviewer engaged — that is the
policy working. This concern has no severity scale and no security framing; it is a scope reminder.
