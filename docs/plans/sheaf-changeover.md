# Plan: the changeover from the tar workspace to a sheaf repository

**Related:** [`../design/sheaf.md`](../design/sheaf.md) (the storage layer this switches the sandbox onto);
[`../design/sandbox-worker.md`](../design/sandbox-worker.md) (the worker whose restore and checkpoint this replaces);
[`../design/workspace-model.md`](../design/workspace-model.md) (what a workspace is for);
`agents/sandbox-probe.agent.yaml` (the prompt this changes).

## Context

The sandbox worker restores `/workspace` from two store rpcs — the working document, versioned in its own bucket, and a
tar of everything else — and checkpoints both after every `shell` call and once more at session end. All of it runs in
the trusted worker through postern's reference-closed accessor. The BFF reads working-document versions straight from
the bucket. The agent is told that `/workspace/working_document.md` is its deliverable and everything else is scratch
discarded between sessions.

`themis.sheaf` is complete and nothing imports it. postern 0.4.0 has the stream hatch: one Unix socket per service, the
guest's bytes reaching nothing but a fixed subprocess's stdin, with git's `ext::` transport as the case it was built
for.

This plan switches the sandbox's workspace to a sheaf repository per Analysis. Existing tar workspaces are not migrated:
an Analysis whose session runs under the new worker starts from an empty repository, and its old scratch is gone. That
is accepted.

## Constraints

**Every git command that touches `/workspace` runs inside the guest.** The guest owns `/workspace`, `.git` included, so
a `git` the trusted worker ran there would execute whatever `core.hooksPath`, `core.fsmonitor` or a clean filter pointed
at, in the process holding the credential. This includes hydration: the clone is a guest command over the hatch, not a
host-side copy. The one host-side git is the mirror's — a bare repository at a host-only path that the guest never sees,
driven through `themis.sheaf.wire.bare.BareRepo` and the hook.

Enforcement, in two layers. The structural one is postern's `host_uid`: mapped to a dedicated uid rather than the
worker's root, every file the guest creates is owned by a uid that owns nothing else on the host, and git refuses to
operate on a repository owned by another user (`fatal: detected dubious ownership`, root not exempt) — so an accidental
host-side `git` in `/workspace` fails, and the only override is a `safe.directory` entry a reviewer would see. Its cost
is that the SDK's file tools write as the worker, so a file they create is not the guest's to modify in place unless the
worker makes it so; that interaction is the first thing to settle in step 1. The conventional layer is a test over the
worker package asserting that its only `git` invocations are `Sandbox.run` (guest) and `BareRepo.git` (mirror).

**The agent controls its snapshots.** The worker makes no commits. What the agent has not committed when the session
ends is gone, and it is told so. The worker's one contribution is at teardown: a guest-side `git push origin --all`, so
anything committed and not yet pushed survives. If that push is refused because the store moved, the tip is pushed to a
fresh `refs/stranded/<session-id>` instead — creating a ref is always allowed — so the work survives for a later session
to merge rather than dying with the container.

**History is append-only**, which the hook enforces ([`sheaf.md`](../design/sheaf.md)). The prompt says so, in git's own
terms: no force-push, no branch deletion; `pull --rebase` then push is the recovery.

## Target shape

```mermaid
sequenceDiagram
    participant W as worker (host)
    participant M as mirror (host-only path)
    participant G as guest
    participant S as Sheaf service
    W->>S: BareRepo.sync(): ReadRefDoc, FetchPack (session token)
    W->>G: sandbox.run(git clone ext::… /workspace)
    G->>M: upload-pack over the hatch
    Note over G: the agent works, commits, pushes
    G->>M: receive-pack over the hatch → pre-receive hook
    M->>S: hook publishes: Publish (packs, reflog entry, compare-and-swap)
    W->>G: teardown: sandbox.run(git push origin --all)
```

- **One repository per Analysis**, reached through the `Sheaf` service
  ([`../design/sheaf-service.md`](../design/sheaf-service.md)), which holds the bucket credential and scopes every call
  by the session token it carries. The worker's mirror runs over the service's client, and the session token is the
  worker's only credential for the repository: it reaches the mirror's hook by a file under the mirror's host-only root,
  never through an environment or the sync state, and the worker learns no Analysis id.
- **Two hatches**, `upload-pack` and `receive-pack`, each splicing to git against the mirror. The handler syncs the
  mirror before handing the connection over, under the per-repository lock the HTTP server already uses, and passes the
  hook its environment explicitly — postern scrubs the subprocess env by default. `origin` gets the upload-pack socket
  as its fetch URL and the receive-pack socket as `remote.origin.pushurl`, which is how one remote reaches two
  single-service sockets. `SheafGitServer` stays for a writer that is not sandboxed; it is not in this path.
- **Restore is fail-closed.** The repository is the deliverable; a hydrate that fails fails the spawn, as the working
  document does today. An Analysis with no repository yet clones an empty one.
- **A new repository's first commit is the worker's, made in the guest**: `.gitignore` naming `scratch/` and `skills/`,
  pushed before the agent runs. Not the agent's job, and not the prompt's.
- **Protected paths** `scratch/**` and `skills/**` on the hook. The `.gitignore` is a writable convenience that keeps
  `git status` honest; the refusal that matters is the hook's, which the guest cannot reach. Locking the file would not
  help — the guest owns the directory and `git add -f` ignores it — and is not needed.
- **The guest rootfs** gains `git`, a system gitconfig with `protocol.ext.allow=always` and the agent's identity as
  `user.name`/`user.email`, and nothing else.
- **Compaction is not part of this.** Who runs it, and when, stays an open question in [`sheaf.md`](../design/sheaf.md);
  nothing here depends on the answer, and the worker's contract does not grow one.

## Steps

1. **Uid mapping.** Set `host_uid` to a dedicated uid in the worker's deploy and confirm the SDK file tools and guest
   `shell` still cooperate on the same files. Land or reject the structural guard on the result; the test-based guard
   lands regardless. Settled as [below](#the-uid-mapping-is-built-and-not-yet-enabled): built, proven under test, off in
   the deploy until the SDK's file tools hand ownership down.
1. **Hatches.** A `git_hatches(mirror)` in the worker: two `StreamHatch`es over the mirror, sync-before-splice, hook env
   passed through. Tested offline with a `LocalBackend`, a real guest where bubblewrap is present and the existing
   fixture path where it is not.
1. **Restore and teardown.** Replace `WorkspaceSync.restore` with the guest-side clone (plus the first commit for an
   empty repository), and its scratch checkpoint with the teardown push and the stranded-ref fallback. The working
   document moves into the repository; its rpc checkpoint stays for the BFF's sake (below).
1. **Guest rootfs and prompt.** `git` and the gitconfig in the guest stage of the Dockerfile; prompt v2 (below) on the
   probe agent, created fresh so a running Analysis keeps the prompt it started with.
1. **Validate in dev** with the probe agent: clone, commit, push, a refused force-push and its recovery, a teardown with
   unpushed commits, a second session finding the first's work.
1. **Retire the tar rpcs.** `PutWorkspace`/`GetWorkspace` go dead at step 3; removing them from `store.proto` is an
   interface change and its own PR.

### The uid mapping is built and not yet enabled

Everything the structural guard needs is here — `host_uid`/`host_gid` on the profile, a socket directory the mapped uid
can traverse, and `sandbox_root` tests that clone, commit and push from the dropped guest and then watch a host-side
`git status` refuse the result — but the deploy sets neither uid variable, so the guard is off in production and the
test over the worker package is what holds. That is a deliberate pause, not an oversight, and it lasts until the file
tools change.

The obstacle is the mechanism itself, pointed the other way. Ownership is what makes a host-side `git` refuse the
guest's repository; it equally makes a worker-authored file refuse the guest. The SDK's `write` and `edit` tools run
trusted-side, so under the mapping they create files the worker owns, mode 0644, in a sticky `/workspace`: the guest can
neither rewrite such a file in place nor unlink and recreate it, and a `git checkout` or `git pull --rebase` that has to
update one fails. Two independent blockers, mode and the sticky bit, so relaxing either alone changes nothing.

Three ways out, and the reason for the choice:

- **Chown each written path down to the guest.** The file tools stay trusted-side; after a successful `write` or `edit`
  the worker hands the file's ownership to the guest's uid through the confined accessor. Contained to the worker, turns
  the `xfail` that records the cost into a test that pins the fix, and leaves everything the SDK writes outside those
  tools — the skills download — read-only to the guest, which is what it should be.
- **Run the file tools in the guest.** Ownership then holds by construction, but it reverses the worker's premise that
  only arbitrary execution is sandboxed, and it means guest-side implementations of read, write, edit, glob and grep.
- **Change what postern does with a mapped workspace** — no sticky bit, group-writable under a shared gid. It touches
  the dependency, and it weakens the protection the sticky bit provides where no uid is mapped.

The first is the plan. Until it lands the deploy stays unmapped, and `sandbox-worker.md` says which layer is live.

### The working document is in the repository; its checkpoint stays until the BFF reads from git

`/workspace/working_document.md` is an ordinary tracked file: the agent commits and pushes it like anything else, and
the repository's history is the document's. Git is the better liveness signal — a commit says what changed and when,
alongside the rest of the work — and the frontend reads the document from the repository next. Until it does, the BFF
still reads working-document versions from the store's bucket, so the worker keeps the existing rpc checkpoint as a copy
of the tree's file: written when a sandboxed command returns and once more at teardown, and written into the tree at
restore only to seed a repository that has no document yet — an untracked file for the agent to commit. Where the
repository tracks the document, its copy wins and the checkpoint is not written over it, in either direction: a store
that has fallen behind the repository catches up on the first command, and a document edited after the session's last
commit is what the reviewer saw but not what the next session starts from — the agent is told to commit it. Two write
paths for the one artifact the curator sees is a state to pass through, not to settle in, and the two PRs that end it
are this one's immediate successors, not later work: first the read path the BFF needs — the document at a commit, and
history — as the Sheaf service's second interface ([`../design/sheaf-service.md`](../design/sheaf-service.md), "The
second consumer"), with the BFF reading versions from it; then the retirement of the checkpoint and of
`GetWorkingDocument`/`PutWorkingDocument` from `store.proto`, an interface change of its own that can take the tar rpcs
of step 6 with it.

## The prompt

What changes in `agents/sandbox-probe.agent.yaml`, at the level of what the agent is told:

- `/workspace` is a git clone of this Analysis's repository; `origin` is the store. Commit your work and push it. What
  is not pushed when the session ends is lost — the worker pushes commits you made and forgot to push, and nothing else.
- History here is append-only. A push is refused if it would rewrite or delete anything; `git pull --rebase` and push
  again. Do not force-push; it will not work.
- `scratch/` is yours and ignored; `skills/` is the platform's. Neither can be pushed.
- `refs/sheaf/reflog` is the record of what each ref pointed at and when. Read it if you need it; you cannot write it.
- The working document is `/workspace/working_document.md`, a file in the repository: commit and push it with the rest.
  It is also read back after each command, so its latest content reaches the reviewer before it is committed.

## Open decisions

- **Stranded refs.** `refs/stranded/<session-id>` preserves refused teardown pushes at the cost of a namespace to
  explain and, eventually, to tidy. The alternative is to log and lose. This plan takes the ref.
- **Whether the guest keeps `git` between spawns.** It does not: the rootfs is rebuilt from the image, so this is a
  build concern only.
