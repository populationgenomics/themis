"""Writing a widget asset: a typed block the working document draws where it says ``::embed[<path>]``.

Guest-side: shipped into the guest rootfs as ``themis.agent.widgets``. Build the payload message from
``themis.widgets.models`` (its schema is ``/usr/local/share/themis/widgets/<name>.proto``), and ``write`` it at a path
in the workspace; the document then names that path, relative to itself. A field the schema marks ``guard``, such as a
checklist item's ``checked``, is a user's judgement on the rest of its element: only the user sets it, and a change to
what it judges clears it. To revise an asset a user may have worked on, ``update`` it: it carries each judgement
across from the last commit, matched by each element's key, where what the judgement is about is unchanged, and
reports each one the change clears. ``python -m themis.document_linter <document>`` checks every ``::embed`` the
document holds against the file it names.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Callable

from google.protobuf import message

from themis.widgets import asset, commits, ownership, tracking

WORKSPACE = pathlib.Path('/workspace')


def _embed_path(path: str, workspace: pathlib.Path) -> str:
    """The asset's path from the workspace root, which is what a document at the root embeds.

    Taken relative to the root, or absolute inside it. What must hold is where the file lies and what names it: inside
    the repository, so a push carries it, and at a path an ``::embed`` can name, so the document can draw it.

    Raises:
        themis.widgets.asset.AssetError: If an absolute path lies outside the workspace, or the path from the root is
            not one an ``::embed`` may name.
    """
    given = pathlib.PurePosixPath(path)
    if given.is_absolute():
        root = pathlib.PurePosixPath(workspace)
        if not given.is_relative_to(root):
            raise asset.AssetError(f'{path!r} is outside the workspace {root}, so no push would carry it')
        path = given.relative_to(root).as_posix()
    asset.check_path(path)
    return path


def write(path: str, payload: message.Message, *, workspace: pathlib.Path = WORKSPACE) -> pathlib.Path:
    """Validate ``payload``, wrap it in an ``Any`` and write it at ``path`` in the workspace, for ``git add``.

    Args:
        path: Where the asset goes, relative to the workspace root or absolute inside it:
            ``assets/checklist.binpb`` is embedded from a document at the root as ``::embed[assets/checklist.binpb]``.
        payload: A widget payload message, e.g. a ``checklist_pb2.Checklist``.
        workspace: The repository root the path is relative to.

    Returns:
        The file written.

    Raises:
        themis.widgets.asset.AssetError: If the path is not one an ``::embed`` may name or git ignores it, the
            message is not a widget payload type or fails its rules, or the push hook would refuse the next push at
            the path: against the last commit's version it sets, changes or keeps a user's judgement where only a
            change to what it judges may clear it (use `update`), or a commit the push carries already does. Nothing
            is written.
        themis.widgets.tracking.TrackingError: If git cannot tell whether it ignores the path.
    """
    path = _embed_path(path, workspace)
    return _write(path, payload, workspace)


def update(
    path: str,
    build: Callable[[message.Message | None], message.Message],
    *,
    workspace: pathlib.Path = WORKSPACE,
) -> pathlib.Path:
    """Rewrite the asset at ``path`` from the version the last commit holds, keeping each judgement that still holds.

    Each judgement the change clears is reported on stderr, one line each, such as ``items[ps3].checked cleared: its
    label changed; the user reviews it again``.

    Args:
        path: The asset's path, relative to the workspace root or absolute inside it.
        build: Given a copy of the payload the last commit holds (None when it holds none), returns the new
            payload; it may change the copy in place and return it. Build it from your own work, leaving every guard
            at its default: for each element it keeps under the same key,
            ``update`` carries the user's judgements across where what they judge is unchanged, so a tick on an item
            whose label and citation you left alone stays, and one on an item you reworded or re-cited is cleared
            for the user to review again. An element left out is removed, and the user's review of it with it.
        workspace: The repository root the path is relative to.

    Returns:
        The file written.

    Raises:
        themis.widgets.asset.AssetError: As `write` does, if the new payload sets a guard on an element of its own,
            or while a merge is in progress: rebase onto the user's commits instead. Nothing is written.
        themis.widgets.tracking.TrackingError: If git cannot read the workspace.
    """
    path = _embed_path(path, workspace)
    if tracking.merging(workspace):
        raise asset.AssetError(commits.REBASE_GUIDANCE)
    before = commits.committed(workspace, path, 'HEAD')
    built = build(None if before is None else _copy(before))
    same = ownership.same_payload(before, built)
    carried, cleared = ownership.carry(same, built)
    if before is not None and same is None:
        cleared = ownership.cleared_all(before, ownership.RETYPED)
    target = _write(path, carried, workspace)
    given = {} if before is None else dict(ownership.judged_values(before))
    for judgement in cleared:
        print(f'[themis.agent.widgets] {path}: {judgement}{_written(given.get(judgement.path))}', file=sys.stderr)
    return target


def read(path: str, *, workspace: pathlib.Path = WORKSPACE) -> message.Message:
    """The payload the asset at ``path`` holds in the workspace, decoded, for reading it back.

    Args:
        path: The asset's path, relative to the workspace root or absolute inside it.
        workspace: The repository root the path is relative to.

    Raises:
        themis.widgets.asset.AssetError: If the path is not one an ``::embed`` may name, or the file is no asset
            this build draws.
        FileNotFoundError: If no file is at the path.
    """
    path = _embed_path(path, workspace)
    return asset.decode((workspace / path).read_bytes())


def judgements(path: str, *, workspace: pathlib.Path = WORKSPACE) -> list[tuple[str, object]]:
    """Print, and return, every judgement a user has made in the asset the last commit holds at ``path``.

    Read these at the start of a turn: a tick says the user reviewed what it sits on, and a note is the user's own
    words about it, such as ``codes[CLN_DNV].note = 'The clinic letter confirms parentage.'``. Nothing starts a turn
    when a user writes one, so a note waits here until a turn reads it.

    Args:
        path: The asset's path, relative to the workspace root or absolute inside it.
        workspace: The repository root the path is relative to.

    Returns:
        Each judgement, where it is as ``codes[CLN_DNV].note``, and its value; empty when the last commit holds no
        asset at the path.

    Raises:
        themis.widgets.tracking.TrackingError: If the workspace's history cannot be read.
    """
    path = _embed_path(path, workspace)
    committed = commits.committed(workspace, path, 'HEAD')
    found = [] if committed is None else ownership.judged_values(committed)
    for where, value in found:
        print(f'[themis.agent.widgets] {path}: {where} = {value!r}')
    return found


def _written(value: object) -> str:
    """What a cleared note said, for the agent to read before it is gone: a note clears when what it judges changes."""
    if isinstance(value, str) and value:
        return f'. The user had written: {value!r}'
    return ''


def _copy(payload: message.Message) -> message.Message:
    copied = type(payload)()
    copied.CopyFrom(payload)
    return copied


def _write(path: str, payload: message.Message, workspace: pathlib.Path) -> pathlib.Path:
    if tracking.ignored(workspace, path):
        raise asset.AssetError(f'{path!r} is ignored by git, so no push would carry it')
    refusals = commits.against_push(workspace, path, payload)
    if refusals:
        lines = [f'{path}: {refusal.explain()}' for refusal in refusals]
        if any(refusal.commit is not None for refusal in refusals):
            lines.append(commits.UNPUSHED_GUIDANCE)
        lines.append(
            "(`update` carries the user's judgements across from the last commit; pull first if the push was refused)"
        )
        raise asset.AssetError('\n'.join(lines))
    data = asset.encode(payload)
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target
