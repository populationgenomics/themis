"""What the pre-receive hook refuses of a push's new commits, and what it must not.

Protected paths, bounded by two cases: a clean merge bringing a protected file in verbatim has to be
allowed, or the pushing side can never take a pull, and a commit that stages content through git's
plumbing without touching the working tree has to be refused. The pusher's identity, bounded the same
way: a merge bringing in another writer's commit passes, a cherry-pick copying one does not. And names
the workbench cannot read, refused on every server. Every push is a real `git push`.
"""

from __future__ import annotations

import base64
import pathlib
import shutil
import subprocess
from collections.abc import Iterator

import pytest

from themis import sheaf
from themis.sheaf.tests import conftest
from themis.sheaf.wire import bare, protect, reflog, server

REPO = 'projects/demo'
REF = 'refs/heads/main'
ASSERTIONS = 'annotations/assertions.jsonl'
ANCHORS = 'annotations/anchors.jsonl'
REVIEWER = conftest.Author('Reviewer One', 'reviewer.one@example.org')
# The identity every commit and tag `conftest.run_git` makes carries.
PUSHER = protect.Identity('Agent', 'agent@example.org')


def test_protection_is_opt_in() -> None:
    assert not protect.Protection().forbids(ASSERTIONS)
    assert protect.Protection().pusher is None


@pytest.mark.parametrize('pattern', ['.MailMap', '[A-z]*'])
def test_a_pattern_not_written_casefolded_is_refused(pattern: str) -> None:
    """Folding a pattern would rewrite its bracket ranges, so it is written folded instead."""
    with pytest.raises(ValueError, match='casefolded'):
        protect.Protection(paths=(pattern,))


def test_patterns_match_in_any_case() -> None:
    """A curator's clone on a case-insensitive filesystem opens `.MailMap` as `.mailmap`."""
    protection = protect.Protection(paths=('.mailmap', 'annotations/*'))
    assert protection.forbids('.MailMap')
    assert protection.forbids('Annotations/Assertions.jsonl')


def test_patterns_cross_directory_separators() -> None:
    protection = protect.Protection(paths=('annotations/*',))
    assert protection.forbids(ASSERTIONS)
    assert protection.forbids('annotations/nested/deep.jsonl')
    assert not protection.forbids('documents/report.md')


@pytest.mark.parametrize('pusher', [None, PUSHER])
def test_it_survives_the_trip_through_the_environment(pusher: protect.Identity | None) -> None:
    original = protect.Protection(paths=(ASSERTIONS, '.gitattributes'), pusher=pusher)
    env = original.as_env()
    assert env[protect.PATHS_ENV] == f'{ASSERTIONS}:.gitattributes'
    assert protect.Protection.from_env(env) == original


@pytest.mark.parametrize(
    ('name', 'email'),
    [('', 'agent@example.org'), ('Agent', ''), ('Agent', 'agent <agent@example.org>'), ('Agent\nX', 'a@example.org')],
)
def test_an_identity_git_cannot_record_is_refused(name: str, email: str) -> None:
    with pytest.raises(ValueError, match='identity'):
        protect.Identity(name, email)


@pytest.mark.parametrize('present', [protect.PUSHER_NAME_ENV, protect.PUSHER_EMAIL_ENV])
def test_half_an_identity_in_the_environment_is_refused(present: str) -> None:
    """A server that passed a name and no email is misbuilt; reading it as no identity would check nothing."""
    with pytest.raises(ValueError, match='together'):
        protect.Protection.from_env({present: 'x'})


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


# --- the pusher's identity -------------------------------------------------------------------------------


@pytest.fixture
def identified(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> Iterator[server.SheafGitServer]:
    """A server that takes new commits only under the pusher's own identity."""
    instance = server.SheafGitServer.over_backend(
        backend, tmp_path / 'bare', repos={REPO}, protection=protect.Protection(pusher=PUSHER)
    )
    with instance:
        yield instance


def _commit(work: pathlib.Path, path: str, content: str) -> None:
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, 'utf-8')
    conftest.run_git('add', path, cwd=work)
    conftest.run_git('commit', '-m', f'write {path}', cwd=work)


@pytest.mark.parametrize(
    ('config', 'author', 'named'),
    [
        ((), f'Reviewer One <{REVIEWER.email}>', f'authored as Reviewer One <{REVIEWER.email}>'),
        (('-c', f'user.email={REVIEWER.email}'), str(PUSHER), f'committed as Agent <{REVIEWER.email}>'),
        # The agent's own email under a curator's name is still a curator's name on the agent's commit.
        ((), f'Jane Smith (curator) <{PUSHER.email}>', f'authored as Jane Smith (curator) <{PUSHER.email}>'),
        (('-c', 'user.name=Jane Smith (curator)'), str(PUSHER), f'committed as Jane Smith (curator) <{PUSHER.email}>'),
    ],
    ids=['author', 'committer', 'author name', 'committer name'],
)
def test_a_new_commit_under_another_name_is_refused(
    identified: server.SheafGitServer,
    curator: conftest.GitRepo,
    tmp_path: pathlib.Path,
    config: tuple[str, ...],
    author: str,
    named: str,
) -> None:
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    (work / 'notes.md').write_text('forged\n', 'utf-8')
    conftest.run_git('add', 'notes.md', cwd=work)
    conftest.run_git(*config, 'commit', '--author', author, '-m', 'forge a sign-off', cwd=work)

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert named in refused.stderr
    assert '--reset-author' in refused.stderr
    assert '--root' in refused.stderr, 'a first push has no upstream to rebase onto'
    assert len(curator.history(REF)) == 1


@pytest.mark.parametrize('crafted', ['\x1c', '\x0c', '\u2028'], ids=['file separator', 'form feed', 'line separator'])
def test_a_name_holding_a_line_break_python_splits_on_is_refused_not_a_traceback(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path, crafted: str
) -> None:
    """Characters `str.splitlines` breaks on but git records in an ident must not split a record."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    _commit(work, 'notes.md', 'one\n')
    forged = f'Agent{crafted}X <agent{crafted}x@example.org>'
    conftest.run_git('commit', '--amend', '--no-edit', '--author', forged, cwd=work)

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'authored as' in refused.stderr
    assert 'Traceback' not in refused.stderr


def test_the_reset_author_the_refusal_names_lands_the_push(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    _commit(work, 'notes.md', 'one\n')
    conftest.run_git('commit', '--amend', '--no-edit', '--author', f'Reviewer One <{REVIEWER.email}>', cwd=work)
    assert conftest.run_git('push', 'origin', 'main', cwd=work, check=False).returncode != 0

    conftest.run_git('commit', '--amend', '--no-edit', '--reset-author', cwd=work)
    conftest.run_git('push', 'origin', 'main', cwd=work)

    assert len(curator.history(REF)) == 2


def test_a_merge_bringing_in_another_writer_s_commit_passes(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The curator's commit is already in the store, so it is not new, and only the merge is checked."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    _commit(work, 'notes.md', 'mine\n')
    _sign_off(curator, 'PP3')
    conftest.run_git('pull', '--no-rebase', '--no-edit', 'origin', 'main', cwd=work)

    conftest.run_git('push', 'origin', 'main', cwd=work)

    authors = conftest.run_git('log', '--format=%ae', 'origin/main', cwd=work).stdout.split()
    assert REVIEWER.email in authors
    assert PUSHER.email in authors


def test_a_cherry_pick_copying_another_writer_s_commit_is_refused(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The copy is new and keeps the curator's name, which would put it on a commit the pusher made."""
    _sign_off(curator, 'PM2')
    curator.write_files(ref='refs/heads/review', files={'review.md': 'approved\n'}, author=REVIEWER, message='approve')
    work = _clone(identified, tmp_path, 'work')
    conftest.run_git('cherry-pick', 'origin/review', cwd=work)

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert f'authored as Reviewer One <{REVIEWER.email}>' in refused.stderr


def test_without_a_pusher_identity_any_name_is_taken(
    backend: sheaf.LocalBackend, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The negative control: the same push the identity rule refuses lands on a server that states none."""
    _sign_off(curator, 'PM2')
    with server.SheafGitServer.over_backend(backend, tmp_path / 'bare', repos={REPO}) as instance:
        work = _clone(instance, tmp_path, 'work')
        _commit(work, 'notes.md', 'one\n')
        conftest.run_git('commit', '--amend', '--no-edit', '--author', f'Reviewer One <{REVIEWER.email}>', cwd=work)
        conftest.run_git('push', 'origin', 'main', cwd=work)
    assert len(curator.history(REF)) == 2


def _tag(work: pathlib.Path, name: str, target: str, *config: str) -> None:
    conftest.run_git(*config, 'tag', '-a', name, '-m', name, target, cwd=work)


def test_an_annotated_tag_under_another_name_is_refused(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """`rev-list` peels a tag to its commit, so the tag object's own tagger line needs a check of its own."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    _tag(work, 'approved', 'HEAD', '-c', 'user.name=Jane Curator', '-c', 'user.email=jane@example.org')

    refused = conftest.run_git('push', 'origin', 'refs/tags/approved', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'tagged as Jane Curator <jane@example.org>' in refused.stderr
    assert 'refs/tags/approved' not in curator.store.read().refs


def test_a_forged_tag_behind_a_tag_of_the_pusher_s_own_is_refused(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """A tag of a tag: the pushed tip is the pusher's, the one it peels through is not."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    _tag(work, 'inner', 'HEAD', '-c', 'user.name=Jane Curator', '-c', 'user.email=jane@example.org')
    _tag(work, 'outer', 'inner')
    conftest.run_git('tag', '-d', 'inner', cwd=work)

    refused = conftest.run_git('push', 'origin', 'refs/tags/outer', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'tagged as Jane Curator <jane@example.org>' in refused.stderr


def test_an_annotated_tag_of_the_pusher_s_own_lands_and_a_fresh_mirror_reads_it(
    identified: server.SheafGitServer, curator: conftest.GitRepo, backend: sheaf.LocalBackend, tmp_path: pathlib.Path
) -> None:
    """A tag object cannot be a commit's parent, so the reflog entry is parented on the commit the tag peels to."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    _tag(work, 'reviewed', 'HEAD')
    tag = conftest.run_git('rev-parse', 'reviewed', cwd=work).stdout.strip()

    conftest.run_git('push', 'origin', 'refs/tags/reviewed', cwd=work)

    snapshot = curator.store.read()
    assert snapshot.refs['refs/tags/reviewed'] == tag
    fresh = bare.BareRepo(sheaf.Store(backend, REPO), tmp_path / 'fresh.git')
    fresh.sync()
    fresh.git('fsck', '--full', '--strict')
    entry = snapshot.tip(reflog.REF)
    assert entry is not None
    newest = reflog.read(fresh.git, entry)[0]
    assert newest == [reflog.Transition('refs/tags/reviewed', None, tag)]
    assert fresh.git('cat-file', '-t', tag).strip() == b'tag'


def _crafted(work: pathlib.Path, kind: str, content: str) -> str:
    """Write an object git's porcelain would not build, bypassing hash-object's own checks as a forger would."""
    written = conftest.run_git('hash-object', '-t', kind, '-w', '--literally', '--stdin', cwd=work, stdin=content)
    return written.stdout.strip()


def _ident(identity: str) -> str:
    return f'{identity} 1700000000 +0000'


@pytest.mark.parametrize(
    'headers',
    [
        # `git log` reports the second author, `for-each-ref` the first: each reader sees a different name.
        [
            f'author {_ident("Jane Curator <jane@example.org>")}',
            f'committer {_ident(str(PUSHER))}',
            f'author {_ident(str(PUSHER))}',
        ],
        [
            f'author {_ident(str(PUSHER))}',
            f'committer {_ident("Jane Curator <jane@example.org>")}',
            f'committer {_ident(str(PUSHER))}',
        ],
    ],
    ids=['two authors', 'two committers'],
)
def test_a_commit_repeating_an_identity_header_is_refused(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path, headers: list[str]
) -> None:
    """Git's fsck accepts a repeated header after `committer`, so the hook counts lines rather than reading one."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    tree = conftest.run_git('rev-parse', 'HEAD^{tree}', cwd=work).stdout.strip()
    parent = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    commit = _crafted(work, 'commit', '\n'.join([f'tree {tree}', f'parent {parent}', *headers, '', 'forged', '']))
    conftest.run_git('update-ref', 'refs/heads/main', commit, cwd=work)

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'written with 2' in refused.stderr
    assert len(curator.history(REF)) == 1


@pytest.mark.parametrize(
    ('taggers', 'named'),
    [
        # The first tagger is the pusher's; the last, the one isomorphic-git reads, is not.
        (
            [f'tagger {_ident(str(PUSHER))}', f'tagger {_ident("Jane Curator <jane@example.org>")}'],
            'written with 2 tagger',
        ),
        ([], 'written with 0 tagger'),
    ],
    ids=['two taggers', 'no tagger'],
)
def test_a_tag_without_exactly_one_tagger_is_refused(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path, taggers: list[str], named: str
) -> None:
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    target = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    tag = _crafted(work, 'tag', '\n'.join([f'object {target}', 'type commit', 'tag approved', *taggers, '', 'ok', '']))

    refused = conftest.run_git('push', 'origin', f'{tag}:refs/tags/approved', cwd=work, check=False)

    assert refused.returncode != 0
    assert named in refused.stderr
    assert 'refs/tags/approved' not in curator.store.read().refs


@pytest.mark.parametrize('annotated', [False, True], ids=['tree', 'tag of a tree'])
def test_a_ref_at_anything_but_a_commit_is_refused_with_a_remedy(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path, annotated: bool
) -> None:
    """The reflog entry is parented on the commit each tip peels to, so a tip that peels to none is refused first."""
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')
    tip = conftest.run_git('rev-parse', 'HEAD^{tree}', cwd=work).stdout.strip()
    if annotated:
        conftest.run_git('tag', '-a', 'snapshot', '-m', 'snapshot', tip, cwd=work)
        tip = conftest.run_git('rev-parse', 'snapshot', cwd=work).stdout.strip()

    refused = conftest.run_git('push', 'origin', f'{tip}:refs/tags/snapshot', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'not a commit or a tag of one' in refused.stderr
    assert 'Traceback' not in refused.stderr
    assert 'refs/tags/snapshot' not in curator.store.read().refs


def _utf7(text: str) -> str:
    return '+' + base64.b64encode(text.encode('utf-16-be')).decode().rstrip('=') + '-'


def _push_crafted_commit(
    work: pathlib.Path, headers: list[str], *, message: list[str] | None = None, upper: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Push a hand-built commit on HEAD.

    `PARENT` in a header stands for HEAD's id, `message` for the lines after the headers, and `upper`
    names the leading `tree` or `parent` line to write in upper-case hex.
    """
    tree = conftest.run_git('rev-parse', 'HEAD^{tree}', cwd=work).stdout.strip()
    parent = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    tree_id = tree.upper() if upper == 'tree' else tree
    parent_id = parent.upper() if upper == 'parent' else parent
    lines = [f'tree {tree_id}', f'parent {parent_id}', *(h.replace('PARENT', parent) for h in headers)]
    commit = _crafted(work, 'commit', '\n'.join([*lines, *(['', 'crafted'] if message is None else message), '']))
    conftest.run_git('update-ref', 'refs/heads/main', commit, cwd=work)
    return conftest.run_git('push', 'origin', 'main', cwd=work, check=False)


_JANE = 'Jane Curator <jane@example.org>'
_EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'


@pytest.mark.parametrize(
    ('extra', 'named'),
    [
        # git log re-decodes the whole object from `encoding`, so the note reads as a second author there.
        (['encoding UTF-7', f'x-note {_utf7(f"\nauthor {_JANE} 1700000000 -0000")}'], 'encoded as UTF-7, not UTF-8'),
        # isomorphic-git folds the line into committer, which it then cannot parse.
        ([f' {_JANE} 1 +0000'], 'continuation line under committer'),
        # isomorphic-git takes a name to the first space, so it reads this as a second author.
        (['authorX'], 'header line authorX, which has no value'),
        # git reads the first tree; isomorphic-git and the workbench the last, where a curator's edit would build.
        ([f'tree {_EMPTY_TREE}'], 'header tree after committer'),
        # git reads only the parents straight after the tree; the browser reads every parent line.
        (['parent PARENT'], 'header parent after committer'),
        # isomorphic-git lets a `message` header stand in for the body.
        (['message approved by Jane'], 'header message after committer'),
        # The workbench's own parser refuses a name it cannot decode.
        (['nöte x'], 'header name nöte, which is not printable ASCII'),
        # fsck checks only the leading author and committer; a reader taking the last sees these.
        ([f'committer {_ident(_JANE)}'], 'header committer after committer'),
        ([f'author {_ident(_JANE)}'], 'header author after committer'),
        (['gpgsig one', 'gpgsig two'], 'written with 2 gpgsig lines'),
    ],
    ids=[
        'utf-7 encoding',
        'continuation under committer',
        'header without a value',
        'trailing tree',
        'trailing parent',
        'message header',
        'non-ascii header name',
        'trailing committer',
        'trailing author',
        'two signatures',
    ],
)
def test_a_commit_outside_the_one_header_grammar_is_refused(
    unprotected: server.SheafGitServer,
    curator: conftest.GitRepo,
    tmp_path: pathlib.Path,
    extra: list[str],
    named: str,
) -> None:
    """On every server: git and the workbench have to read a new commit the same way, or at all."""
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')

    refused = _push_crafted_commit(work, [f'author {_ident(str(PUSHER))}', f'committer {_ident(str(PUSHER))}', *extra])

    assert refused.returncode != 0
    assert named in refused.stderr
    assert len(curator.history(REF)) == 1


@pytest.mark.parametrize(
    ('author', 'named'),
    [
        # The workbench reads the time after the last `>` as ` <digits> <zone>`, one space each.
        (f'{PUSHER} \t1 +0000', 'author line git does not write'),
        # Seventeen digits are past a JavaScript number's exact integers, so the browser reads another time.
        (f'{PUSHER} 12345678901234567 +0000', 'author line git does not write'),
    ],
    ids=['tab before the date', 'seventeen-digit date'],
)
def test_an_identity_line_the_workbench_cannot_read_is_refused(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path, author: str, named: str
) -> None:
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')

    refused = _push_crafted_commit(work, [f'author {author}', f'committer {_ident(str(PUSHER))}'])

    assert refused.returncode != 0
    assert named in refused.stderr
    assert len(curator.history(REF)) == 1


@pytest.mark.parametrize('field', ['tree', 'parent'])
def test_a_commit_naming_an_object_in_upper_case_hex_is_refused(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path, field: str
) -> None:
    """Git reads the id either way; isomorphic-git and the workbench look objects up by the lowercase form."""
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')

    refused = _push_crafted_commit(
        work, [f'author {_ident(str(PUSHER))}', f'committer {_ident(str(PUSHER))}'], upper=field
    )

    assert refused.returncode != 0
    assert f'written with the {field} id' in refused.stderr
    assert len(curator.history(REF)) == 1


def test_a_tag_naming_its_object_in_upper_case_hex_is_refused(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')
    target = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip().upper()
    tagger = f'tagger {_ident(str(PUSHER))}'
    tag = _crafted(work, 'tag', '\n'.join([f'object {target}', 'type commit', 'tag approved', tagger, '', 'ok', '']))

    refused = conftest.run_git('push', 'origin', f'{tag}:refs/tags/approved', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'written with the object id' in refused.stderr
    assert 'refs/tags/approved' not in curator.store.read().refs


def test_a_commit_with_no_blank_line_after_its_headers_is_refused_by_name(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """Git's fsck takes a header block with no message after it; isomorphic-git's split on the blank line does not."""
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')

    refused = _push_crafted_commit(
        work, [f'author {_ident(str(PUSHER))}', f'committer {_ident(str(PUSHER))}'], message=[]
    )

    assert refused.returncode != 0
    assert 'no blank line between its headers and its message' in refused.stderr
    assert 'which has no value' not in refused.stderr


def test_the_remedy_for_an_extra_header_rebuilds_a_commit_that_lands(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """`--amend` would keep the refused header; the remedy the refusal names has to drop it."""
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')
    refused = _push_crafted_commit(
        work, [f'author {_ident(str(PUSHER))}', f'committer {_ident(str(PUSHER))}', 'message x']
    )
    assert 'git reset --soft HEAD^ && git commit --allow-empty -C ORIG_HEAD' in refused.stderr

    conftest.run_git('reset', '--soft', 'HEAD^', cwd=work)
    conftest.run_git('commit', '--allow-empty', '-C', 'ORIG_HEAD', cwd=work)
    conftest.run_git('push', 'origin', 'main', cwd=work)

    assert len(curator.history(REF)) == 2


def test_the_commits_and_tags_stock_git_writes_all_land(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The positive control for the header grammar: every shape git's own porcelain writes passes it."""
    ssh_keygen = shutil.which('ssh-keygen')
    if ssh_keygen is None:
        raise RuntimeError('this test signs with an SSH key; ssh-keygen is not on PATH')
    key = tmp_path / 'signing-key'
    subprocess.run([ssh_keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True, timeout=60)
    signing = ('-c', 'gpg.format=ssh', '-c', f'user.signingkey={key}')
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')

    def lands(*refspecs: str) -> None:
        pushed = conftest.run_git('push', 'origin', *(refspecs or ('main',)), cwd=work, check=False)
        assert pushed.returncode == 0, pushed.stderr

    _commit(work, 'one.md', 'one\n')
    lands()
    tree = conftest.run_git('rev-parse', 'HEAD^{tree}', cwd=work).stdout.strip()
    plumbed = conftest.run_git('commit-tree', tree, '-p', 'HEAD', '-m', 'plumbed', cwd=work).stdout.strip()
    conftest.run_git('update-ref', 'refs/heads/main', plumbed, cwd=work)
    lands()
    _commit(work, 'two.md', 'two\n')
    conftest.run_git('commit', '--amend', '--no-edit', '-m', 'two, amended', cwd=work)
    lands()
    for side in ('left', 'right'):
        conftest.run_git('checkout', '-q', '-b', side, 'main', cwd=work)
        _commit(work, f'{side}.md', f'{side}\n')
    conftest.run_git('checkout', '-q', 'main', cwd=work)
    conftest.run_git('merge', '--no-edit', 'left', 'right', cwd=work)  # an octopus
    lands()
    conftest.run_git('revert', '--no-edit', 'HEAD^2', cwd=work)
    lands()
    conftest.run_git('checkout', '-q', '-b', 'picked', 'main', cwd=work)
    _commit(work, 'picked.md', 'picked\n')
    conftest.run_git('checkout', '-q', 'main', cwd=work)
    conftest.run_git('cherry-pick', 'picked', cwd=work)
    lands()
    conftest.run_git('tag', '-a', 'reviewed', '-m', 'reviewed', cwd=work)
    lands('refs/tags/reviewed')
    other = _clone(identified, tmp_path, 'other')
    _commit(other, 'other.md', 'other\n')
    conftest.run_git('push', 'origin', 'main', cwd=other)
    _commit(work, 'mine.md', 'mine\n')
    conftest.run_git('pull', '-q', '--rebase', 'origin', 'main', cwd=work)
    lands()
    (work / 'signed.md').write_text('signed\n', 'utf-8')
    conftest.run_git('add', 'signed.md', cwd=work)
    conftest.run_git(*signing, 'commit', '-S', '-m', 'an SSH-signed commit', cwd=work)
    lands()
    conftest.run_git('checkout', '-q', '-b', 'release', 'main', cwd=work)
    _commit(work, 'release.md', 'release\n')
    conftest.run_git(*signing, 'tag', '-s', 'v1', '-m', 'v1', cwd=work)
    conftest.run_git('checkout', '-q', 'main', cwd=work)
    conftest.run_git('merge', '--no-ff', '--no-edit', 'v1', cwd=work)
    assert b'mergetag ' in conftest.run_git('cat-file', 'commit', 'HEAD', cwd=work).stdout.encode()
    lands()


def test_a_signed_commit_s_continuation_lines_are_taken(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The positive control: `gpgsig` is a field whose value continues, and both readers fold it alike."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    signature = ['gpgsig -----BEGIN PGP SIGNATURE-----', ' ', ' iQEzBAABCAAdFiEE', ' -----END PGP SIGNATURE-----']

    pushed = _push_crafted_commit(
        work, [f'author {_ident(str(PUSHER))}', f'committer {_ident(str(PUSHER))}', *signature]
    )

    assert pushed.returncode == 0, pushed.stderr
    assert len(curator.history(REF)) == 2


def test_a_tag_with_a_header_before_its_tagger_is_refused(
    identified: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """Git's fsck checks `tagger` only straight after `tag`, so moved down it can carry a second ident."""
    _sign_off(curator, 'PM2')
    work = _clone(identified, tmp_path, 'work')
    target = conftest.run_git('rev-parse', 'HEAD', cwd=work).stdout.strip()
    tagger = f'tagger {PUSHER} 0 -0000 {_JANE} 1700000000 +1000'
    tag = _crafted(
        work, 'tag', '\n'.join([f'object {target}', 'type commit', 'tag approved', 'x-note z', tagger, '', 'ok', ''])
    )

    refused = conftest.run_git('push', 'origin', f'{tag}:refs/tags/approved', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'where git writes object, type, tag, tagger' in refused.stderr
    assert f'tagger line git does not write ({PUSHER} 0 -0000 {_JANE}' in refused.stderr
    assert 'refs/tags/approved' not in curator.store.read().refs


# --- names the workbench cannot read ---------------------------------------------------------------------


@pytest.fixture
def unprotected(backend: sheaf.LocalBackend, tmp_path: pathlib.Path) -> Iterator[server.SheafGitServer]:
    """A server configured with nothing: the name rule is not opt-in."""
    with server.SheafGitServer.over_backend(backend, tmp_path / 'bare', repos={REPO}) as instance:
        yield instance


@pytest.mark.parametrize('path', ['back\\slash.md', 'dir\\name/inside.md'], ids=['file', 'directory'])
def test_a_new_name_with_a_backslash_is_refused(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path, path: str
) -> None:
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')
    _commit(work, path, 'unreadable\n')

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert path in refused.stderr
    assert 'backslash' in refused.stderr
    assert len(curator.history(REF)) == 1


def test_a_later_rename_does_not_clear_the_commit_that_added_the_name(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """Every new commit is checked, so the tip's tree being readable is not enough; the history has to be."""
    _sign_off(curator, 'PM2')
    work = _clone(unprotected, tmp_path, 'work')
    _commit(work, 'back\\slash.md', 'unreadable\n')
    conftest.run_git('mv', 'back\\slash.md', 'back-slash.md', cwd=work)
    conftest.run_git('commit', '-m', 'rename', cwd=work)

    refused = conftest.run_git('push', 'origin', 'main', cwd=work, check=False)

    assert refused.returncode != 0
    assert 'back\\slash.md' in refused.stderr


def test_editing_a_stored_name_with_a_backslash_is_allowed(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """The rule is on new names: an edit leaves the directory no less readable than it already was."""
    curator.write_files(ref=REF, files={'back\\slash.md': 'stored\n'}, author=REVIEWER, message='store it')
    work = _clone(unprotected, tmp_path, 'work')
    (work / 'back\\slash.md').write_text('edited\n', 'utf-8')
    conftest.run_git('commit', '-qam', 'edit the stored name', cwd=work)

    conftest.run_git('push', 'origin', 'main', cwd=work)

    assert len(curator.history(REF)) == 2


def test_removing_a_name_with_a_backslash_is_allowed(
    unprotected: server.SheafGitServer, curator: conftest.GitRepo, tmp_path: pathlib.Path
) -> None:
    """A name another writer stored is the pusher's to clear: a deletion leaves nothing unreadable behind."""
    curator.write_files(ref=REF, files={'back\\slash.md': 'stored\n'}, author=REVIEWER, message='store it')
    work = _clone(unprotected, tmp_path, 'work')
    conftest.run_git('rm', '-q', 'back\\slash.md', cwd=work)
    conftest.run_git('commit', '-m', 'remove the unreadable name', cwd=work)

    conftest.run_git('push', 'origin', 'main', cwd=work)

    assert len(curator.history(REF)) == 2
