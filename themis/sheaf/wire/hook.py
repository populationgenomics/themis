"""The pre-receive hook: the one place sheaf gets a say in a push.

Git has validated the objects by the time this runs, and no ref has moved yet. The hook refuses
anything that would rewrite or delete history, writes the reflog entry for what remains, packs the
new objects, uploads them, and compare-and-swaps the ref document against the generation the
client's view was built from. Exiting non-zero makes git
discard the quarantine and leave every ref untouched, so a rejection here cannot leave the bare repo
disagreeing with the store. The converse has to hold too: anything git would refuse *after* this hook
— a ref name that collides with an existing one as a directory — must be refused here, or the store
commits what the mirror can never write.

`pre-receive` rather than `update`: it sees the whole push at once, so a multi-ref push maps onto a
single compare-and-swap. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import os
import sys

from themis.sheaf import errors, stores
from themis.sheaf import store as store_mod
from themis.sheaf.wire import bare, protect, reflog

# An all-zero object id means "absent". Both widths, because git sends the zero oid at the
# repository's own hash length.
ZERO_OIDS = frozenset({'0' * 40, '0' * 64})
# git sends `<old> <new> <ref>` per line.
FIELDS_PER_LINE = 3
SYNC_STATE_ENV = 'SHEAF_SYNC_STATE'
# git sets GIT_DIR for hooks, but the server passes an absolute path so the hook never depends on
# the working directory it was invoked in.
GIT_DIR_ENV = 'SHEAF_GIT_DIR'

_MOVED = 'the workspace moved while your push was in flight'
_MOVED_HINT = 'run `git pull --rebase` (or merge) and push again.'


def environment(repo: bare.BareRepo, protection: protect.Protection) -> dict[str, str]:
    """The environment a `git` serving `repo` runs with, so that this hook can find its inputs.

    Scrubbed on purpose: the hook holds the store credential and reads a pack the client composed,
    so it must not inherit the host's secrets, and only the variables named here reach it. A server
    hands this to the `git` it spawns — `http-backend`, `upload-pack`, `receive-pack` — and adds
    whatever that command itself needs.
    """
    # STORAGE_EMULATOR_HOST is the storage SDK's own contract for redirecting the client, and
    # carrying it through is what lets the whole path run against an emulator. Unset in
    # production, where it does nothing.
    emulator = os.environ.get('STORAGE_EMULATOR_HOST')
    return {
        **protection.as_env(),
        **({'STORAGE_EMULATOR_HOST': emulator} if emulator else {}),
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        SYNC_STATE_ENV: str(repo.sync_state_path),
        GIT_DIR_ENV: str(repo.path),
    }


def _refuse(reasons: list[str], hint: str) -> int:
    """Report a refusal to the client and return the hook's exit status.

    Each line says what happened and the last says what to do about it: git relays stderr to the
    client prefixed with `remote:`, and the thing reading it may be a model trained on git's own
    wording.
    """
    for reason in reasons:
        print(f'sheaf: refused: {reason}', file=sys.stderr)
    print(f'sheaf: {hint}', file=sys.stderr)
    return 1


def parse_stdin(lines: list[str]) -> dict[str, store_mod.RefUpdate]:
    """Turn git's `<old> <new> <ref>` lines into ref updates.

    An all-zero object id means absent: as `old` that is a ref being created, as `new` a ref being
    deleted.

    Raises:
        ValueError: If a line is not three whitespace-separated fields. Publishing the lines that
            did parse would compare-and-swap a subset of what git is about to update, leaving the
            store missing a ref the mirror has.
    """
    updates = {}
    for line in lines:
        parts = line.split()
        if len(parts) != FIELDS_PER_LINE:
            raise ValueError(f'expected {FIELDS_PER_LINE} fields per line, got {line!r}')
        old, new, ref = parts
        updates[ref] = store_mod.RefUpdate(
            old=None if old in ZERO_OIDS else old,
            new=None if new in ZERO_OIDS else new,
        )
    return updates


def main(argv: list[str] | None = None) -> int:
    """Run as git's pre-receive hook, reading the push from stdin.

    Args:
        argv: Unused; git passes a pre-receive hook no arguments.

    Returns:
        A process exit status: zero to accept the push, non-zero to refuse it.

    Anything the store refuses — a lost race, a non-fast-forward, a name git cannot hold — is a
    refusal message to the client, never a traceback; so is a deployment fault — an unreadable
    state file or token file, a descriptor this build cannot rebuild, a service that cannot be
    reached or does not admit the caller, a document this code did not write.

    Raises:
        RuntimeError: If git fails while the pack of new objects is being built.
        SheafError: If the store cannot be read or written for a reason that is neither a refusal
            nor one of the deployment faults above.
    """
    del argv
    state_path = os.environ.get(SYNC_STATE_ENV)
    if not state_path:
        return _refuse([f'{SYNC_STATE_ENV} is not set'], 'this is a deployment fault, not yours.')

    try:
        updates = parse_stdin(sys.stdin.read().splitlines())
    except ValueError as exc:
        return _refuse([str(exc)], 'this is a deployment fault, not yours.')
    if not updates:
        return 0

    # The state file and the descriptor are written by the process that installed this hook, so a
    # fault in either is that deployment's, and is reported to the pusher as such rather than as
    # the traceback git would otherwise relay.
    try:
        state = bare.SyncState.load(state_path)
        store = stores.from_descriptor(state.store)
    except (OSError, ValueError, KeyError, ImportError, errors.CredentialsUnusable) as exc:
        return _refuse([f'{type(exc).__name__}: {exc}'], 'this is a deployment fault, not yours.')
    repo = bare.BareRepo(store, os.environ.get(GIT_DIR_ENV) or os.environ.get('GIT_DIR', '.'))

    # Policy first: a protection violation is a definite refusal, so report it even where the push
    # also lost a race — otherwise the pusher is told to retry something that can never succeed.
    refusals = protect.violations(repo, updates, protect.Protection.from_env())
    if refusals:
        return _refuse(refusals, 'history here is append-only: fast-forward the ref, and revert any protected path.')

    # The client built its push against the refs advertised at `state.generation`. Anything else
    # there now means somebody landed in between, and the push has to be rebuilt rather than merged
    # blindly on this side.
    try:
        return _publish(store, repo, updates, state.generation)
    except (errors.ServiceFault, errors.CorruptRepository, errors.CredentialsUnusable) as exc:
        return _refuse([str(exc)], 'this is a deployment fault, not yours.')


def _publish(
    store: store_mod.Repository,
    repo: bare.BareRepo,
    updates: dict[str, store_mod.RefUpdate],
    synced_generation: int | None,
) -> int:
    """Read once more, write the reflog entry, pack, publish; the hook's exit status.

    Raises:
        ServiceFault: If the service could not be reached or did not admit the caller.
        CredentialsUnusable: If the token file a remote store presents cannot be used.
        CorruptRepository: If the stored document is not one this code wrote.
        RuntimeError: If git fails while the pack of new objects is being built.
    """
    snapshot = store.read()
    if snapshot.generation != synced_generation:
        return _refuse([_MOVED], _MOVED_HINT)
    # Every update is a create or a fast-forward by now, so each has a new tip to log. The reflog
    # commit is written into git's quarantine alongside the pushed objects and travels in the same
    # pack and the same compare-and-swap; a refusal discards it with the rest.
    transitions = [
        reflog.Transition(ref, update.old, update.new) for ref, update in sorted(updates.items()) if update.new
    ]
    entry = reflog.record(repo.git, snapshot.tip(reflog.REF), transitions)
    ref_updates = {**updates, reflog.REF: store_mod.RefUpdate(snapshot.tip(reflog.REF), entry)}
    packs = [repo.pack_for([entry])]

    try:
        store.publish(snapshot, store_mod.Intent(ref_updates=ref_updates, packs=packs))
    except errors.InvalidRefName as exc:
        # Git's own check for this runs in the ref transaction, after this hook, so it has to be
        # refused here or the store commits a ref set the mirror can never write.
        return _refuse([str(exc)], 'rename the ref and push again.')
    except (errors.RaceLost, errors.RefConflict) as exc:
        # Same refusal, different guidance: a race is retryable, a non-fast-forward needs a merge.
        raced = isinstance(exc, errors.RaceLost)
        return _refuse(
            [_MOVED if raced else str(exc)],
            _MOVED_HINT if raced else 'fetch first, then push again.',
        )
    except errors.PublishRefused as exc:
        return _refuse(
            [str(exc)], 'the service refused this publish; the same push will not land, so fix what it names.'
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
