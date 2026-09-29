"""Check every ``::embed[<path>]`` a working document holds against the file it names (document-widgets.md).

The document is parsed as markdown by markdown-it-py, extended with the leaf-directive grammar the browser's
remark-directive reads: a line holding ``::name[label]`` and at most an attribute block after it. So an
``::embed`` inside a fenced or indented code block is text here as it is in the browser, and neither checks nor
draws it. The label is read as the raw characters between its brackets, which is also what the browser resolves.

Each embed has to name a path the asset contract admits, resolve inside the document's directory segment by
segment, hold an asset `themis.widgets.asset.decode` accepts, and be a regular file git tracks, since the browser
reads the committed revision and nothing else. It has to keep the user's judgements, its schema's guards, as the push
hook will hold it to (`themis.widgets.commits.against_push`): against the asset's version in the last commit, and with
no commit the push carries breaking one.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import stat
import unicodedata

import markdown_it
from google.protobuf import message
from markdown_it.rules_block import state_block

from themis.widgets import asset, commits, tracking

_DIRECTIVE = 'leaf_directive'
_EMBED = 'embed'
# The block rules a leaf directive may interrupt, as a fence does; `table` too, since remark-gfm ends a table there.
_INTERRUPTS = ['paragraph', 'reference', 'blockquote', 'list', 'table']
_MAX_BRACKET_DEPTH = 32
# A line whose content, inside any quote and list markers and indentation, opens with the `::embed` directive.
_OPENS_EMBED = re.compile(r'^(?:[ \t]*(?:>|[-*+](?=[ \t])|\d{1,9}[.)](?=[ \t])))*[ \t]*::embed(?=[\[{\s]|$)')


@dataclasses.dataclass(frozen=True)
class _Directive:
    name: str
    label: str | None
    # Written as `::name[label]` and nothing else on the line: no attribute block.
    exact: bool


@dataclasses.dataclass(frozen=True)
class Embed:
    """One ``::embed[<path>]`` the browser draws: the line it stands on, and its label as written."""

    line: int
    path: str


@dataclasses.dataclass(frozen=True)
class Reading:
    """The embeds a document draws, and each other line opening with ``::embed`` outside a fence, by number."""

    embeds: list[Embed]
    stray: list[int]


def read(markdown: str) -> Reading:
    """Read ``markdown`` as the browser does: every top-level ``::embed[<path>]``, and every other ``::embed`` line.

    The browser draws an embed only written exactly as ``::embed[<path>]`` on a line of its own at the top level
    of the document, and draws a placeholder, or the line's own text, for anything else opening with ``::embed``.
    """
    parser = markdown_it.MarkdownIt('commonmark').enable('table')
    parser.block.ruler.before('table', _DIRECTIVE, _leaf_directive, {'alt': _INTERRUPTS})
    tokens = parser.parse(markdown)
    embeds = []
    fenced: set[int] = set()
    for token in tokens:
        if token.map is None:
            continue
        if token.type == 'fence':
            fenced.update(range(token.map[0], token.map[1]))
        directive = token.meta.get(_DIRECTIVE)
        if (
            token.type == _DIRECTIVE
            and isinstance(directive, _Directive)
            and directive.name == _EMBED
            and directive.exact
            and directive.label is not None
            and token.level == 0
        ):
            embeds.append(Embed(token.map[0] + 1, directive.label))
    drawn = {embed.line for embed in embeds}
    stray = [
        number
        for number, line in enumerate(markdown.splitlines(), start=1)
        if number - 1 not in fenced and number not in drawn and _OPENS_EMBED.match(line)
    ]
    return Reading(embeds, stray)


def issues(markdown: str, directory: pathlib.Path) -> list[str]:
    """Why each ``::embed`` in ``markdown`` would not draw, resolving its path against ``directory``."""
    reading = read(markdown)
    found = [
        f'line {line}: an ::embed draws only written exactly as ::embed[<path>] on a line of its own, at the top '
        'level of the document (outside any list, quote, footnote or container)'
        for line in reading.stray
    ]
    for embed in reading.embeds:
        problem = _problem(embed, directory)
        if problem is not None:
            found.append(f'line {embed.line}: ::embed[{embed.path}] {problem}')
    return sorted(found, key=_line_of)


def _line_of(issue: str) -> int:
    return int(issue.split(':', 1)[0].removeprefix('line '))


def _problem(embed: Embed, directory: pathlib.Path) -> str | None:
    if not embed.path:
        return 'names no path'
    try:
        asset.check_path(embed.path)
        payload = asset.decode(_resolve(directory, embed.path).read_bytes())
    except asset.AssetError as error:
        return f'does not draw: {error}'
    return _untracked(embed.path, directory) or _breaks_judgements(embed.path, payload, directory)


def _breaks_judgements(path: str, payload: message.Message, directory: pathlib.Path) -> str | None:
    """Why the push hook would refuse the asset for breaking a user's judgement, or None when it would not."""
    try:
        refusals = commits.against_push(directory, path, payload)
    except (asset.AssetError, tracking.TrackingError) as error:
        return f"cannot be checked against the user's judgements: {error}"
    if not refusals:
        return None
    return "does not keep the user's judgements: " + '; '.join(refusal.explain() for refusal in refusals)


def _untracked(path: str, directory: pathlib.Path) -> str | None:
    """Why the push would not carry the asset at ``path``, or None when it would."""
    try:
        mode = tracking.index_mode(directory, path)
        if mode is None:
            if tracking.ignored(directory, path):
                return 'is ignored by git, so no push carries it; write it outside the ignored paths'
            return "is not in git's index, so no push carries it yet; `git add` it"
    except tracking.TrackingError as error:
        return f'cannot be checked against git: {error}'
    if mode not in tracking.REGULAR_FILE_MODES:
        return f'is recorded in git with mode {mode}, not as a regular file'
    return None


def _resolve(directory: pathlib.Path, path: str) -> pathlib.Path:
    """The regular file ``path`` names under ``directory``, walked segment by segment as a git tree is.

    Raises:
        asset.AssetError: If a segment is missing or a symbolic link, one before the last is not a directory, or
            the last is not a regular file.
    """
    current = directory
    segments = path.split('/')
    for depth, segment in enumerate(segments, start=1):
        current = current / segment
        walked = '/'.join(segments[:depth])
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise asset.AssetError(f'{walked} does not exist') from error
        if stat.S_ISLNK(mode):
            raise asset.AssetError(f'{walked} is a symbolic link')
        last = depth == len(segments)
        if not last and not stat.S_ISDIR(mode):
            raise asset.AssetError(f'{walked} is not a directory')
        if last and not stat.S_ISREG(mode):
            raise asset.AssetError(f'{walked} is not a file')
    return current


def _leaf_directive(state: state_block.StateBlock, start_line: int, end_line: int, silent: bool) -> bool:  # noqa: ARG001
    """The micromark leaf-directive construct: ``::name``, an optional ``[label]``, an optional ``{attributes}``."""
    if state.is_code_block(start_line):
        return False
    begin = state.bMarks[start_line] + state.tShift[start_line]
    line = state.src[begin : state.eMarks[start_line]]
    directive = _parse(line)
    if directive is None:
        return False
    if silent:
        return True
    state.line = start_line + 1
    token = state.push(_DIRECTIVE, '', 0)
    token.map = [start_line, state.line]
    token.meta[_DIRECTIVE] = directive
    return True


def _parse(line: str) -> _Directive | None:
    if not line.startswith('::'):
        return None
    name_end = _name_end(line, 2)
    if name_end is None:
        return None
    position, label = name_end, None
    if line[position : position + 1] == '[':
        closed = _label_end(line, position + 1)
        if closed is not None:
            label, position = line[position + 1 : closed], closed + 1
    rest = line[position:]
    exact = not rest.strip(' \t')
    # An attribute block, which micromark reads as far as its closing brace; nothing may follow that.
    if not exact and not (rest.startswith('{') and '}' in rest and not rest[rest.rindex('}') + 1 :].strip(' \t')):
        return None
    return _Directive(line[2:name_end], label, exact)


def _punctuation(character: str) -> bool:
    return unicodedata.category(character)[0] in 'PS'


def _name_end(line: str, start: int) -> int | None:
    """Where the directive name starting at ``start`` ends, or None when none starts there (micromark's rule)."""
    if start >= len(line) or line[start].isspace() or _punctuation(line[start]):
        return None
    end = start + 1
    while end < len(line) and not line[end].isspace() and (not _punctuation(line[end]) or line[end] in '-_'):
        end += 1
    return None if line[end - 1] in '-_' else end


def _label_end(line: str, start: int) -> int | None:
    """The index of the ``]`` closing a label whose text starts at ``start``, or None when the label never closes."""
    depth = 0
    position = start
    while position < len(line):
        character = line[position]
        if character == '\\' and line[position + 1 : position + 2] in ('[', '\\', ']'):
            position += 2
            continue
        if character == '[':
            depth += 1
            if depth > _MAX_BRACKET_DEPTH:
                return None
        elif character == ']':
            if depth == 0:
                return position
            depth -= 1
        position += 1
    return None
