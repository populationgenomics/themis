# Design: PR review screenshots

**Status:** draft **Related:** [`deployment.md`](deployment.md) (IaC, state, mirror-safety posture),
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
produce the file but not attach it, and the rule falls back to handing the path to a human. This design closes that gap.

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
- **Dev-only; no per-environment variant.** Screenshots are of the dev fixture UI for review. Unlike state or app
  buckets, there is no prod counterpart — the resource exists only in the `dev` stack.
- **Reads are not restricted to GitHub.** Camo's egress IPs are neither published nor stable, so a network ACL is not an
  option. Confidentiality is object-name unguessability plus non-listability, not a firewall.

## Design

A single bucket `cpg-themis-dev-pr-screenshots`, public-read-by-URL but not listable, provisioned in the `dev` stack
under `infra/themis_infra/`. It is the one bucket in the project that opts out of the repo's default
`public_access_prevention`.

```python
bucket = gcp.storage.Bucket(
    'themis-pr-screenshots',
    project=project,
    name=f'{project}-pr-screenshots',
    location=region,
    uniform_bucket_level_access=True,
    # The only bucket that allows public access: Camo fetches the origin anonymously.
    public_access_prevention='inherited',
    # Keep-forever, rarely re-fetched: let Autoclass tier cold objects down to Archive.
    # No storage-class-transition lifecycle rules, which would invalidate Autoclass.
    autoclass=gcp.storage.BucketAutoclassArgs(enabled=True, terminal_storage_class='ARCHIVE'),
    opts=opts,
)
```

**IAM.** Three bindings, all `BucketIAMMember`:

- `allUsers` → a custom role holding **only `storage.objects.get`**. This is the load-bearing detail: the stock public
  recipe `allUsers → roles/storage.objectViewer` also grants `storage.objects.list`, making the bucket publicly
  *listable*. A get-only role gives read-by-URL with no way to enumerate objects.
- `group:themis-dev-access@populationgenomics.org.au` → `roles/storage.objectViewer` (get + list, so the team can browse
  and audit) and `roles/storage.objectCreator` (create). Create-only, not `objectAdmin`: objects are immutable once
  written; deletion is a rare console/admin action, not a routine grant.

```python
reader = gcp.projects.IAMCustomRole(
    'themis-pr-screenshots-public-reader',
    role_id='themisPrScreenshotsPublicReader',
    project=project,
    title='PR screenshots public read (get, no list)',
    permissions=['storage.objects.get'],
    opts=opts,
)
gcp.storage.BucketIAMMember(
    'themis-pr-screenshots-public-read',
    bucket=bucket.name,
    role=reader.name,               # projects/<project>/roles/themisPrScreenshotsPublicReader
    member='allUsers',
    opts=opts,
)
for role in ('roles/storage.objectViewer', 'roles/storage.objectCreator'):
    gcp.storage.BucketIAMMember(
        f'themis-pr-screenshots-team-{role.rsplit(".", 1)[-1].lower()}',
        bucket=bucket.name,
        role=role,
        member=f'group:{iap_access_group}',   # themis-dev-access@populationgenomics.org.au
        opts=opts,
    )
```

The writer is the developer's own `gcloud` ADC — a member of `themis-dev-access` — so the local agent uploads with the
credentials it already has; no service account is minted for this.

**Object model.** Content-addressed: the object name is `<sha256-hex>.png`. This dedupes identical captures, makes
objects immutable (the name is the content), and makes them unguessable (256-bit name) so get-only public access
discloses nothing to someone without the URL. No PR or branch prefixing — the PR body carries the links; the bucket is a
flat content store.

Uploads set `Content-Type: image/png` (so Camo and browsers render inline rather than download) and
`Cache-Control: public, max-age=31536000, immutable` (safe because the name is a content hash). Objects are kept
forever: PRs are permanent records, a TTL would rot their images once Camo's cache evicts, and the data is tiny.

**Upload path.** A helper under `tools/` hashes the file, uploads with the right content-type and cache-control and no
clobber, and prints the markdown link — so the agent's step stays a one-liner and the hashing convention lives in one
place:

```
$ tools/screenshot/upload.py after.png
![after](https://storage.googleapis.com/cpg-themis-dev-pr-screenshots/<sha256>.png)
```

The public URL is `https://storage.googleapis.com/cpg-themis-dev-pr-screenshots/<sha256>.png`. The agent pastes the
printed `![…](…)` lines into the PR body per the `CLAUDE.md` before/after rule.

**`CLAUDE.md` rule.** The screenshot rule ships in this PR with its human-attach clause intact — the bucket does not
exist yet. Provisioning it flips that final clause to the upload path; the human-attaches case then survives only for
the not-capturable state the rule already calls out.

This doc, like [`deployment.md`](deployment.md), is mirror-safe: it names identifiers (project, bucket, group, public
URLs), never credentials or participant data.

## Alternatives considered

- **GitHub-native attachment via browser automation** (gh-attach / Playwright / the `claude-in-chrome` MCP). GitHub
  hosts the image and, since the 2023 "more secure private attachments" change, access-controls it to repo members —
  better confidentiality and zero infrastructure. Rejected as primary: it drives an undocumented web-UI endpoint that
  takes a `user_session` cookie and no token, so it needs a maintained logged-in browser session, is local-only, and
  breaks silently when GitHub changes the form. The repo's posture favors robust, boring infrastructure — it already
  shed MCP for code mode to drop exactly this kind of fragile coupling. A `gcloud storage cp` one-liner does not rot.
  Kept as the fallback if the org-policy gate below rejects a public bucket.
- **Commit screenshots to the repo (plain, git-LFS, or an orphan branch).** Rejected: on `main` they mirror to the
  public repo (leak); on a private branch they don't render, because `raw.githubusercontent.com` needs auth Camo cannot
  present. Also permanent history/repo bloat.
- **Third-party image host (Imgur and similar).** Rejected: sends fixture UI to an uncontrolled third party under its
  terms and retention, with no access control and eventual link rot.
- **Keep the human in the loop (`CLAUDE.md` as-is).** This is the status quo the design removes, retained only as the
  not-capturable fallback.

## Implementation state

The `CLAUDE.md` screenshot rule and this doc ship together; the bucket and upload helper are not yet built.

- **infra** — the bucket, custom get-only role, and IAM bindings above, in a new `infra/themis_infra/` module wired into
  the `dev` stack. (Editing `infra/` triggers the Pulumi-skill rule in `.claude/rules/pulumi.md`.)
- **tooling** — `tools/screenshot/upload.py` (hash, upload with content-type + cache-control + no-clobber, print the
  link).
- **rule flip** — once the bucket and helper exist, rewrite the rule's attachment clause from human-attach to the upload
  path, and flip this doc's Status to `current`.

## Open questions

- **Org-policy gate.** Every other bucket in the project sets `public_access_prevention='enforced'`; this is the first
  public one. If `constraints/storage.publicAccessPrevention` or `constraints/iam.allowedPolicyMemberDomains`
  (domain-restricted sharing) is enforced above the project, `pulumi up` fails loud on the `allUsers` binding or the
  `inherited` setting — no pre-flight API check is needed. If it rejects, fall back to browser-native attachment or
  request an org-policy exception.
