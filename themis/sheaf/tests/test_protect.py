"""Protected paths: what the pre-receive hook refuses, and what it must not.

The two cases that bound the check: a clean merge bringing a protected file in verbatim has to be
allowed, or the pushing side can never take a pull, and a commit that stages content through git's
plumbing without touching the working tree has to be refused.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest

from themis import sheaf
from themis.sheaf.tests import conftest
from themis.sheaf.wire import protect, reflog, server

REPO = 'projects/demo'
REF = 'refs/heads/main'
ASSERTIONS = 'annotations/assertions.jsonl'
ANCHORS = 'annotations/anchors.jsonl'
REVIEWER = conftest.Author('Reviewer One', 'reviewer.one@example.org')


def test_protection_is_opt_in() -> None:
    assert not protect.Protection().active
    assert not protect.Protection().forbids(ASSERTIONS)


def test_patterns_cross_directory_separators() -> None:
    protection = protect.Protection(paths=('annotations/*',))
    assert protection.forbids(ASSERTIONS)
    assert protection.forbids('annotations/nested/deep.jsonl')
    assert not protection.forbids('documents/report.md')


def test_it_survives_the_trip_through_the_environment() -> None:
    original = protect.Protection(paths=(ASSERTIONS, '.gitattributes'))
    env = original.as_env()
    assert env[protect.PATHS_ENV] == f'{ASSERTIONS}:.gitattributes'
    assert protect.Protection.from_env(env) == original


def test_a_pattern_carrying_the_separator_is_refused() -> None:
    """A colon is legal in a path, and the trip through the environment cannot survive one.

    `annotations/a:b.jsonl` would reach the hook as two patterns matching nothing, so the
    protection would be silently absent and a push fabricating the file accepted.
    """
    with pytest.raises(ValueError, match='may not contain'):
        protect.Protection(paths=(f'annotations/a{protect.SEPARATOR}b.jsonl',))


@pytest.fixture
def protected(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> Iterator[server.SheafGitServer]:
    """A server that refuses writes to the assertions log.

    History is append-only on every server, so the rewrite route to a protected file — drop the
    commit that wrote it and force-push — is closed without configuration; only the path half is
    opted into here.
    """
    instance = server.SheafGitServer.over_backend(
        backend,
        tmp_path / 'bare',
        repos={REPO},
        protection=protect.Protection(paths=(ASSERTIONS,)),
    )
    with instance:
        yield instance


@pytest.fixture
def curator(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> conftest.GitRepo:
    """A writer publishing straight to the store, which never meets the hook."""
    return conftest.GitRepo.open(backend, REPO, tmp_path / 'curator.git')


def _sign_off(curator: conftest.GitRepo, code: str) -> None:
    curator.append_line(
        ref=REF,
        path=ASSERTIONS,
        line=f'{{"code": "{code}", "state": "reviewed", "by": "reviewer.one@example.org"}}',
        author=REVIEWER,
        message=f'review {code}',
    )


def _clone(instance: server.SheafGitServer, tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    target = tmp_path / name
    conftest.run_git('clone', instance.url(REPO), str(target), cwd=tmp_path)
    return target


def test_a_protected_path_cannot_be_appended_to(
    protected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    _sign_off(curator, 'PM2')
    work = _clone(protected, tmp_path, 'work')

    (work / ASSERTIONS).write_text(
        (work / ASSERTIONS).read_text('utf-8')
        + '{"code": "PP3", "state": "reviewed", "by": "reviewer.one@example.org"}\n',
        'utf-8',
    )
    conftest.run_git('commit', '-am', 'fabricate a sign-off', cwd=work)
    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'protected' in refused.stderr
    assert ASSERTIONS in refused.stderr


def test_the_plumbing_route_is_refused_too(
    protected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """What a read-only mount cannot stop: staging content without touching the working tree.

    `hash-object` plus `update-index` builds the tree directly. The file on disk is never opened for
    writing, so no filesystem permission is involved — but the pushed objects still carry the
    fabrication, and objects are what the hook inspects.
    """
    _sign_off(curator, 'PM2')
    work = _clone(protected, tmp_path, 'work')
    before = (work / ASSERTIONS).read_text('utf-8')

    fabricated = tmp_path / 'fabricated.jsonl'
    fabricated.write_text(before + '{"code": "PP3", "state": "reviewed", "by": "reviewer.one@example.org"}\n', 'utf-8')
    sha = conftest.run_git('hash-object', '-w', str(fabricated), cwd=work).stdout.strip()
    conftest.run_git('update-index', '--cacheinfo', f'100644,{sha},{ASSERTIONS}', cwd=work)
    conftest.run_git('commit', '-m', 'staged without touching the file', cwd=work)

    assert (work / ASSERTIONS).read_text('utf-8') == before, 'the working tree was never written'
    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)
    assert refused.returncode != 0
    assert ASSERTIONS in refused.stderr


def test_a_clean_merge_of_a_protected_path_is_allowed(
    protected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The case a naive diff-against-first-parent check would break, making pulls impossible."""
    _sign_off(curator, 'PM2')
    work = _clone(protected, tmp_path, 'work')

    (work / 'documents').mkdir()
    (work / 'documents' / 'report.md').write_text('a revision\n', 'utf-8')
    conftest.run_git('add', 'documents/report.md', cwd=work)
    conftest.run_git('commit', '-m', 'revise the report', cwd=work)

    # A concurrent sign-off lands, so the push is behind.
    _sign_off(curator, 'PP3')
    assert conftest.run_git('push', 'origin', 'main', cwd=work, check=False).returncode != 0

    merged = conftest.run_git('pull', '--no-rebase', 'origin', 'main', cwd=work, check=False)
    assert merged.returncode == 0, merged.stderr
    conftest.run_git('push', 'origin', 'main', cwd=work)

    assert [line.split('"')[3] for line in curator.read_log(ref=REF, path=ASSERTIONS)] == ['PM2', 'PP3']


def test_an_orphan_root_commit_cannot_smuggle_a_protected_path(
    protected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The route that is invisible at both steps unless roots are diffed.

    `diff-tree` emits nothing for a parentless commit without `--root`, and `-c` on the merge lists
    only paths differing from every parent — so a merge resolved in the orphan's favour introduces
    nothing either. Both halves have to be checked for the fabrication to be caught.
    """
    _sign_off(curator, 'PM2')
    work = _clone(protected, tmp_path, 'work')

    conftest.run_git('checkout', '--orphan', 'fake', cwd=work)
    conftest.run_git('rm', '-rqf', '.', cwd=work)
    (work / ASSERTIONS).parent.mkdir(parents=True, exist_ok=True)
    (work / ASSERTIONS).write_text('{"code": "PVS1", "state": "reviewed", "by": "fabricated"}\n', 'utf-8')
    conftest.run_git('add', ASSERTIONS, cwd=work)
    conftest.run_git('commit', '-m', 'orphan root', cwd=work)

    conftest.run_git('checkout', 'main', cwd=work)
    conftest.run_git('merge', '--allow-unrelated-histories', '-X', 'theirs', '--no-edit', 'fake', cwd=work)
    assert 'fabricated' in (work / ASSERTIONS).read_text('utf-8'), 'the merge took the orphan side'

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)
    assert refused.returncode != 0
    assert ASSERTIONS in refused.stderr
    assert [line.split('"')[3] for line in curator.read_log(ref=REF, path=ASSERTIONS)] == ['PM2']


def _benign_and_forged(work: pathlib.Path) -> tuple[str, str]:
    """Two children of `main`: one revising an unprotected file, and one fabricating a sign-off, left checked out."""
    conftest.run_git('checkout', '-q', '-b', 'decoy', cwd=work)
    (work / 'documents').mkdir()
    (work / 'documents' / 'report.md').write_text('a revision\n', 'utf-8')
    conftest.run_git('add', 'documents/report.md', cwd=work)
    conftest.run_git('commit', '-m', 'revise the report', cwd=work)
    benign = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()

    conftest.run_git('checkout', '-q', 'main', cwd=work)
    with (work / ASSERTIONS).open('a', encoding='utf-8') as log:
        log.write('{"code": "PP3", "state": "reviewed", "by": "reviewer.one@example.org"}\n')
    conftest.run_git('commit', '-am', 'fabricate a sign-off', cwd=work)
    forged = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    return benign, forged


def test_a_replace_ref_cannot_be_pushed_to_disguise_a_protected_write(
    protected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The two-push shape: first a replace ref standing a benign commit in for a forged one, then the forged commit.

    Were the first push to land, a mirror's git honouring replace refs would check the second as the benign commit
    while `pack-objects`, which ignores them, stored the forged one.
    """
    _sign_off(curator, 'PM2')
    work = _clone(protected, tmp_path, 'work')
    benign, forged = _benign_and_forged(work)

    disguise = conftest.run_git('push', 'origin', f'{benign}:refs/replace/{forged}', cwd=work, check=False)
    assert disguise.returncode != 0
    assert f'refs/replace/{forged} is not a ref a push may write' in disguise.stderr

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)
    assert refused.returncode != 0
    assert ASSERTIONS in refused.stderr
    assert not [ref for ref in curator.store.read().refs if ref.startswith('refs/replace/')]
    assert [line.split('"')[3] for line in curator.read_log(ref=REF, path=ASSERTIONS)] == ['PM2']


def test_a_replace_ref_already_in_the_mirror_does_not_change_what_the_hook_reads(
    protected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The mirror's git reads the objects a push carries, whatever replace refs the mirror holds.

    The replace ref is published straight to the store, as a writer that never meets the hook could, so every sync
    writes it into the mirror ahead of the push the hook then checks.
    """
    _sign_off(curator, 'PM2')
    work = _clone(protected, tmp_path, 'work')
    benign, forged = _benign_and_forged(work)
    conftest.run_git('push', 'origin', 'decoy', cwd=work)

    mirror = protected.bare(REPO)
    base = mirror.sync()
    replace_ref = f'refs/replace/{forged}'
    entry = reflog.record(mirror.git, base.tip(reflog.REF), [reflog.Transition(replace_ref, None, benign)])
    curator.store.publish(
        base,
        sheaf.Intent(
            ref_updates={
                replace_ref: sheaf.RefUpdate(None, benign),
                reflog.REF: sheaf.RefUpdate(base.tip(reflog.REF), entry),
            },
            packs=[mirror.pack_for([entry])],
        ),
    )
    mirror.sync()
    assert mirror.local_refs()[replace_ref] == benign

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)
    assert refused.returncode != 0
    assert ASSERTIONS in refused.stderr
    assert [line.split('"')[3] for line in curator.read_log(ref=REF, path=ASSERTIONS)] == ['PM2']


def test_a_quoted_path_cannot_dodge_a_glob(
    backend: sheaf.LocalBackend, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """One non-ASCII byte in a filename must not defeat a directory glob.

    `diff-tree` C-quotes such a path unless `-z` turns quoting off, and the quoted form starts with
    a literal double quote that no `annotations/*` glob matches — while a consumer listing the
    directory still reads the file. The check has to see the same name the consumer does.
    """
    _sign_off(curator, 'PM2')
    protection = protect.Protection(paths=('annotations/*',))
    with server.SheafGitServer.over_backend(
        backend, tmp_path / 'bare', repos={REPO}, protection=protection
    ) as instance:
        work = _clone(instance, tmp_path, 'work')
        smuggled = 'annotations/naïve.jsonl'
        (work / smuggled).write_text('{"code": "PP3", "state": "reviewed", "by": "fabricated"}\n', 'utf-8')
        conftest.run_git('add', smuggled, cwd=work)
        conftest.run_git('commit', '-m', 'fabricate under a name git would quote', cwd=work)
        refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'protected' in refused.stderr
    # History length rather than a lookup of the smuggled path: Unicode normalisation on the way
    # through the filesystem could make that lookup miss and pass vacuously.
    assert len(curator.history(REF)) == 1


def test_an_unprotected_sibling_path_stays_writable(
    protected: server.SheafGitServer,
    curator: conftest.GitRepo,
    backend: sheaf.LocalBackend,
    tmp_path: pathlib.Path,
) -> None:
    """The split that makes a one-line glob sufficient.

    Assertions and anchors are separate files so that where a mark points stays writable by the
    pushing side while whether it exists does not.
    """
    _sign_off(curator, 'PM2')
    work = _clone(protected, tmp_path, 'work')

    (work / ANCHORS).write_text('{"code": "PM2", "span": [120, 180]}\n', 'utf-8')
    conftest.run_git('add', ANCHORS, cwd=work)
    conftest.run_git('commit', '-m', 're-anchor PM2 after editing the report', cwd=work)
    conftest.run_git('push', 'origin', 'main', cwd=work)

    verify = conftest.GitRepo.open(backend, REPO, tmp_path / 'verify.git')
    assert verify.read_log(ref=REF, path=ANCHORS) == ['{"code": "PM2", "span": [120, 180]}']
    assert len(verify.read_log(ref=REF, path=ASSERTIONS)) == 1


def test_without_protection_the_same_push_is_accepted(
    backend: sheaf.LocalBackend, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """Policy lives in the wire layer and is opt-in; the store itself has no opinion."""
    _sign_off(curator, 'PM2')
    with server.SheafGitServer.over_backend(backend, tmp_path / 'bare', repos={REPO}) as instance:
        work = _clone(instance, tmp_path, 'work')
        (work / ASSERTIONS).write_text('{"code": "PP3", "state": "reviewed"}\n', 'utf-8')
        conftest.run_git('commit', '-am', 'write the log', cwd=work)
        conftest.run_git('push', 'origin', 'main', cwd=work)
    assert sheaf.Store(backend, REPO).read().tip(REF) is not None
