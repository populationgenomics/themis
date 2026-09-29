"""The ownership rule over git history: what a commit does to the user's judgements in each asset it changes.

For each path a commit writes differently from a parent, where the new version is a widget asset, the change has to keep
the ownership rule (`themis.widgets.ownership`) against every parent. A parent that holds no version of the path, or
one that is no asset of that type, is compared as a new asset, as a root commit is; a path whose new version is no
asset was removed, which the rule allows. Against every parent, not any: otherwise a merge whose parents the agent
chose, or whose conflict it resolved, could take a user's judgement from whichever side suits it. So a merge that takes
in a user's edit to an asset is refused wherever either side's version breaks the rule against the other, and the agent
rebases onto the user's commits instead.

Both versions are parsed without their validation rules, since passing them does not change what a user judged; an
asset of a widget type this build does not know is refused, since its fields cannot be checked.

`check` is the push hook's reading of one commit (the worker's `widget_check` runs it against the mirror);
`against_push` is the guest's reading of the next push, the commits it carries and the version about to be written,
which is how the asset helper and the working-document linter refuse what the hook would.
"""

from __future__ import annotations

import collections
import dataclasses
import functools
import pathlib
from collections.abc import Collection, Mapping, Sequence
from typing import Protocol

from google.protobuf import message

from themis.widgets import asset, ownership, tracking

_ABSENT_MODE = '000000'

REBASE_GUIDANCE = (
    "a merge is checked against each of its parents, so one that takes in a user's edits to an asset is refused "
    "wherever either side's version breaks the rule against the other: abort it (`git merge --abort`) and rebase onto "
    "the user's commits instead (`git pull --rebase`), then change the asset on top of them"
)
UNPUSHED_GUIDANCE = (
    "a commit the push carries already breaks a user's judgement, and the push is refused whatever is written "
    "now: rebase onto the user's commits (`git pull --rebase`), or reset onto the upstream (`git reset @{upstream}`) "
    'and commit the work again'
)


class Git(Protocol):
    """Runs git in one repository and returns its output; raises `RuntimeError` when git exits non-zero."""

    def __call__(self, *args: str, stdin: bytes | None = None) -> bytes: ...


@dataclasses.dataclass(frozen=True)
class _Entry:
    mode: str
    blob: str


def _diff(git: Git, parent: str | None, commit: str) -> dict[str, tuple[_Entry | None, _Entry | None]]:
    """Each path `commit` writes differently from `parent` (the empty tree for None): its entry there and here.

    Raises:
        RuntimeError: If the tree names a path twice: fsck refuses one, and which entry a reader takes is its own.
    """
    args = ['diff-tree', '-r', '-z', '--no-commit-id', '--no-renames']
    args += ['--root', commit] if parent is None else [parent, commit]
    fields = git(*args).split(b'\x00')
    changed: dict[str, tuple[_Entry | None, _Entry | None]] = {}
    for meta, raw_path in zip(fields[0:-1:2], fields[1::2], strict=True):
        old_mode, new_mode, old_blob, new_blob, _status = meta.decode().lstrip(':').split(' ')
        path = raw_path.decode('utf-8', 'surrogateescape')
        if path in changed:
            raise RuntimeError(f'{commit} lists {path} twice in its tree, which git does not write')
        changed[path] = (
            None if old_mode == _ABSENT_MODE else _Entry(old_mode, old_blob),
            None if new_mode == _ABSENT_MODE else _Entry(new_mode, new_blob),
        )
    return changed


def _require_single_entries(git: Git, commit: str, paths: Collection[str]) -> None:
    """Raise unless `commit`'s tree holds one entry at each of `paths`.

    A diff against a parent holding one of two entries at a path lists the other alone, so only the tree itself
    says there are two.

    Raises:
        RuntimeError: If the tree names one of `paths` twice: fsck refuses one, and which entry a reader takes is its
            own.
    """
    if not paths:
        return
    # The whole tree, not the paths as pathspecs: ls-tree reads pathspecs from argv alone, which a push of many
    # assets would overflow.
    listed = git('ls-tree', '-r', '-z', '--name-only', commit)
    entries = collections.Counter(raw.decode('utf-8', 'surrogateescape') for raw in listed.split(b'\x00') if raw)
    for path in sorted(paths):
        if entries[path] > 1:
            raise RuntimeError(f'{commit} lists {path} twice in its tree, which git does not write')


def _blobs(git: Git, ids: Collection[str]) -> dict[str, bytes]:
    """The content of each blob in `ids`, read through one `git cat-file --batch`.

    Raises:
        RuntimeError: If the repository lacks one, or git answers for another object than the one asked for.
    """
    if not ids:
        return {}
    ordered = sorted(ids)
    out = git('cat-file', '--batch', stdin=''.join(f'{blob}\n' for blob in ordered).encode())
    contents: dict[str, bytes] = {}
    position = 0
    for blob in ordered:
        header_end = out.index(b'\n', position)
        header = out[position:header_end].decode()
        if header == f'{blob} missing':
            raise RuntimeError(f'the repository holds no object {blob}, which a commit it holds names')
        name, kind, size = header.split(' ')
        if name != blob or kind != 'blob':
            raise RuntimeError(f'git cat-file answered {name} {kind} for {blob}')
        start = header_end + 1
        contents[blob] = out[start : start + int(size)]
        position = start + int(size) + 1
    return contents


def _file(entry: _Entry | None) -> _Entry | None:
    """`entry` if it is a regular file, the only entry that can be an asset; else None."""
    return entry if entry is not None and entry.mode in tracking.REGULAR_FILE_MODES else None


def _payload(entry: _Entry | None, contents: Mapping[str, bytes]) -> message.Message | None:
    """The asset an entry holds, parsed for the rule; None for no entry, no regular file, or no widget asset."""
    file = _file(entry)
    return None if file is None else asset.parse(contents[file.blob])


@dataclasses.dataclass(frozen=True)
class _Outcome:
    """What a commit does at one path: the fields it alters, or why a version there cannot be checked."""

    violations: list[ownership.Violation]
    unreadable: asset.AssetError | None = None


def _outcomes(git: Git, commit: str, parents: Sequence[str], only: str | None = None) -> dict[str, _Outcome]:
    """What `commit` does at each path it writes differently from a parent, or at `only`, in path order.

    New versions are read first, and a parent's version only where the new one is an asset.
    """
    diffs = [_diff(git, parent, commit) for parent in parents] or [_diff(git, None, commit)]
    paths = [path for path in sorted({path for diff in diffs for path in diff}) if only is None or path == only]
    heres = {path: next(diff[path][1] for diff in diffs if path in diff) for path in paths}
    new_contents = _blobs(git, {file.blob for file in map(_file, heres.values()) if file is not None})
    news: dict[str, message.Message] = {}
    outcomes: dict[str, _Outcome] = {}
    for path, here in heres.items():
        try:
            new = _payload(here, new_contents)
        except asset.AssetError as error:
            outcomes[path] = _Outcome([], error)
            continue
        if new is not None:
            news[path] = new
    _require_single_entries(git, commit, news)
    befores = {path: [diff[path][0] for diff in diffs if path in diff] for path in news}
    old_contents = _blobs(
        git, {file.blob for entries in befores.values() for file in map(_file, entries) if file is not None}
    )
    for path, new in news.items():
        outcomes[path] = _against_parents(new, befores[path], old_contents)
    return dict(sorted(outcomes.items()))


def _against_parents(new: message.Message, befores: Sequence[_Entry | None], contents: Mapping[str, bytes]) -> _Outcome:
    """What writing `new` does against each parent's entry at its path, a parent the path is unchanged against aside.

    That parent holds this very version, which keeps the rule against itself, so its diff lists no entry to compare.
    """
    try:
        olds = [ownership.same_payload(_payload(before, contents), new) for before in befores]
    except asset.AssetError as error:
        return _Outcome([], error)
    found: list[ownership.Violation] = []
    for old in olds:
        for violation in ownership.violations(old, new):
            if violation not in found:
                found.append(violation)
    return _Outcome(found)


def check(git: Git, commit: str, parents: Sequence[str]) -> list[str]:
    """Why `commit` may not land: each user's judgement an asset it changes breaks, empty when it keeps them all.

    Raises:
        RuntimeError: If git cannot read the commit or its parents, or the commit's tree names a path twice.
    """
    reasons: list[str] = []
    for path, outcome in _outcomes(git, commit, parents).items():
        if outcome.unreadable is not None:
            reasons.append(
                f"{path}: {outcome.unreadable}; the user's judgements in it cannot be checked, so the push is refused"
            )
            continue
        reasons.extend(ownership.describe(path, violation) for violation in outcome.violations)
        if len(parents) > 1 and outcome.violations:
            reasons.append(f'{path}: {REBASE_GUIDANCE}')
    return reasons


def committed(directory: pathlib.Path, path: str, revision: str) -> message.Message | None:
    """The payload the asset at `path`, relative to `directory`, holds at `revision`; None when it holds none.

    Parsed as the rule reads it, without the payload's validation rules.

    Raises:
        asset.AssetError: If the version there names a widget type this build does not know, or does not parse.
        tracking.TrackingError: If git cannot tell, as when `directory` is in no repository.
    """
    data = tracking.committed(directory, path, revision)
    return None if data is None else asset.parse(data)


@dataclasses.dataclass(frozen=True)
class Refusal:
    """A field the next push would alter: in a commit it carries, or in the version to be written (`commit` None)."""

    violation: ownership.Violation
    commit: str | None = None

    def explain(self) -> str:
        """`ownership.explain`, naming the commit that alters the field if one does."""
        said = ownership.explain(self.violation)
        return said if self.commit is None else f'in commit {self.commit[:12]}, {said}'


def against_push(directory: pathlib.Path, path: str, new: message.Message) -> list[Refusal]:
    """What the push hook would refuse at `path` once `new` is written there and committed on the current branch.

    The hook's reading, mirrored: each commit the push carries (those after the upstream as last fetched) against
    every parent, then `new` against the last commit. With no upstream, as on a detached HEAD, only the last commit
    is compared.

    Raises:
        asset.AssetError: If a merge is in progress (`REBASE_GUIDANCE`), or a version the push carries names a
            widget type this build does not know or does not parse.
        tracking.TrackingError: If git cannot tell, as when `directory` is in no repository.
    """
    if tracking.merging(directory):
        raise asset.AssetError(REBASE_GUIDANCE)
    git = functools.partial(tracking.git, directory)
    found: list[Refusal] = []
    for commit, parents in tracking.unpushed(directory):
        outcome = _outcomes(git, commit, parents, only=path).get(path)
        if outcome is None:
            continue
        if outcome.unreadable is not None:
            raise asset.AssetError(f'in commit {commit[:12]}, {outcome.unreadable}')
        found.extend(Refusal(violation, commit) for violation in outcome.violations)
    last = ownership.same_payload(committed(directory, path, 'HEAD'), new)
    found.extend(Refusal(violation) for violation in ownership.violations(last, new))
    return found
