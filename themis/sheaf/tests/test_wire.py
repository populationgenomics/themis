"""End-to-end with the real git binary: clone, push, and what a lost race looks like to a client.

Nothing here mocks git. The point of serving with `git http-backend` is that the wire protocol is
not sheaf's to get wrong, so a test that stubbed it out would be testing the wrong thing.
"""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Iterator

import pytest

from themis import sheaf
from themis.sheaf import orphans
from themis.sheaf.tests import conftest
from themis.sheaf.wire import bare, reflog, server

REPO = 'projects/demo'
REF = 'refs/heads/main'
LOG = 'annotations/review.jsonl'
REVIEWER = conftest.Author('Reviewer One', 'reviewer.one@example.org')


@pytest.fixture
def git_server(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> Iterator[server.SheafGitServer]:
    """A running loopback git server backed by the sheaf store."""
    instance = server.SheafGitServer(backend, tmp_path / 'bare', repos={REPO})
    with instance:
        yield instance


def _clone(instance: server.SheafGitServer, tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    target = tmp_path / name
    conftest.run_git('clone', instance.url(REPO), str(target), cwd=tmp_path)
    return target


def _commit(work: pathlib.Path, filename: str, content: str, message: str) -> None:
    (work / filename).write_text(content, 'utf-8')
    conftest.run_git('add', filename, cwd=work)
    conftest.run_git('commit', '-m', message, cwd=work)


def test_clone_commit_push_round_trip(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'notes.md', 'first\n', 'add notes')
    conftest.run_git('push', 'origin', 'main', cwd=work)

    store = sheaf.Store(backend, REPO)
    snapshot = store.read()
    assert REF in snapshot.refs
    assert snapshot.packs, 'the push should have landed a pack in the store'

    # A second, independent clone reconstructs everything from the store.
    other = _clone(git_server, tmp_path, 'other')
    assert (other / 'notes.md').read_text('utf-8') == 'first\n'


def test_a_mirror_left_without_its_hook_is_repaired_before_it_serves(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """`git init` writes HEAD first and the hook last, so a mirror can exist without one.

    Receive-pack with no `pre-receive` accepts a push, moves the mirror's refs and publishes
    nothing — `git push` reports success and the next sync force-resets the refs over the commit.
    So `ensure` has to finish a half-built mirror, not conclude from HEAD that there is nothing
    left to do.
    """
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'notes.md', 'first\n', 'add notes')
    conftest.run_git('push', 'origin', 'main', cwd=work)

    hook = git_server.bare(REPO).path / 'hooks' / bare.HOOK_NAME
    hook.unlink()

    _commit(work, 'notes.md', 'second\n', 'extend notes')
    conftest.run_git('push', 'origin', 'main', cwd=work)

    local_tip = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    assert sheaf.Store(backend, REPO).read().refs[REF] == local_tip, 'the push was accepted but not published'


def test_a_store_that_cannot_be_read_is_a_status_not_a_hang_up(
    git_server: server.SheafGitServer,
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store outage has to reach the client as a reason it can act on.

    The design leans on the client reading a real error — it is the argument for letting git's own
    fast-forward check do the rejecting. An unhandled exception in the handler thread closes the
    connection instead, and `Empty reply from server` is indistinguishable from a crashed process.
    """

    def unreadable(_key: str) -> object:
        raise ConnectionError('the bucket is unreachable')

    monkeypatch.setattr(backend, 'get_mutable', unreadable)
    refused = conftest.run_git('clone', git_server.url(REPO), str(tmp_path / 'work'), cwd=tmp_path, check=False)

    assert refused.returncode != 0
    assert '503' in refused.stderr, refused.stderr


def test_stopping_a_server_that_never_started_returns(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    """A failure between construction and `start()` leaves a caller's `finally` holding `stop()`.

    `shutdown()` waits on an event `serve_forever` sets, so on a server that never served it blocks
    with no diagnostic at all. Run on a thread so a regression fails the test rather than hanging
    the suite.
    """
    instance = server.SheafGitServer(backend, tmp_path / 'bare', repos={REPO})
    stopping = threading.Thread(target=instance.stop, daemon=True)

    stopping.start()
    stopping.join(timeout=10)

    assert not stopping.is_alive(), 'stop() deadlocked on a server that was never started'


def _pack_object_count(pack: bytes) -> int:
    """Objects in a packfile, read from its header: `PACK`, a version, then the count."""
    assert pack[:4] == b'PACK', 'not a packfile'
    return int.from_bytes(pack[8:12], 'big')


def test_creating_a_ref_at_a_published_commit_packs_only_the_reflog_entry(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """A push whose new sha the store already holds introduces no objects of its own.

    The exclusion set is the mirror's refs, and a pushed sha already among them is one of the
    published values; excluding it is what bounds the rev list. Unbounded, `pack-objects --revs`
    re-uploads the whole object database on a push that carries nothing. The one object every push
    does add is its reflog entry.
    """
    work = _clone(git_server, tmp_path, 'work')
    # Two pushes, so the history is spread over two packs: a re-pack of everything reachable would
    # be a third distinct pack rather than a duplicate of either, and dedup cannot mask it.
    _commit(work, 'notes.md', 'first\n', 'add notes')
    conftest.run_git('push', 'origin', 'main', cwd=work)
    _commit(work, 'notes.md', 'second\n', 'extend notes')
    conftest.run_git('push', 'origin', 'main', cwd=work)
    store = sheaf.Store(backend, REPO)
    published = set(store.read().packs)

    conftest.run_git('push', 'origin', 'main:refs/heads/feature', cwd=work)

    snapshot = store.read()
    assert snapshot.refs['refs/heads/feature'] == snapshot.refs[REF]
    added = [ident for ident in snapshot.packs if ident not in published]
    assert len(added) == 1, 'the push should have landed exactly one pack'
    assert _pack_object_count(store.fetch_pack(added[0])) == 1, 'the reflog commit, and nothing else'


def test_a_stale_push_is_rejected_and_the_retry_converges(
    git_server: server.SheafGitServer, tmp_path: pathlib.Path
) -> None:
    """Rejection, pull, push. Git does the rejecting, with git's own wording."""
    first = _clone(git_server, tmp_path, 'first')
    second = _clone(git_server, tmp_path, 'second')

    _commit(first, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main', cwd=first)

    _commit(second, 'b.md', 'b\n', 'add b')
    rejected = conftest.run_git('push', 'origin', 'main', cwd=second, check=False)
    assert rejected.returncode != 0
    assert 'rejected' in rejected.stderr.lower()

    conftest.run_git('pull', '--rebase', 'origin', 'main', cwd=second)
    conftest.run_git('push', 'origin', 'main', cwd=second)

    final = _clone(git_server, tmp_path, 'final')
    assert (final / 'a.md').exists()
    assert (final / 'b.md').exists()


def test_a_rejected_push_changes_nothing(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    first = _clone(git_server, tmp_path, 'first')
    second = _clone(git_server, tmp_path, 'second')

    _commit(first, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main', cwd=first)
    store = sheaf.Store(backend, REPO)
    accepted = store.read()

    _commit(second, 'b.md', 'b\n', 'add b')
    conftest.run_git('push', 'origin', 'main', cwd=second, check=False)

    after = store.read()
    assert after.refs == accepted.refs
    assert after.generation == accepted.generation


def test_both_write_paths_share_one_history(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """A commit published straight to the store and one pushed through git share a branch.

    Attribution survives the trip, and the two routes are distinguishable in it: the store's commit
    is authored by the acting user and committed by the service, so history says who decided
    something as well as what recorded it, while a pushed commit carries the client's identity in
    both. A client's own `git log` is what has to read that back.
    """
    direct = conftest.GitRepo.open(backend, REPO, tmp_path / 'seed.git')
    direct.append_line(ref=REF, path=LOG, line='PM2 reviewed', author=REVIEWER, message='review PM2')

    work = _clone(git_server, tmp_path, 'work')
    assert (work / LOG).read_text('utf-8') == 'PM2 reviewed\n'

    _commit(work, 'notes.md', 'pushed from a clone\n', 'a pushed commit')
    conftest.run_git('push', 'origin', 'main', cwd=work)

    assert direct.read_log(ref=REF, path=LOG) == ['PM2 reviewed']
    log = conftest.run_git('log', '--format=%an <%ae>|%cn <%ce>', cwd=work).stdout.splitlines()
    assert 'Reviewer One <reviewer.one@example.org>|sheaf <sheaf@localhost>' in log
    assert 'Agent <agent@example.org>|Agent <agent@example.org>' in log


def test_a_concurrent_write_is_visible_after_a_fetch(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """A client has its own clone, so it learns nothing until something makes it fetch."""
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'notes.md', 'first\n', 'add notes')
    conftest.run_git('push', 'origin', 'main', cwd=work)

    direct = conftest.GitRepo.open(backend, REPO, tmp_path / 'seed.git')
    direct.append_line(ref=REF, path=LOG, line='PM2 reviewed', author=REVIEWER, message='review PM2')

    assert not (work / 'annotations').exists()
    conftest.run_git('pull', '--rebase', 'origin', 'main', cwd=work)
    assert (work / LOG).read_text('utf-8') == 'PM2 reviewed\n'


def test_a_push_git_rejects_uploads_nothing(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """A push that git rejects never reaches the hook, so it uploads nothing."""
    first = _clone(git_server, tmp_path, 'first')
    second = _clone(git_server, tmp_path, 'second')
    _commit(first, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main', cwd=first)
    _commit(second, 'b.md', 'b\n', 'add b')
    conftest.run_git('push', 'origin', 'main', cwd=second, check=False)

    store = sheaf.Store(backend, REPO)
    assert orphans.measure(store).orphans == ()


def test_a_force_push_is_refused_and_the_store_is_untouched(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """History is append-only: a non-fast-forward is refused by the hook, not by receive-pack.

    `receive.denyNonFastForwards` would refuse it too, but only after the pre-receive hook has run
    — by which point the hook would have published the rewrite. So the hook checks ancestry itself,
    and the refusal reads as git's own so the client's ordinary recovery (`pull --rebase`, push)
    applies.
    """
    first = _clone(git_server, tmp_path, 'first')
    second = _clone(git_server, tmp_path, 'second')
    _commit(first, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main', cwd=first)
    store = sheaf.Store(backend, REPO)
    before = store.read()

    _commit(second, 'b.md', 'b\n', 'add b')
    pushed = conftest.run_git('push', '--force', 'origin', 'main', cwd=second, check=False)

    assert pushed.returncode != 0
    assert 'may only fast-forward' in pushed.stderr
    assert store.read().generation == before.generation
    final = _clone(git_server, tmp_path, 'final')
    assert (final / 'a.md').exists()
    assert not (final / 'b.md').exists()


def test_a_branch_deletion_is_refused(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main', cwd=work)
    conftest.run_git('push', 'origin', 'main:refs/heads/side', cwd=work)
    store = sheaf.Store(backend, REPO)
    before = store.read()

    pushed = conftest.run_git('push', 'origin', '--delete', 'side', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'may not be deleted' in pushed.stderr
    assert store.read().generation == before.generation


def test_a_tag_may_not_be_moved_or_deleted_either(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """`receive.denyNonFastForwards` and `receive.denyDeletes` cover branches only; the hook covers every ref."""
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    conftest.run_git('tag', 'v1', cwd=work)
    conftest.run_git('push', 'origin', 'main', 'v1', cwd=work)
    # An unrelated root, so the retagged commit does not descend from the published one: a tag
    # moved to a descendant is a fast-forward, which is allowed.
    conftest.run_git('checkout', '-q', '--orphan', 'other', cwd=work)
    _commit(work, 'c.md', 'c\n', 'add c')
    conftest.run_git('tag', '-f', 'v1', cwd=work)
    store = sheaf.Store(backend, REPO)
    before = store.read()

    moved = conftest.run_git('push', '--force', 'origin', 'v1', cwd=work, check=False)
    deleted = conftest.run_git('push', 'origin', ':refs/tags/v1', cwd=work, check=False)

    assert moved.returncode != 0
    assert deleted.returncode != 0
    assert store.read().generation == before.generation


def test_a_push_to_the_reflog_ref_is_refused(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """A fast-forward of the reflog ref is a forgery, so the whole namespace is sheaf's alone."""
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main', cwd=work)
    store = sheaf.Store(backend, REPO)
    before = store.read()

    pushed = conftest.run_git('push', 'origin', 'main:refs/sheaf/anything', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'written by sheaf' in pushed.stderr
    assert store.read().generation == before.generation


def test_every_push_writes_a_reflog_entry_that_keeps_its_tip_reachable(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """One reflog commit per push, parented on the tips it set, in the same compare-and-swap.

    The parents are what make every commit ever pushed reachable from a ref, so a repack keeps it;
    the message is what says which commit was the tip when.
    """
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main', cwd=work)
    first_tip = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    _commit(work, 'b.md', 'b\n', 'add b')
    conftest.run_git('push', 'origin', 'main', 'main:refs/heads/side', cwd=work)
    second_tip = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()

    store = sheaf.Store(backend, REPO)
    snapshot = store.read()
    entry = snapshot.tip(reflog.REF)
    assert entry is not None
    mirror = bare.BareRepo(store, tmp_path / 'mirror')
    mirror.sync()
    entries = reflog.read(mirror.git, entry)

    assert entries == [
        [reflog.Transition(REF, first_tip, second_tip), reflog.Transition('refs/heads/side', None, second_tip)],
        [reflog.Transition(REF, None, first_tip)],
    ]
    parents = mirror.git('rev-list', '--parents', '-n', '1', entry).decode().split()[1:]
    assert set(parents) >= {second_tip}, 'the new tip is a parent, so it stays reachable from the reflog'
    assert mirror.git('merge-base', '--is-ancestor', first_tip, entry) == b''


def test_two_divergent_appends_merge_without_a_conflict(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """The payoff of modelling a log as append-only.

    Without `merge=union` this is a textual conflict for the pusher to resolve; with it, git keeps
    both sides and the merge is automatic — so "appends commute" holds for a client's merges too,
    not only for the store's compare-and-swap.
    """
    direct = conftest.GitRepo.open(backend, REPO, tmp_path / 'seed.git')
    direct.ensure_attributes(ref=REF, author=REVIEWER)
    direct.append_line(ref=REF, path=LOG, line='PM2', author=REVIEWER, message='review PM2')

    first = _clone(git_server, tmp_path, 'first')
    second = _clone(git_server, tmp_path, 'second')

    (first / LOG).write_text('PM2\nPP3-from-first\n', 'utf-8')
    conftest.run_git('commit', '-am', 'append from first', cwd=first)
    conftest.run_git('push', 'origin', 'main', cwd=first)

    (second / LOG).write_text('PM2\nBS1-from-second\n', 'utf-8')
    conftest.run_git('commit', '-am', 'append from second', cwd=second)
    assert conftest.run_git('push', 'origin', 'main', cwd=second, check=False).returncode != 0

    # A merge, not a rebase: the union driver is what resolves it.
    merged = conftest.run_git('pull', '--no-rebase', 'origin', 'main', cwd=second, check=False)
    assert merged.returncode == 0, merged.stderr
    lines = (second / LOG).read_text('utf-8').splitlines()
    assert 'PP3-from-first' in lines
    assert 'BS1-from-second' in lines

    conftest.run_git('push', 'origin', 'main', cwd=second)
    assert set(direct.read_log(ref=REF, path=LOG)) == {'PM2', 'PP3-from-first', 'BS1-from-second'}


def test_the_server_serves_only_the_repositories_it_was_given(
    backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """`repos` is the only boundary a caller has, so it has to bound the server.

    The repository otherwise comes entirely from the request path, and any client that can reach
    the server could then read or write every repository in the store.
    """
    other = 'projects/other-case'
    for name in (REPO, other):
        seed = conftest.GitRepo.open(backend, name, tmp_path / f'seed-{name.replace("/", "-")}.git')
        seed.append_line(
            ref=REF,
            path='secret.txt',
            line=f'confidential to {name}',
            author=REVIEWER,
            message='seed',
        )

    with server.SheafGitServer(backend, tmp_path / 'bare', repos={REPO}) as instance:
        allowed = conftest.run_git('clone', instance.url(REPO), str(tmp_path / 'mine'), cwd=tmp_path, check=False)
        refused = conftest.run_git('clone', instance.url(other), str(tmp_path / 'theirs'), cwd=tmp_path, check=False)

    assert allowed.returncode == 0, allowed.stderr
    assert (tmp_path / 'mine' / 'secret.txt').read_text('utf-8').strip() == f'confidential to {REPO}'
    assert refused.returncode != 0
    assert not (tmp_path / 'theirs' / 'secret.txt').exists()


def test_a_malformed_object_is_refused_before_the_hook_runs(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """Incoming objects are validated by git, not by sheaf.

    The pre-receive hook is host-side code holding the store credential, and it walks a pack the
    pushing side composed. `receive.fsckObjects` is off by default in git, which would make the hook
    the first thing to inspect a malformed object; with it on, receive-pack rejects the push before
    the hook is invoked. The assertion that no `sheaf:` message appears is what proves the ordering.
    """
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'notes.md', 'first\n', 'add notes')
    conftest.run_git('push', 'origin', 'main', cwd=work)
    accepted = sheaf.Store(backend, REPO).read()

    tree = conftest.run_git('rev-parse', 'HEAD^{tree}', cwd=work).stdout.strip()
    # A commit git will happily create with --literally, and happily refuse to receive.
    malformed = (
        f'tree {tree}\n'
        'author Bad Person<bad@example.org> 1700000000 +0000\n'
        'committer Bad Person<bad@example.org> 1700000000 +0000\n'
        '\nmissing the space before the email\n'
    )
    blob = tmp_path / 'malformed-commit'
    blob.write_text(malformed, 'utf-8')
    sha = conftest.run_git('hash-object', '-t', 'commit', '-w', '--literally', str(blob), cwd=work).stdout.strip()
    conftest.run_git('update-ref', 'refs/heads/malformed', sha, cwd=work)

    refused = conftest.run_git('push', 'origin', 'refs/heads/malformed', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'missingSpaceBeforeEmail' in refused.stderr
    assert 'sheaf:' not in refused.stderr, 'the hook must never have been reached'
    after = sheaf.Store(backend, REPO).read()
    assert after.generation == accepted.generation, 'nothing may have been published'


def test_a_push_creating_a_directory_of_an_existing_ref_is_refused_by_the_hook(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """Git's own check for this runs after the hook, so the hook has to make it, or the store commits first.

    Verified the other way round before the check existed: git refused the push, the store already
    held both names, and every later sync of the mirror failed.
    """
    work = _clone(git_server, tmp_path, 'work')
    _commit(work, 'a.md', 'a\n', 'add a')
    conftest.run_git('push', 'origin', 'main:refs/heads/a', cwd=work)
    store = sheaf.Store(backend, REPO)
    before = store.read()

    pushed = conftest.run_git('push', 'origin', 'main:refs/heads/a/b', cwd=work, check=False)

    assert pushed.returncode != 0
    assert 'cannot both exist' in pushed.stderr
    assert 'Traceback' not in pushed.stderr
    assert store.read().generation == before.generation
    other = _clone(git_server, tmp_path, 'other')
    assert (other / 'a.md').exists(), 'the mirror must still serve'


def test_a_mirror_syncs_across_a_directory_file_swap(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> None:
    """A mirror holding `refs/heads/feature/x` the store does not, when the store gains `refs/heads/feature`.

    The store never deletes a ref, so the mirror only has one to drop after a push that bypassed the
    hook — the case `ensure` repairs. One update-ref transaction refuses the pair as a collision even
    though it deletes one of them, so the mirror runs the deletes first, or it could never sync again.
    """
    store = sheaf.Store(backend, REPO)
    writer = conftest.GitRepo.open(backend, REPO, tmp_path / 'writer.git')
    tip = writer.append_line(ref=REF, path=LOG, line='one', author=REVIEWER, message='one').tip(REF)
    assert tip is not None
    mirror = bare.BareRepo(store, tmp_path / 'mirror')
    mirror.sync()
    mirror.git('update-ref', 'refs/heads/feature/x', tip)
    writer.append_line(ref='refs/heads/feature', path=LOG, line='two', author=REVIEWER, message='two')

    mirror.sync()

    assert 'refs/heads/feature' in mirror.local_refs()
    assert 'refs/heads/feature/x' not in mirror.local_refs()


@pytest.mark.parametrize(
    'forged',
    [
        'sheaf: refs/heads/main\n\nnot a transition line at all',
        'sheaf: refs/heads/main',
        f'sheaf: refs/heads/main\n\nrefs/heads/main {"0" * 40} {"d" * 40}\n',
    ],
    ids=['raises on a bad body', 'phantom empty entry', 'forged transition'],
)
def test_a_root_commit_shaped_like_an_entry_cannot_reach_the_reader(
    git_server: server.SheafGitServer, backend: sheaf.LocalBackend, tmp_path: pathlib.Path, forged: str
) -> None:
    """The pushing side controls its commit messages, so the chain must end on a commit sheaf wrote.

    Each of these three root commits fooled a reader that stopped on a subject prefix. The chain is
    now rooted on a parentless entry, so the walk never reaches the pushed history at all.
    """
    work = _clone(git_server, tmp_path, 'work')
    (work / 'a.md').write_text('a\n', 'utf-8')
    conftest.run_git('add', 'a.md', cwd=work)
    conftest.run_git('commit', '-m', forged, cwd=work)
    conftest.run_git('push', 'origin', 'main', cwd=work)
    tip = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()

    store = sheaf.Store(backend, REPO)
    entry = store.read().tip(reflog.REF)
    assert entry is not None
    mirror = bare.BareRepo(store, tmp_path / 'mirror')
    mirror.sync()

    assert reflog.read(mirror.git, entry) == [[reflog.Transition(REF, None, tip)]]
