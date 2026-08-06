# Design: PR review screenshots

**Status:** current **Related:** [`deployment.md`](deployment.md) (IaC, state, mirror-safety posture),
[`../plans/screen-and-mirror-workflow.md`](../plans/screen-and-mirror-workflow.md) (the 1:1 public mirror this design
routes around)

## Overview

A public-read GCS bucket that agents upload review screenshots to and reference by URL in a PR body, so a
rendered-surface change can ship its before/after images without a human attaching them by hand.

## Background

`CLAUDE.md` requires a change to a rendered surface to ship with before/after screenshots in the PR description;
`apps/web` renders offline against the fixture backend, so capturing them needs no cloud access. Capture is automatable;
attachment is not. GitHub has no API to attach images — its `user-attachments` uploader is web-UI-only
(`POST /upload/policies/assets`, authenticated by a `user_session` cookie, rejects tokens with 422). So an agent can
produce the file but not attach it. This design closes that gap by hosting the image and linking it instead.

Two mechanics constrain any fix:

- **GitHub renders external markdown images through its Camo proxy** — the viewer's browser hits
  `camo.githubusercontent.com`, which fetches the origin server-side, anonymously, once, and caches it. A self-hosted
  image must therefore be readable with no credentials. Reads never come from arbitrary clients, so origin egress is one
  fetch per distinct image.
- **`main` mirrors 1:1 to the public `themis` repo.** Committing screenshots leaks them there; on a private branch they
  don't render at all, because `raw.githubusercontent.com` requires auth that Camo cannot present. Repo-hosting is a
  dead end either way (see Alternatives).

Every hosted image is fixture-backed UI (the rule excludes states reachable only against real data), so sensitivity is
low. The design still keeps objects non-discoverable as defense-in-depth, not because a leak would expose data.

## Non-goals

- **Not a store for real-data screenshots.** The `CLAUDE.md` rule already excludes states reachable only against real
  data; everything here is fixture UI. A screenshot of production data must never be uploaded.
- **Not a general asset host or CDN.** Scope is PR review screenshots. No other content type is uploaded, and the bucket
  is not a dependency of any running service.
- **Not a per-environment resource.** Screenshots are of the dev fixture UI for review, so only a stack that opts in
  gets a bucket. Unlike state or app buckets, there is no prod counterpart to keep in step.
- **Reads are not restricted to GitHub.** Camo's egress IPs are neither published nor stable, so a network ACL is not an
  option. Confidentiality is object-name unguessability plus non-listability, not a firewall.

## Design

A single bucket `cpg-themis-dev-pr-screenshots`, public-read-by-URL but not listable, provisioned in the `dev` stack by
[`../../infra/themis_infra/screenshots.py`](../../infra/themis_infra/screenshots.py): uniform bucket-level access,
Autoclass tiering cold objects to Archive, no lifecycle rules, and the one `public_access_prevention='inherited'` in a
project where every other bucket enforces it. The bucket depends on neither `constraints/storage.publicAccessPrevention`
nor `constraints/iam.allowedPolicyMemberDomains` (domain-restricted sharing) being enforced above the project; should
either ever be, `pulumi up` fails loud on the `allUsers` binding, so nothing needs to pre-flight them.

A stack opts in through `themis:enablePrScreenshotBucket`, which the program reads with `require_bool`. The flag is not
a convenience: it is what confines the project's only public bucket to an environment that has a reason for one, and
requiring it means a new stack decides rather than inheriting a public bucket by omission.

**IAM.** Two `BucketIAMMember` bindings:

- `allUsers` → `roles/storage.legacyObjectReader`, whose whole permission set is `storage.objects.get`. The stock public
  recipe `allUsers → roles/storage.objectViewer` also grants `storage.objects.list`, which would make the bucket
  publicly *listable*. Get without list gives read-by-URL with no way to enumerate.
- `group:themis-dev-access@populationgenomics.org.au` → `roles/storage.objectAdmin`, which covers browsing the bucket,
  uploading, and deleting. `objectAdmin` rather than `objectCreator` because retraction is the safety valve this bucket
  uniquely needs: a capture that turns out to expose something is world-readable at a permanent URL, and the population
  that can publish has to be able to withdraw without escalating to a project owner. Soft delete is off
  (`retention_duration_seconds=0`) so that withdrawal takes effect at once — GCS's default 7-day window cannot be
  overridden, and would keep a retracted object retrievable throughout. Camo may go on serving a cached copy for a while
  after the object is gone, so a takedown is delete, edit the PR body, and accept that tail.

The writer is the developer's own `gcloud` ADC — a member of `themis-dev-access` — so the local agent uploads with the
credentials it already has; no service account is minted for this.

Reads are anonymous and uncapped: anyone holding a URL can fetch it at the project's expense, and no budget alert
watches it. Accepted unmonitored — the objects are small, Camo fetches each distinct image once, and Autoclass charges
no retrieval fee on a tiered-down object.

**Object model.** Content-addressed: the object name is `<sha256-hex>.png`. This dedupes identical captures, makes
objects immutable (the name is the content), and makes them unguessable (256-bit name) so get-only public access
discloses nothing to someone without the URL. No PR or branch prefixing — the PR body carries the links; the bucket is a
flat content store.

Uploads set `Content-Type: image/png` (so Camo and browsers render inline rather than download) and
`Cache-Control: public, max-age=31536000, immutable` (safe because the name is a content hash). Objects are kept
forever: PRs are permanent records, a TTL would rot their images once Camo's cache evicts, and the data is tiny.

**Upload path.** [`../../tools/screenshot/upload.py`](../../tools/screenshot/upload.py) hashes each file, uploads it
with that content-type and cache-control conditional on the object not existing, and prints the markdown link — so the
agent's step stays a one-liner and the hashing convention lives in one place:

```
$ uv run --group screenshot python -m tools.screenshot.upload after.png
![after](https://storage.googleapis.com/cpg-themis-dev-pr-screenshots/<sha256>.png)
```

Stdout carries only the `![…](…)` lines, one per argument in order, so all of it pastes into the PR body; notes go to
stderr. A file that is not a PNG is refused rather than stored under a content-type that would break its rendering.
Uploading identical bytes twice is a no-op — the conditional write's 412 means that hash is already stored, which is the
dedupe, not a conflict.

**`CLAUDE.md` rule.** The screenshot rule's attachment clause is this upload path. Its one escape hatch is the
not-capturable state the rule already calls out — a surface reachable only against real data — where there is no offline
capture to host in the first place.

This doc, like [`deployment.md`](deployment.md), is mirror-safe: it names identifiers (project, bucket, group, public
URLs), never credentials or participant data.

## Alternatives considered

- **GitHub-native attachment via browser automation** (gh-attach / Playwright / the `claude-in-chrome` MCP). GitHub
  hosts the image and, since the 2023 "more secure private attachments" change, access-controls it to repo members —
  better confidentiality and zero infrastructure. Rejected as primary: it drives an undocumented web-UI endpoint that
  takes a `user_session` cookie and no token, so it needs a maintained logged-in browser session, is local-only, and
  breaks silently when GitHub changes the form. The repo's posture favors robust, boring infrastructure — it already
  shed MCP for code mode to drop exactly this kind of fragile coupling. A content-addressed object write does not rot.
  Kept as the fallback should the public bucket ever have to go, an org-policy change being the likeliest cause.
- **Commit screenshots to the repo (plain, git-LFS, or an orphan branch).** Rejected: on `main` they mirror to the
  public repo (leak); on a private branch they don't render, because `raw.githubusercontent.com` needs auth Camo cannot
  present. Also permanent history/repo bloat.
- **Third-party image host (Imgur and similar).** Rejected: sends fixture UI to an uncontrolled third party under its
  terms and retention, with no access control and eventual link rot.
- **A project custom role holding `storage.objects.get`** instead of `roles/storage.legacyObjectReader`. Identical
  permission set, so no security difference, and rejected on lifecycle: creating one needs `iam.roles.create`, which no
  role the CI deploy SA holds carries, so it would fail the deploy while `pulumi preview` — run as an identity with
  `roles/viewer`, which can read roles — still rendered a clean plan. Custom-role deletion is also soft, reserving the
  id for 7 to 37 days, which would wedge a destroy-then-recreate of the stack.
- **Keep the human in the loop.** Rejected: an agent can capture but not attach, so every rendered-surface PR stalls on
  a manual step. Survives only for the not-capturable state.

## Implementation state

Shipped (#296): the bucket and its two bindings; the upload helper; the `CLAUDE.md` rule pointing at it.
