# The sheaf service

**Related:** [`sheaf.md`](sheaf.md) (the storage model: packs, the ref document, append-only history, the reflog);
[`sandbox-worker.md`](sandbox-worker.md) (the worker whose mirror and hook call this service);
[`services.md`](services.md) (how a data-plane service is built and called).

The `Sheaf` gRPC service is how a sheaf repository is read and written. It holds the only credential on the workspace
bucket; a caller holds the objects — a bare git mirror — and drives the protocol through the rpcs below. The first
caller is the sandbox worker: its mirror hydrates through `ReadRefDoc` and `FetchPack`, and its pre-receive hook
publishes through `Publish`. The guest's `git` sees none of this: it speaks git's own protocol to the worker's mirror
over the stream hatch, and the mirror is where git's world ends and sheaf's begins. The curator's browser is the second
caller: it keeps a copy of the repository of its own, hydrated and published through the same rpcs, which the BFF
relays, and it downloads packs by URLs this service signs ([below](#the-second-caller)). The protocol lives in the
service rather than in each caller so that the checks a repository's document can decide are made once, for every
writer, and so that no caller needs a bucket credential of its own. Which deployment hosts the service is not part of
the contract; every rpc is scoped by the session it carries, not by where it runs.

Contract: [`sheaf.proto`](../../schema/proto/themis/rpc/sheaf.proto). Stubs: `themis.rpc.sheaf_pb2`,
`themis.rpc.sheaf_pb2_grpc`.

## Scope of a call

Every rpc is gated by the auth interceptor ([`rpc-authorization.md`](rpc-authorization.md)): the caller is verified from
the ID token Cloud Run forwards, its claim names the session it is calling within, and the contract admits two
principals: the sandbox job's account calling as the worker within a session, and the web tier naming the Analysis it
acts on through a session it derives, on the rpcs the browser reaches (§"The second caller"). As on every rpc, the
developer identity is admitted too. The repository every rpc acts on is the Analysis that session resolves to — its ref
document and packs under the Analysis's key prefix in sheaf's bucket. No request names a repository, so a caller cannot
reach another Analysis's; a developer's call, admitted without a session, names one in its claim for the same reason,
and one that names none is `INVALID_ARGUMENT`, since there is nothing to serve.

The service never sends a status itself: every refusal is raised as the status it maps to, and the gate ends the call
with it ([`rpc-authorization.md`](rpc-authorization.md), "Default-deny is enforced at the interceptor"). For `Publish`
that means a refusal decided from the intent is sent at once, and the packs the client had left to send are never read.

## The rpcs

### `ReadRefDoc`

`google.protobuf.Empty` → `RefDocSnapshot`

Returns the repository's ref document and the generation that wrote it.

- `document` is the `RefDoc` message as stored — refs, the pack manifest, HEAD — carried as a message so a field this
  build does not model survives the round trip. Unset when the repository does not exist yet.
- `generation` is the bucket's version token for the document: the value the object had when read, and the value a
  conditional write is made against. It is opaque — a GCS generation is a microsecond timestamp, neither dense nor
  ordered in any way a caller may rely on — and is compared for equality only ([`sheaf.md`](sheaf.md), Consequences).
  Zero exactly when `document` is unset.

A repository that does not exist is not an error. A publish against `generation = 0` asserts that it still does not.

Errors: `DATA_LOSS` if the stored document does not parse as one this code wrote — a truncated or foreign encoding. That
is damage, and a caller treats it as terminal, not as a fault to retry.

### `FetchPack`

`FetchPackRequest{pack_id}` → `stream PackChunk`

Streams the bytes of one pack the document names, in order. `pack_id` is the lowercase hex SHA-256 of the pack's bytes,
as the document's `packs` lists it, so a caller can verify what it receives.

Errors: `INVALID_ARGUMENT` if `pack_id` is not sixty-four hex digits, before the store is consulted — the id becomes
part of an object key, so its form is the contract's to fix. `NOT_FOUND` if no such pack. Nothing in a sheaf store is
ever deleted, so a pack the current document names is always present; `NOT_FOUND` means the id did not come from a
document this service returned.

### `Publish`

`stream PublishRequest` → `PublishResponse`

Uploads a publish's packs and replaces the ref document in one compare-and-swap.

The stream is one `PublishIntent` first, then the bytes of each pack the intent declared, in order — a stream of the
intent alone is a complete publish when it declares no packs:

- `PublishIntent.base_generation` — the generation of the `RefDocSnapshot` the caller built against. Zero asserts the
  repository does not exist yet.
- `PublishIntent.ref_updates` — ref name → `RefUpdate{old, new}`. `old` absent requires the ref not to exist; `new`
  absent is a deletion, which is refused by name, as is git's zero object id. Names are fully qualified under `refs/`. A
  publish that moves any ref outside `refs/sheaf/` must also move `refs/sheaf/reflog`, the ref sheaf keeps as its own
  record of which commit was each ref's tip and when — one commit per publish, parented on the previous entry and every
  tip the publish set, so every commit ever a tip stays reachable ([`sheaf.md`](sheaf.md), "The reflog ref says what was
  current"). The service can check that an entry is present; what it points at is the caller's contract. A publish that
  moves no ref outside `refs/sheaf/` is refused: nothing legitimate publishes only sheaf's own bookkeeping, and the
  classification below is over exactly those refs. With none of them in the intent, the classification's receipt check
  ("every such ref already holds its `new`") would hold trivially, and a publish that lost a race would be reported as
  landed.
- `PublishIntent.head` — optional `RefTarget`. Unset carries the document's HEAD over, except an unborn one — a symbolic
  HEAD naming a branch that does not exist — once the publish leaves a branch, so a clone lands on a branch rather than
  on nothing. Re-derivation, on a first publish and in that case, is one rule: `refs/heads/main` if it exists after the
  publish, else the first branch in name order, else an unborn `refs/heads/main`.
- `PublishIntent.packs` — one `PackDescriptor{size, pack_id}` per pack the publish carries, in the order their chunks
  follow: the pack's byte length, and the SHA-256 of those bytes that becomes its id — sixty-four hex digits, refused
  before any byte is read if anything else, since it becomes part of an object key. Declared up front so a stream that
  delivers the wrong number of bytes for a pack — a client that half-closed after a short read of the quarantine — is
  refused rather than stored. A truncated pack the manifest names would be the one damage this store cannot undo, since
  nothing is ever deleted and `Publish` only extends the manifest.
- `PublishChunk.pack` — which declared pack the bytes belong to. Chunks of one pack arrive in order and packs are
  contiguous — all of pack *n* before any of pack *n+1* — so the service hashes, checks and stores one pack at a time.
  Every pack is self-contained (not `--thin`).

The service validates the intent — including that the declared packs fit the deployment's per-publish byte ceiling,
refused with `RESOURCE_EXHAUSTED` before any byte is stored — then checks and uploads each pack under its declared hash,
and compare-and-swaps the document at `base_generation`. Packs land before the document names them, so a refusal at any
point leaves at most an unreferenced pack and never a ref that points at an object nothing can fetch.

Response: `generation`, the value at which the document holds this publish's outcome — the one the publish wrote, or the
current one when the call succeeded because the publish had already landed — which the caller's next `ReadRefDoc`
returns. Nothing else: the packs stored are exactly the declared descriptors, so the caller already knows their ids.

Errors, and what each means to the caller:

| Code                  | Cause                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | The caller's move                                         |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| `ABORTED`             | The document is no longer at `base_generation`, and every ref the intent moves outside `refs/sheaf/` still holds its `old` in the current document: an unrelated publish landed first.                                                                                                                                                                                                                                                                                                                                                                                                                                                        | Read again, rebuild the intent against it, publish again. |
| `FAILED_PRECONDITION` | The document is no longer at `base_generation`, and a ref the intent moves outside `refs/sheaf/` has moved: the caller's view of that ref is behind.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | A non-fast-forward: merge or rebase, then push again.     |
| `INVALID_ARGUMENT`    | A ref name or object id git cannot hold; two names that collide as a directory and a file; an update with no `new` or with the zero id (a deletion); an intent moving no ref outside `refs/sheaf/`; a declared pack id that is not sixty-four hex digits, or declared twice, or declared with no bytes; a pack whose bytes do not match its declared size or hash; a chunk carrying no bytes; a HEAD naming neither an object nor a ref; a publish that moves a ref outside `refs/sheaf/` without moving `refs/sheaf/reflog`; a malformed stream — no intent, an intent after a chunk, a pack index out of order or beyond the declared list. | Fix the intent; retrying the same one never lands.        |
| `INVALID_ARGUMENT`    | The document is still at `base_generation`, and a ref the intent moves does not hold its `old` there: the intent disagrees with the document it claims to have been built from — a caller's bug, not a race, so it is not classified as one.                                                                                                                                                                                                                                                                                                                                                                                                  | Rebuild the intent from the document as read.             |
| `INVALID_ARGUMENT`    | An admitted call whose claim names no session: no request names a repository, so there is nothing to serve.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Claim the session whose repository is meant.              |
| `DATA_LOSS`           | The stored document does not parse as one this code wrote: a truncated or foreign encoding.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Stop: this is damage, not a fault to retry.               |
| `UNAUTHENTICATED`     | No ID token the service can verify — the gate's denial, before anything is read.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | —                                                         |
| `PERMISSION_DENIED`   | The caller is not one the contract admits, or the session its claim names does not resolve.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | —                                                         |
| `INTERNAL`            | The gate could not honour the caller's claim: none from an account that never calls as itself, one naming a session with no token, or one that does not decode. The details name which.                                                                                                                                                                                                                                                                                                                                                                                                                                                       | The caller is misbuilt or out of date; redeploy it.       |

`RESOURCE_EXHAUSTED` is the size refusal: the declared packs exceed the deployment's per-publish byte ceiling, or the
ref set the publish would leave exceeds its ref-count or document-size ceiling — both decided from the intent and the
current document before any pack is stored, and the second for the same reason as the first: nothing is deleted, so the
ref set only grows. Faults are gRPC's ordinary codes and are not refusals: `UNAVAILABLE` or `INTERNAL` for a storage
fault — its details name the storage error, where the gate's `INTERNAL` above names the claim — which a caller retries
under gRPC's usual semantics and the worker's hook reports as a deployment fault rather than the pusher's.

A moved document is classified in a fixed order, from one read of the current document, over the refs the intent moves
outside `refs/sheaf/` — the reflog ref advances on every publish, so it can never still hold its `old` after a race and
is bookkeeping the service checks for presence, not an input to the split. First the receipt: if every such ref already
holds its `new`, this publish landed and only its response was lost, so the call succeeds with the current generation
and a retry completes. Then the split: every such ref still at its `old` is `ABORTED`; one that moved is
`FAILED_PRECONDITION`. Those are the two rejections [`sheaf.md`](sheaf.md) keeps distinct: the first is replayable and
the second is semantic. The classification is the service's to do. Sheaf's in-process store — `Store.publish` in
[`themis/sheaf/store.py`](../../themis/sheaf/store.py), which this rpc calls — compares `old` against the caller's own
base snapshot. A caller that derives its intent from that snapshot can never fail that comparison, so without a re-read
every moved generation would be `ABORTED`, and the split between `ABORTED` and `FAILED_PRECONDITION` would exist only in
the table above. The service therefore re-reads the current document to make the split. The worker's hook turns the
first into git's "the remote moved, pull first" and the second into git's own non-fast-forward message.

## What the service checks, and what the caller does

The service holds the document and no objects, so it decides what the document alone decides:

- every ref name is fully qualified and one git can hold;
- every object id is a SHA-1, and not the zero id;
- every declared pack arrives with exactly its declared size and hashes to its declared id;
- no two names in the resulting ref set collide as a directory and a file;
- no update deletes a ref;
- a publish moving any ref outside `refs/sheaf/` also moves `refs/sheaf/reflog`;
- `base_generation` is the document's current generation — and when it is not, whether every ref the intent moves
  outside `refs/sheaf/` already holds its `new` (the publish landed: success), still holds its `old` (`ABORTED`), or has
  moved (`FAILED_PRECONDITION`);
- the ref set the publish would leave is within the deployment's ref-count and document-size ceilings.

The caller, which holds the objects, is responsible for:

- every move being a fast-forward (the worker's hook checks with `git merge-base --is-ancestor`; a writer that builds
  its commit on the tip it read satisfies it by construction);
- the reflog entry's parents being the previous entry and every tip the publish sets;
- the packs making every new tip reachable.

## Flows

Hydration, at session start. The worker syncs its mirror from the service; the guest then clones the mirror over the
hatch, and never reaches the service or the bucket.

```mermaid
sequenceDiagram
    participant G as guest git
    participant W as worker (mirror + hatch)
    participant S as sheaf service
    participant B as bucket
    W->>S: ReadRefDoc (session token)
    S->>B: read the Analysis's ref document
    S-->>W: RefDoc + generation
    loop each pack the document names and the mirror lacks
        W->>S: FetchPack(pack_id)
        S->>B: get object
        S-->>W: pack bytes (stream)
    end
    Note over W: update-ref, symbolic-ref: the mirror matches the document
    G->>W: git clone over the upload-pack hatch
    W-->>G: objects from the mirror
```

A push, from the guest's `git push` to the accepted publish. Before `receive-pack` spawns, the connection's handler runs
the same sync as hydration — `ReadRefDoc`, then `FetchPack` for anything new — which is what lets git's own fast-forward
check reject in the common case; the hook then reads the document once more, to compare generations and to find the
reflog tip, so a push is two reads unless the sync state carries the tip. The hook is a subprocess `receive-pack` spawns
with a scrubbed environment; it finds the service's address and the session token the way it finds the sync state — by a
file path — rather than in its environment, since it parses guest bytes and `/proc/<pid>/environ` is readable by
anything sharing its user.

```mermaid
sequenceDiagram
    participant G as guest git
    participant W as worker (receive-pack + hook)
    participant S as sheaf service
    participant B as bucket
    G->>W: git push over the receive-pack hatch
    Note over W: pre-receive: fast-forward only, protected paths, reflog entry
    W->>S: Publish: intent, then pack bytes (stream)
    S->>B: put each pack under its content hash
    S->>B: compare-and-swap the ref document at base_generation
    alt accepted
        S-->>W: new generation
        W-->>G: refs updated
    else ABORTED / FAILED_PRECONDITION / INVALID_ARGUMENT
        S-->>W: status
        W-->>G: refused, in git's wording for the case
    end
```

The hook does not update the mirror's refs on success; the next request's sync reads them from the document. Its exit
status is what tells git to keep or discard the push's quarantine.

## The second caller

A curator changes the workspace from the workbench, and the browser does it the way the worker does: it keeps a copy of
the repository, hydrates it from the ref document and the packs, builds a commit and its reflog entry locally, and
publishes ([`workbench-workspace.md`](workbench-workspace.md)).

The browser never calls this service directly; the BFF relays each call. Before it does, it checks that the curator
belongs to the Analysis's Project, the authorization check [`workspace-model.md`](workspace-model.md) gives the BFF. It
then calls as the web tier and names the Analysis through a session it derives for it
([`rpc-authorization.md`](rpc-authorization.md)). So the web tier is admitted on `ReadRefDoc` and `Publish` beside the
worker, and `SignPackUrls` admits the web tier alone, since the worker streams packs through `FetchPack`. The service
acts on that Analysis's repository as it does for the worker.

A browser cannot stream a request body, so it hands the BFF a publish's intent and packs in one unary call, under a size
cap, and the BFF streams them into `Publish`. The service makes the checks every publish gets and adds none for this
caller. The browser is trusted as the worker is ([`workbench-workspace.md`](workbench-workspace.md) §Background). The
agent is the one writer that is checked, and the hook checks its pushes before they reach this service. The writer's
contract the hook meets for a push, a fast-forward and a correctly parented reflog entry, the browser meets by
construction, because it builds its commit on the tip it read.

### `SignPackUrls`

`SignPackUrlsRequest{pack_ids}` → `SignPackUrlsResponse`

Returns, for each named pack, a short-lived signed URL to download it from, and the pack's size. The browser hydrates
through this rather than through `FetchPack`, because a first hydration downloads the whole repository. Streaming that
through `FetchPack` would pass every byte through this service and then through the BFF, two instances that do nothing
with it. A signed URL lets the browser download each pack straight from the bucket.

- The service refuses to sign a pack the current document does not list, so a URL is only ever issued for a pack a
  reader of this repository can already name.
- The size comes back with the URL, so the browser can refuse a repository larger than its budget before it downloads
  anything.
- The signing happens here because a signed URL carries the signer's own permission to read, and this service is the one
  identity with a role on the bucket. Signing anywhere else would need a second identity with read access to every
  repository, and would move the bucket's key layout out of this service.

A signed URL works for anyone who holds it until it expires, so the browser keeps it in memory and fetches it once.

Errors: `INVALID_ARGUMENT` for a malformed request, such as more than 256 ids or one named twice. `NOT_FOUND` for a pack
the current document does not list, even one that exists; the caller reads the document again, since the list it asked
from may be stale. `DATA_LOSS` when the document lists a pack the store does not hold, or the stored document does not
parse: damage, which a caller must not retry. `UNIMPLEMENTED` when the deployment's storage backend cannot sign, as the
local-directory backend cannot.

## Deployment

`Sheaf` is a logical service; which Cloud Run service hosts it, and beside which other interfaces, is the deploy's
arrangement and is recorded there ([`deployment.md`](deployment.md)) rather than here, so the arrangement can change
without making this doc wrong. The servicer's port is the library's own storage seam, `themis.sheaf.Backend`, rather
than a port of its own ([`services.md`](services.md) has one per interface): the protocol already lives behind that
seam, and its offline mode is the local-directory backend, which mirrors a versioned bucket, not a seeded fixture — a
seedable git store would have nothing to seed it with. What the service needs of its deployment, wherever it lands:

- **Credential:** the service's identity holds `roles/storage.objectUser` on sheaf's bucket, which the sheaf service's
  infrastructure defines. No other identity holds a role on it. Replacing the ref document under `ifGenerationMatch` is
  an overwrite, and GCS requires `storage.objects.delete` for an overwrite, so a role that can only create and read
  cannot implement the protocol. "Nothing deletes" is a property of the protocol; making it a property of the bucket too
  would take object versioning or a retention policy on the pack prefix, not a narrower role. The worker's identity
  holds `run.invoker` on the service and nothing on the bucket.
- **Signing:** pack URLs are signed through IAM `signBlob`, so the service's identity holds `serviceAccountTokenCreator`
  on itself. The bucket carries a CORS rule that admits the workbench's origin, for the browser's downloads.
- **Ingress:** IAM-gated public — `run.invoker` granted per caller, default-deny otherwise — because two of its callers
  have no path onto the services VPC: the BFF, and a person driving the protocol by hand through the automation user.
- **Size:** chunks are sized under gRPC's default 4 MiB message limit. A publish's declared total, and the ref count and
  document size it would leave, are bounded by per-deployment ceilings and refused with `RESOURCE_EXHAUSTED` beyond
  them; the value is deployment configuration, and the ceiling exists because the bytes are whatever the guest pushed
  and nothing is ever reclaimed.
