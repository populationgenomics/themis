"""What a push may not do: write outside its refs, rewrite or delete history, or write what is not its to write.

The history rules hold for every repository and are not configurable, and so does one rule on names:
no new path may carry a backslash, which the workbench's git library cannot read. What the pushing side
may write — which paths, and under whose name — is a `Protection` built from the process environment
rather than from anything in the repository, so a push cannot relax the policy it is being checked
against. Design: `docs/design/sheaf.md`.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
from typing import override

from themis.sheaf import store as store_mod
from themis.sheaf.wire import bare, reflog

PATHS_ENV = 'SHEAF_PROTECTED_PATHS'
PUSHER_NAME_ENV = 'SHEAF_PUSHER_NAME'
PUSHER_EMAIL_ENV = 'SHEAF_PUSHER_EMAIL'
SEPARATOR = ':'
# Work a writer could not land on its branch, kept under a ref of its own for a later merge.
STRANDED_NAMESPACE = 'refs/stranded'
# The refs a push may write. Any other is refused, `refs/replace/` among them: it changes what git reads an object as.
WRITABLE_NAMESPACES = ('refs/heads/', 'refs/tags/', f'{STRANDED_NAMESPACE}/')

_HISTORY_REMEDY = 'history here is append-only: push only branches and tags, and fast-forward them.'
_PROTECTED_REMEDY = (
    'take each protected path out of the unpushed commit that writes it; a later commit that reverts '
    'it does not help, because every new commit is checked.'
)
_UNREADABLE_REMEDY = (
    'rename each path with a backslash in the unpushed commit that adds it; a later rename does not help, '
    'because every new commit is checked.'
)
# `cat-file --batch` heads each object with `<oid> <type> <size>`.
_BATCH_INFO_FIELDS = 3
_TIP_REMEDY = 'point every ref you push at a commit, or at an annotated tag of one.'
# An ident line's value as git writes it, `Name <email> <seconds> <zone>`.
# Matched whole, so nothing trails it. One `<` and one `>`, since the workbench reads the time after the last `>`;
# single spaces; and a date of at most sixteen digits with no leading zero, within a JavaScript number's precision.
_IDENT_LINE = re.compile(rb'^(?P<who>[^<>\n]* <[^<>\n]*>) (?:0|[1-9][0-9]{0,15}) [+-][0-9]{4}$')
# An object id as git writes one; isomorphic-git and the workbench look objects up by this exact lowercase form.
_OBJECT_ID = re.compile(rb'^[0-9a-f]{40}$')
# The identity fields each kind of object carries.
_IDENT_FIELDS = {'commit': (b'author', b'committer'), 'tag': (b'tagger',)}
# The only fields whose value continues on lines that start with a space.
_CONTINUED_FIELDS = frozenset({b'gpgsig', b'gpgsig-sha256', b'mergetag'})
# A tag's header exactly as `git tag -a` writes it; fsck checks `tagger` only in this place.
_TAG_FIELDS = (b'object', b'type', b'tag', b'tagger')
# The one encoding every reader takes a commit in: isomorphic-git decodes UTF-8 whatever the header says.
_UTF8 = frozenset({b'utf-8', b'utf8'})
_FORM_REMEDY = (
    'rebuild each such object with git itself rather than by hand: for the last commit, if it is not a '
    'merge, `git reset --soft HEAD^ && git commit --allow-empty -C ORIG_HEAD` (`--amend` keeps the headers '
    'that were refused); otherwise `git rebase --force-rebase --rebase-merges <upstream>` (`--root` in place '
    'of <upstream> when the branch has never been pushed); and for a tag `git tag -f -a <name>`.'
)
# A header name as git writes one: printable ASCII, no space.
_FIELD_NAME = re.compile(rb'^[\x21-\x7e]+$')
# What may follow a commit's committer line, and how often; git writes these and nothing else there.
_COMMIT_TRAILING_ONCE = frozenset({b'encoding', b'gpgsig', b'gpgsig-sha256'})
_COMMIT_TRAILING_REPEATED = frozenset({b'mergetag'})


@dataclasses.dataclass(frozen=True)
class Violation:
    """One reason a push is refused, and what the pusher does about it."""

    reason: str
    remedy: str


@dataclasses.dataclass(frozen=True)
class Identity:
    """A name and email as git records them on a commit or a tag.

    Raises:
        ValueError: If either is empty or holds a character git cannot record in an ident.
    """

    name: str
    email: str

    def __post_init__(self) -> None:
        for part in (self.name, self.email):
            if not part or any(c in part for c in '<>\n\0'):
                raise ValueError(f'{part!r} is not something git can record in an identity')

    @override
    def __str__(self) -> str:
        return f'{self.name} <{self.email}>'


@dataclasses.dataclass(frozen=True)
class Header:
    """An object's header block as its raw bytes record it.

    `fields` are `(name, value)` in order, continuation lines dropped; `malformed` describes each line
    the one grammar a new object is held to refuses.
    """

    fields: list[tuple[bytes, bytes]]
    malformed: list[str]

    def values(self, name: bytes) -> list[bytes]:
        """Every value of the field `name`, in order."""
        return [value for field, value in self.fields if field == name]


@dataclasses.dataclass(frozen=True)
class Protection:
    """What the pushing side may write: which paths are off limits, and under whose name.

    Patterns are `fnmatch` globs, so `*` crosses `/`: `annotations/*` covers everything beneath it. They
    match in any case, because a curator's clone may sit on a case-insensitive filesystem.
    `pusher` is the pusher's own identity — the agent's, behind the sandbox worker — which every new
    commit's author and committer, and every new tag's tagger, must carry, name and email both, so
    one naming anyone else is refused. Both are opt-in, and the storage layer stays free of them:
    empty paths protect nothing, and no pusher accepts any identity.

    Raises:
        ValueError: If a pattern is not casefolded, or contains `SEPARATOR`. Colons are legal in POSIX paths, and the
            patterns reach the hook as one separator-joined environment variable each, so a pattern
            carrying one would arrive as two that match nothing — the protection silently gone on
            the boundary the whole fabrication defence rests on.
    """

    paths: tuple[str, ...] = ()
    pusher: Identity | None = None

    def __post_init__(self) -> None:
        splittable = sorted(p for p in self.paths if SEPARATOR in p)
        if splittable:
            raise ValueError(f'a protection pattern may not contain {SEPARATOR!r}: {splittable}')
        # The path is folded to match; folding a pattern would also rewrite its bracket ranges ([A-z], [ß]).
        unfolded = sorted(p for p in self.paths if p != p.casefold())
        if unfolded:
            raise ValueError(f'a protection pattern is matched in any case, so it is written casefolded: {unfolded}')

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Protection:
        """Read protection from the environment the server passed through.

        Args:
            env: Mapping to read instead of `os.environ`.

        Raises:
            ValueError: If the environment names the pusher's name without its email or the other way
                round, or either is not something git can record; half an identity is a server
                misbuilt, not one checking nothing.
        """
        env = dict(os.environ if env is None else env)
        name, email = env.get(PUSHER_NAME_ENV), env.get(PUSHER_EMAIL_ENV)
        if (name is None) != (email is None):
            raise ValueError(f'{PUSHER_NAME_ENV} and {PUSHER_EMAIL_ENV} are set together or not at all')
        return cls(
            paths=tuple(p for p in env.get(PATHS_ENV, '').split(SEPARATOR) if p),
            pusher=None if name is None or email is None else Identity(name, email),
        )

    def as_env(self) -> dict[str, str]:
        """Render for passing to the hook."""
        pusher = {} if self.pusher is None else {PUSHER_NAME_ENV: self.pusher.name, PUSHER_EMAIL_ENV: self.pusher.email}
        return {PATHS_ENV: SEPARATOR.join(self.paths), **pusher}

    def forbids(self, path: str) -> bool:
        """Whether `path` is off limits, in any case: a case-insensitive filesystem opens `.MailMap` as `.mailmap`."""
        folded = path.casefold()
        return any(fnmatch.fnmatchcase(folded, pattern) for pattern in self.paths)


def introduced_paths(repo: bare.BareRepo, commit: str) -> list[str]:
    """Paths where `commit` differs from every parent.

    For an ordinary commit this is its diff; for a merge it is `git diff-tree -c`, the combined
    diff, so content taken unchanged from one side does not count as introduced by the merge — a
    merge that brings in a protected file verbatim has to pass, or the pushing side can never merge.

    `--root` is what makes that allowance safe. Without it git prints nothing for a parentless
    commit, so an orphan root can carry a protected path unseen and a merge resolved in its favour
    inherits the same silence — neither commit differs from every parent.

    Raises:
        RuntimeError: If git cannot read the commit.
    """
    return _diff_tree_paths(repo, commit)


def added_paths(repo: bare.BareRepo, commit: str) -> list[str]:
    """The paths `commit` adds: absent from every parent, present in its tree.

    An edit to a path that already exists is not an addition, so a name another writer stored stays
    editable.

    Raises:
        RuntimeError: If git cannot read the commit.
    """
    return _diff_tree_paths(repo, commit, '--diff-filter=A')


def _diff_tree_paths(repo: bare.BareRepo, commit: str, *options: str) -> list[str]:
    # -z, because otherwise git C-quotes any path with a byte outside printable ASCII and the
    # quoted form (leading '"') matches no glob; surrogateescape so an arbitrary-byte path still
    # reaches the glob intact.
    out = bare.git(
        'diff-tree', '-r', '-c', '--root', '--no-commit-id', '--name-only', '-z', *options, commit, cwd=repo.path
    )
    return [entry.decode('utf-8', 'surrogateescape') for entry in out.split(b'\x00') if entry]


def new_commits(repo: bare.BareRepo, tip: str) -> list[str]:
    """Commits reachable from `tip` that the store does not already have.

    `--all` is the mirror's pre-update refs, because a pre-receive hook runs before any ref moves.

    Raises:
        RuntimeError: If git cannot walk from the tip.
    """
    out = bare.git('rev-list', tip, '--not', '--all', cwd=repo.path)
    return [line for line in out.decode().splitlines() if line]


def new_tags(repo: bare.BareRepo, tip: str) -> list[str]:
    """New annotated tag objects reachable from `tip`: the tip, if it is one, and every tag it peels through.

    New means the store does not already have it.

    Raises:
        RuntimeError: If git cannot walk from the tip.
    """
    # The filter drops trees and blobs but not commits, so the listing is typed and the tags kept.
    listed = bare.git(
        'rev-list', '--objects', '--no-object-names', '--filter=object:type=tag', tip, '--not', '--all', cwd=repo.path
    )
    if not listed.strip():
        return []
    typed = bare.git('cat-file', '--batch-check=%(objectname) %(objecttype)', cwd=repo.path, stdin=listed)
    return [oid for oid, kind in (line.split() for line in typed.decode().split('\n') if line) if kind == 'tag']


def headers(repo: bare.BareRepo, oids: list[str]) -> dict[str, Header]:
    """Each object's header block as its raw bytes record it.

    Read from the object itself rather than through a formatter: `git log` reports the last of two
    `author` lines, `for-each-ref` the first, and isomorphic-git the last tagger, and fsck accepts a
    repeated header in either, so only a count taken from the bytes says what every reader sees. No
    re-encoding either: the bytes compared are the bytes stored, whatever `encoding` header they carry.

    Raises:
        RuntimeError: If git cannot read an object.
    """
    if not oids:
        return {}
    out = bare.git('cat-file', '--batch', cwd=repo.path, stdin=''.join(f'{oid}\n' for oid in oids).encode())
    found = {}
    position = 0
    for oid in oids:
        end = out.index(b'\n', position)
        info = out[position:end].split()
        if len(info) != _BATCH_INFO_FIELDS or info[0].decode() != oid:
            raise RuntimeError(f'git could not read {oid}: {out[position:end]!r}')
        size = int(info[2])
        found[oid] = _fields(out[end + 1 : end + 1 + size])
        position = end + 1 + size + 1
    return found


def _fields(body: bytes) -> Header:
    fields: list[tuple[bytes, bytes]] = []
    malformed = []
    block, blank, _ = body.partition(b'\n\n')
    if not blank:
        malformed.append('written with no blank line between its headers and its message')
        block = block.removesuffix(b'\n')
    for line in block.split(b'\n'):
        if line.startswith(b' '):
            under = fields[-1][0] if fields else b''
            if under not in _CONTINUED_FIELDS:
                malformed.append(f'written with a continuation line under {_shown(under) or "nothing"}')
            continue
        name, space, value = line.partition(b' ')
        if not space:
            # isomorphic-git takes a field's name to the first space and would read `authorX` as `author`.
            malformed.append(f'written with the header line {_shown(line)}, which has no value')
            continue
        if not _FIELD_NAME.match(name):
            malformed.append(f'written with the header name {_shown(name)}, which is not printable ASCII')
            continue
        fields.append((name, value))
    return Header(fields, malformed)


def _shown(raw: bytes) -> str:
    return raw.decode('utf-8', 'backslashreplace')


def violations(repo: bare.BareRepo, updates: dict[str, store_mod.RefUpdate], protection: Protection) -> list[Violation]:
    """Return why the push must be refused, empty if it is allowed.

    History first, for every ref: nothing outside `WRITABLE_NAMESPACES`, no deletion, no rewrite.
    These are what make the store append-only, and they are the hook's to enforce rather than
    receive-pack's — `receive.denyNonFastForwards` and `receive.denyDeletes` are checked *after*
    the pre-receive hook, and only for branches, so relying on them would publish the rewrite before
    git refused it. Then each commit and annotated tag the store does not have yet: under whose name
    it was made, what a commit introduces at a protected path — compared against every parent, so a
    merge taking the other side's edit verbatim passes — and whether it adds a name the workbench
    cannot read. An object already in the store is never rechecked, so a merge bringing in another
    writer's commit passes.

    Raises:
        RuntimeError: If git cannot walk the pushed commits, in which case nothing is decided and
            the push must not be accepted.
    """
    found = []
    commits: dict[str, None] = {}
    tags: dict[str, None] = {}
    for ref, update in sorted(updates.items()):
        found.extend(Violation(reason, _HISTORY_REMEDY) for reason in history_reasons(repo, ref, update))
        if update.new is None:
            continue
        if not _peels_to_commit(repo, update.new):
            found.append(
                Violation(f'{ref} points at {update.new[:12]}, which is not a commit or a tag of one', _TIP_REMEDY)
            )
            continue
        commits.update(dict.fromkeys(new_commits(repo, update.new)))
        tags.update(dict.fromkeys(new_tags(repo, update.new)))
    recorded = headers(repo, [*commits, *tags])
    found.extend(_form_violations(recorded, list(commits), list(tags)))
    if protection.pusher is not None:
        found.extend(_identity_violations(recorded, list(commits), list(tags), protection.pusher))
    for commit in commits:
        found.extend(_path_violations(repo, commit, protection))
    return found


def _peels_to_commit(repo: bare.BareRepo, tip: str) -> bool:
    try:
        bare.git('rev-parse', '--verify', '--quiet', f'{tip}^{{commit}}', cwd=repo.path)
    except RuntimeError:
        return False
    return True


def _path_violations(repo: bare.BareRepo, commit: str, protection: Protection) -> list[Violation]:
    found = []
    if protection.paths:
        offending = sorted(p for p in introduced_paths(repo, commit) if protection.forbids(p))
        if offending:
            found.append(Violation(f'{commit[:12]} writes protected {", ".join(offending)}', _PROTECTED_REMEDY))
    # isomorphic-git refuses to parse a tree holding such a name, and with it every other entry there.
    unreadable = sorted(p for p in added_paths(repo, commit) if '\\' in p)
    if unreadable:
        found.append(
            Violation(
                f'{commit[:12]} adds {", ".join(unreadable)}: a backslash in a file or directory name '
                'makes the directory holding it unreadable to the workbench',
                _UNREADABLE_REMEDY,
            )
        )
    return found


def _form_violations(recorded: dict[str, Header], commits: list[str], tags: list[str]) -> list[Violation]:
    """Refuse a new object outside the one header grammar git and isomorphic-git both read alike.

    Held on every server, like the name rule: an object the browser reads differently from git, or
    cannot parse, breaks every walk of history through it, curators' publishes included.
    """
    found = []
    for commit in commits:
        header = recorded[commit]
        problems = [*header.malformed, *_commit_sequence_problems(header), *_ident_shape_problems(header, 'commit')]
        problems += [
            f'written with the {_shown(field)} id {_shown(value)}, where git writes forty lowercase hex digits'
            for field, value in header.fields
            if field in (b'tree', b'parent') and not _OBJECT_ID.match(value)
        ]
        if problems:
            found.append(Violation(f'commit {commit[:12]} is {"; ".join(problems)}', _FORM_REMEDY))
    for tag in tags:
        header = recorded[tag]
        problems = [*header.malformed, *_ident_shape_problems(header, 'tag')]
        problems += [
            f'written with the object id {_shown(value)}, where git writes forty lowercase hex digits'
            for value in header.values(b'object')
            if not _OBJECT_ID.match(value)
        ]
        names = tuple(name for name, _ in header.fields)
        if names != _TAG_FIELDS:
            problems.append(
                f'written with the headers {", ".join(_shown(n) for n in names)}, '
                f'where git writes {", ".join(n.decode() for n in _TAG_FIELDS)}'
            )
        if problems:
            found.append(Violation(f'tag {tag[:12]} is {"; ".join(problems)}', _FORM_REMEDY))
    return found


def _ident_shape_problems(header: Header, kind: str) -> list[str]:
    return [
        f'written with a {_shown(field)} line git does not write ({_shown(value)})'
        for field in _IDENT_FIELDS[kind]
        for value in header.values(field)
        if not _IDENT_LINE.match(value)
    ]


def _commit_sequence_problems(header: Header) -> list[str]:
    """`tree`, `parent`*, `author`, `committer`, then only what git writes after them.

    git reads the first `tree` and the parents directly after it; isomorphic-git and the workbench
    read the last tree, every parent line and the last author, and fsck checks only the leading
    block. A commit in this one order is the same commit to all of them.
    """
    names = [name for name, _ in header.fields]
    position = 0
    expected = [b'tree']
    if names[position : position + 1] == [b'tree']:
        position += 1
        while names[position : position + 1] == [b'parent']:
            position += 1
        expected = [b'author', b'committer']
        if names[position : position + 2] == expected:
            position += 2
            expected = []
    if expected:
        shown = ', '.join(_shown(n) for n in names)
        return [f'written with the headers {shown}, where git writes tree, parent..., author, committer first']
    problems = []
    trailing = names[position:]
    for name in dict.fromkeys(trailing):
        if name in _COMMIT_TRAILING_ONCE and trailing.count(name) > 1:
            problems.append(f'written with {trailing.count(name)} {_shown(name)} lines')
        elif name not in _COMMIT_TRAILING_ONCE and name not in _COMMIT_TRAILING_REPEATED:
            problems.append(f'written with the header {_shown(name)} after committer, where git writes none')
    problems += [
        f'encoded as {_shown(value)}, not UTF-8' for value in header.values(b'encoding') if value.lower() not in _UTF8
    ]
    return problems


def _identity_violations(
    recorded: dict[str, Header], commits: list[str], tags: list[str], pusher: Identity
) -> list[Violation]:
    commit_remedy = (
        f'commits pushed here are authored and committed as {pusher}: drop any other name or email you set '
        '(user.name, user.email, GIT_AUTHOR_*, GIT_COMMITTER_*, --author), then redo the last commit with '
        '`git commit --amend --no-edit --reset-author`, or every unpushed one with '
        '`git rebase --exec "git commit --amend --no-edit --reset-author" <upstream>` '
        '(`--root` in place of <upstream> when the branch has never been pushed).'
    )
    tag_remedy = (
        f'annotated tags pushed here are tagged as {pusher}: drop any other name or email you set, then '
        'recreate the tag with `git tag -f -a <name>` and push it again.'
    )
    expected = f'{pusher.name} <{pusher.email}>'.encode()
    found = []
    for commit in commits:
        header = recorded[commit]
        problems = [
            *_ident_problems(header.values(b'author'), 'author', 'authored', expected),
            *_ident_problems(header.values(b'committer'), 'committer', 'committed', expected),
        ]
        if problems:
            found.append(Violation(f'{commit[:12]} is {" and ".join(problems)}, not {pusher}', commit_remedy))
    for tag in tags:
        header = recorded[tag]
        problems = _ident_problems(header.values(b'tagger'), 'tagger', 'tagged', expected)
        if problems:
            found.append(Violation(f'tag {tag[:12]} is {" and ".join(problems)}, not {pusher}', tag_remedy))
    return found


def _ident_problems(values: list[bytes], field: str, verb: str, expected: bytes) -> list[str]:
    if len(values) != 1:
        return [f'written with {len(values)} {field} lines']
    match = _IDENT_LINE.match(values[0])
    if match is None:
        return []  # the form rule refuses the line on every server
    if match['who'] != expected:
        return [f'{verb} as {_shown(match["who"])}']
    return []


def history_reasons(repo: bare.BareRepo, ref: str, update: store_mod.RefUpdate) -> list[str]:
    """Refuse any ref outside `WRITABLE_NAMESPACES`, and deleting or rewriting any ref."""
    if ref.startswith(reflog.NAMESPACE):
        return [f'{ref} is written by sheaf, not by a push']
    if not ref.startswith(WRITABLE_NAMESPACES):
        return [f'{ref} is not a ref a push may write (those under {", ".join(WRITABLE_NAMESPACES)})']
    if update.new is None:
        return [f'{ref} may not be deleted: history here is append-only']
    if update.old is None:
        return []
    try:
        bare.git('merge-base', '--is-ancestor', update.old, update.new, cwd=repo.path)
    except RuntimeError as exc:
        # `--is-ancestor` exits 1 for "not an ancestor" and 128 for an unreadable object, and both
        # arrive as one RuntimeError -- so git's own message is carried through rather than
        # reporting an unreadable repository as a rewrite.
        return [f'{ref} may only fast-forward (rewriting it would drop commits): {exc}']
    return []
