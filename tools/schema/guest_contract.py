"""Generate the guest's contract tree from the marked rpcs (sandbox-rpc-exposure.md).

The guest — the sandboxed process the agent's code runs in — is handed the contract as it may use it and nothing
else: each proto declaring a marked rpc, cut down to the marked rpcs, the types they reach and the imports those
still need, with the marks themselves removed; and the Python stubs protoc generates from those cut sources. What
the model reads and what its calls run on are one artifact, so a stub cannot offer an rpc the hatch refuses and the
sources cannot describe one. Both halves are committed under
``themis/services/sandbox_worker/guest_contract/`` (``proto/`` and ``python/``) and held fresh like every other
generated file.

Every cut is made at the positions protoc itself recorded — the descriptor's source info gives each declaration's
span and the comments around it — never by parsing the text; what the cuts leave behind (an rpc body emptied of
its mark, runs of blank lines) is tidied by pattern. The cut sources are then compiled and checked against the
full contract before any stub is generated from them: the kept declarations, and every comment on them, have to
come through exactly, and a comment the cut would orphan or re-attach is refused. Files this repo does not author
ship intact — the upstream record schema copies, whose wheels ship their own stubs, and a dependency's protos,
whose stubs are generated here as they are for the main tree.
"""

from __future__ import annotations

import importlib.resources
import pathlib
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from typing import NamedTuple

from google.protobuf import descriptor_pb2, message
from grpc_tools import protoc

from tools.schema import agent_exposed

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GUEST_CONTRACT = _REPO_ROOT / 'themis' / 'services' / 'sandbox_worker' / 'guest_contract'
PROTO = 'proto'
PYTHON = 'python'
# grpcio-tools' bundled well-known-type protos; runtime-provided in the guest, so never shipped.
WELL_KNOWN = importlib.resources.files('grpc_tools') / '_proto'
_RUNTIME_PREFIX = 'google/protobuf/'
# What this repo authors, and therefore cuts. Anything else in the closure ships as it is.
_AUTHORED_PREFIX = 'themis/'
# The upstream record schemas: the wheel publishing each ships its own `_pb2`, which the guest installs, so
# generating one here would shadow it. Their sources still ship, for the model to read.
_WHEEL_PREFIXES = ('clinvar_proto/', 'pubmed_proto/')

# FileDescriptorProto field numbers, as a SourceCodeInfo path spells them.
_DEPENDENCY, _MESSAGE_TYPE, _ENUM_TYPE, _SERVICE = 3, 4, 5, 6
_METHOD = 2  # ServiceDescriptorProto.method
_METHOD_OPTIONS = 4  # MethodDescriptorProto.options

_EMPTY_BODY = re.compile(r'\)\s*\{\s*\}')
_BLANK_RUN = re.compile(r'\n{3,}')
_BLANK_BEFORE_CLOSE = re.compile(r'\n\n(\s*\})')


class _Extensions:
    """Where each custom option an options message can carry is declared, read off the image.

    An option set on a kept declaration is a use of the file declaring the extension, which a cut must not
    orphan. The extensions are read as the message library sees them, so the stubs declaring them have to be
    registered first: the two option files this repo carries are imported below. An option whose extension is
    registered by neither shows up as an unknown field and is not counted, and the import it needs is then cut —
    which protoc reports when it compiles the cut sources, so the miss is loud rather than silent.
    """

    def __init__(self, image: descriptor_pb2.FileDescriptorSet) -> None:
        # Imported here, not at module top: both stubs are (re)generated during the same regen run.
        from buf.validate import validate_pb2  # noqa: F401, PLC0415
        from themis.rpc import sandbox_options_pb2  # noqa: F401, PLC0415

        self._files: dict[str, str] = {}
        for file in image.file:
            prefix = f'{file.package}.' if file.package else ''
            for extension in file.extension:
                self._files[f'{prefix}{extension.name}'] = file.name
            for msg in file.message_type:
                self._nested(msg, prefix, file.name)

    def _nested(self, msg: descriptor_pb2.DescriptorProto, prefix: str, file: str) -> None:
        for extension in msg.extension:
            self._files[f'{prefix}{msg.name}.{extension.name}'] = file
        for nested in msg.nested_type:
            self._nested(nested, f'{prefix}{msg.name}.', file)

    def files_used_by(self, *options: message.Message) -> set[str]:
        """The files declaring every extension set on the given options messages.

        Raises:
            ValueError: If an extension set on the options is declared by no file in the image.
        """
        files = set()
        for each in options:
            for field, _ in each.ListFields():
                if not field.is_extension:
                    continue
                if field.full_name not in self._files:
                    raise ValueError(f'option {field.full_name} is set but declared by no file in the image')
                files.add(self._files[field.full_name])
        return files


class _Type(NamedTuple):
    """A message or enum: the file declaring it, the types its fields name, and the option files it uses."""

    file: str
    references: frozenset[str]
    option_files: frozenset[str]


def _types(image: descriptor_pb2.FileDescriptorSet, extensions: _Extensions) -> dict[str, _Type]:
    """Every message and enum in the image, nested ones included, keyed by fully-qualified name (leading dot).

    A type's option files are those of every option on it and inside it: the message's, its fields', its oneofs',
    an enum's and its values'.
    """
    types: dict[str, _Type] = {}

    def enum(prefix: str, declared: descriptor_pb2.EnumDescriptorProto, file: str) -> None:
        options = [declared.options, *(value.options for value in declared.value)]
        types[f'{prefix}.{declared.name}'] = _Type(file, frozenset(), frozenset(extensions.files_used_by(*options)))

    def walk(messages: Iterable[descriptor_pb2.DescriptorProto], prefix: str, file: str) -> None:
        for msg in messages:
            name = f'{prefix}.{msg.name}'
            options = [msg.options, *(f.options for f in msg.field), *(o.options for o in msg.oneof_decl)]
            types[name] = _Type(
                file,
                frozenset(field.type_name for field in msg.field if field.type_name),
                frozenset(extensions.files_used_by(*options)),
            )
            walk(msg.nested_type, name, file)
            for declared in msg.enum_type:
                enum(name, declared, file)

    for file in image.file:
        prefix = f'.{file.package}' if file.package else ''
        walk(file.message_type, prefix, file.name)
        for declared in file.enum_type:
            enum(prefix, declared, file.name)
    return types


class _Kept(NamedTuple):
    """One marked rpc, as declared: its service's file and descriptors."""

    file: descriptor_pb2.FileDescriptorProto
    service: descriptor_pb2.ServiceDescriptorProto
    method: descriptor_pb2.MethodDescriptorProto


def _kept_methods(
    image: descriptor_pb2.FileDescriptorSet, services: Iterable[agent_exposed.MarkedService]
) -> list[_Kept]:
    files = {file.name: file for file in image.file}
    kept = []
    for marked in services:
        file = files[marked.file]
        service = next(s for s in file.service if s.name == marked.name)
        kept.extend(_Kept(file, service, m) for m in service.method if m.name in marked.methods)
    return kept


def _parent(name: str) -> str:
    return name.rsplit('.', 1)[0]


def reachable_types(
    image: descriptor_pb2.FileDescriptorSet, services: Iterable[agent_exposed.MarkedService]
) -> set[str]:
    """Every type a marked rpc's request or response reaches, transitively, and everything declared around it.

    A message ships whole — the cut removes declarations, never fields — so reaching a type reaches the messages
    it is nested in, and reaching a message reaches every type nested in it, whose fields are then followed too.

    Raises:
        ValueError: If a marked rpc, or a field on the way, names a type the image does not declare.
    """
    types = _types(image, _Extensions(image))
    children: dict[str, list[str]] = {}
    for name in types:
        if _parent(name) in types:
            children.setdefault(_parent(name), []).append(name)
    kept = _kept_methods(image, services)
    stack = [name for k in kept for name in (k.method.input_type, k.method.output_type)]
    reached: set[str] = set()
    while stack:
        name = stack.pop()
        if name in reached:
            continue
        if name not in types:
            raise ValueError(f'{name} is reached from a marked rpc but not declared in the image')
        reached.add(name)
        stack.extend(types[name].references)
        stack.extend(children.get(name, ()))
        if _parent(name) in types:
            stack.append(_parent(name))
    return reached


class Closure(NamedTuple):
    """What the guest tree holds: the files, and per file what survives the cut."""

    files: tuple[str, ...]
    types: frozenset[str]
    methods: Mapping[str, Mapping[str, frozenset[str]]]  # file → service → kept rpc names


def agent_exposed_file() -> str:
    """The proto declaring the `agent_exposed` option: the one import every cut leaves unused."""
    from themis.rpc import sandbox_options_pb2  # noqa: PLC0415  (regenerated in the same run; see agent_exposed)

    return sandbox_options_pb2.DESCRIPTOR.name


def _agent_exposed_number() -> int:
    from themis.rpc import sandbox_options_pb2  # noqa: PLC0415

    return sandbox_options_pb2.agent_exposed.number


def _option_files_of(
    file: descriptor_pb2.FileDescriptorProto,
    types: Mapping[str, _Type],
    extensions: _Extensions,
    kept_types: frozenset[str],
    kept: Iterable[_Kept],
) -> set[str]:
    """The files whose extensions the kept declarations of `file` set as options, the mark's file excepted."""
    files = set()
    for name, declared in types.items():
        if declared.file == file.name and name in kept_types:
            files |= declared.option_files
    services_kept = {k.service.name for k in kept if k.file.name == file.name}
    files |= extensions.files_used_by(file.options, *(s.options for s in file.service if s.name in services_kept))
    files |= extensions.files_used_by(*(k.method.options for k in kept if k.file.name == file.name))
    return files - {agent_exposed_file()}


def closure(image: descriptor_pb2.FileDescriptorSet, services: list[agent_exposed.MarkedService]) -> Closure:
    """The files the guest tree ships: each marked rpc's file and every file the kept declarations reach."""
    extensions = _Extensions(image)
    types = _types(image, extensions)
    reached = frozenset(reachable_types(image, services))
    kept = _kept_methods(image, services)
    files = {s.file for s in services} | {types[t].file for t in reached}
    for file in image.file:
        if file.name in files:
            files |= _option_files_of(file, types, extensions, reached, kept)
    methods: dict[str, dict[str, frozenset[str]]] = {}
    for s in services:
        methods.setdefault(s.file, {})[s.name] = frozenset(s.methods)
    shipped = tuple(sorted(f for f in files if not f.startswith(_RUNTIME_PREFIX)))
    return Closure(shipped, reached, methods)


def _needed_imports(
    file: descriptor_pb2.FileDescriptorProto,
    types: Mapping[str, _Type],
    extensions: _Extensions,
    kept_types: frozenset[str],
    kept: list[_Kept],
) -> set[str]:
    """The files a cut file's surviving declarations still name, by type or by option extension."""
    needed = _option_files_of(file, types, extensions, kept_types, kept)
    for name, declared in types.items():
        if declared.file == file.name and name in kept_types:
            needed |= {types[ref].file for ref in declared.references}
    for k in kept:
        if k.file.name == file.name:
            needed |= {types[k.method.input_type].file, types[k.method.output_type].file}
    return needed - {file.name}


class _Cut(NamedTuple):
    """A region to delete: whole lines (`columns` None) or one span's characters within its lines."""

    start: int
    end: int
    columns: tuple[int, int] | None = None


def _span(location: descriptor_pb2.SourceCodeInfo.Location) -> tuple[int, int, int, int]:
    """A location's span as (start line, start column, end line, end column), all 0-based."""
    start_line, start_column = location.span[0], location.span[1]
    if len(location.span) == 4:
        return start_line, start_column, location.span[2], location.span[3]
    return start_line, start_column, start_line, location.span[2]


def _declaration_cut(location: descriptor_pb2.SourceCodeInfo.Location, lines: list[str], what: str, file: str) -> _Cut:
    """The whole-line cut of a doomed declaration: its span, the comment above it, a trailing comment below it.

    Raises:
        ValueError: If a comment sits above the declaration detached from it (it belongs to no declaration, so
            the cut cannot decide whether it stays), or if the comment above it is not `//` lines (a block comment
            has no line count to read off the descriptor).
    """
    start, _, end, _ = _span(location)
    if location.leading_detached_comments:
        raise ValueError(
            f'{file}: a detached comment sits above {what}, which the guest does not get; attach or move it'
        )
    leading = location.leading_comments.count('\n')
    if location.leading_comments and not leading:
        raise ValueError(f'{file}: the comment above {what} is not `//` lines; the cut counts lines by them')
    for line in lines[start - leading : start]:
        if not line.lstrip().startswith('//'):
            raise ValueError(f'{file}: the comment above {what} is not `//` lines; the cut counts lines by them')
    if location.trailing_comments and '//' not in lines[end]:
        end += location.trailing_comments.count('\n')  # the trailing comment is on the lines below the span
    return _Cut(start - leading, end)


def _cuts(
    file: descriptor_pb2.FileDescriptorProto,
    lines: list[str],
    kept_types: frozenset[str],
    kept_methods: Mapping[str, frozenset[str]],
    needed_imports: set[str],
    exposed_number: int,
) -> list[_Cut]:
    """Every region to delete from `file`'s text: doomed declarations whole, the marks by their exact span."""
    prefix = f'.{file.package}' if file.package else ''
    cuts: list[_Cut] = []
    for location in file.source_code_info.location:
        path = tuple(location.path)
        what = None
        if len(path) == 2:
            kind, index = path
            if kind == _MESSAGE_TYPE and f'{prefix}.{file.message_type[index].name}' not in kept_types:
                what = f'message {file.message_type[index].name}'
            elif kind == _ENUM_TYPE and f'{prefix}.{file.enum_type[index].name}' not in kept_types:
                what = f'enum {file.enum_type[index].name}'
            elif kind == _SERVICE and not kept_methods.get(file.service[index].name):
                what = f'service {file.service[index].name}'
            elif kind == _DEPENDENCY and file.dependency[index] not in needed_imports:
                what = f'import {file.dependency[index]}'
        elif len(path) == 4 and path[0] == _SERVICE and path[2] == _METHOD:
            service = file.service[path[1]]
            if service.method[path[3]].name not in kept_methods.get(service.name, frozenset()):
                what = f'rpc {service.name}.{service.method[path[3]].name}'
        elif len(path) == 6 and (path[0], path[2], path[4], path[5]) == (
            _SERVICE,
            _METHOD,
            _METHOD_OPTIONS,
            exposed_number,
        ):
            # The mark itself, cut to the character: it may share its line with the rpc it marks.
            start, start_column, end, end_column = _span(location)
            leading = location.leading_comments.count('\n')
            if leading:
                cuts.append(_Cut(start - leading, start - 1))
            cuts.append(_Cut(start, end, (start_column, end_column)))
        if what is not None:
            cuts.append(_declaration_cut(location, lines, what, file.name))
    return cuts


def _apply(lines: list[str], cuts: list[_Cut]) -> str:
    """Delete the cuts from the lines: spans within their lines first, then whole lines, merging overlaps."""
    lines = list(lines)
    emptied: list[_Cut] = []
    for cut in sorted((c for c in cuts if c.columns), key=lambda c: (c.start, c.columns), reverse=True):
        start_column, end_column = cut.columns or (0, 0)
        if cut.start == cut.end:
            lines[cut.start] = lines[cut.start][:start_column] + lines[cut.start][end_column:]
        else:
            lines[cut.start] = lines[cut.start][:start_column] + '\n'
            for line in range(cut.start + 1, cut.end):
                lines[line] = '\n'
            lines[cut.end] = lines[cut.end][end_column:]
        for line in range(cut.start, cut.end + 1):
            if not lines[line].strip():
                emptied.append(_Cut(line, line))
    merged: list[tuple[int, int]] = []
    for start, end, _ in sorted(c for c in [*cuts, *emptied] if not c.columns):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        del lines[start : end + 1]
    return ''.join(lines)


def _tidy(text: str) -> str:
    """Collapse what a cut leaves behind: an rpc body emptied of its mark, and runs of blank lines."""
    text = _EMPTY_BODY.sub(');', text)
    text = _BLANK_RUN.sub('\n\n', text)
    return _BLANK_BEFORE_CLOSE.sub(r'\n\1', text)


def cut_file(
    image: descriptor_pb2.FileDescriptorSet,
    file: descriptor_pb2.FileDescriptorProto,
    source: str,
    services: list[agent_exposed.MarkedService],
    kept_types: frozenset[str],
) -> str:
    """The guest's copy of one authored proto: the marked rpcs, the types they reach, the imports still needed."""
    extensions = _Extensions(image)
    types = _types(image, extensions)
    kept = [k for k in _kept_methods(image, services) if k.file.name == file.name]
    kept_methods = {s.name: frozenset(s.methods) for s in services if s.file == file.name}
    needed = _needed_imports(file, types, extensions, kept_types, kept)
    lines = source.splitlines(keepends=True)
    text = _apply(lines, _cuts(file, lines, kept_types, kept_methods, needed, _agent_exposed_number()))
    header = f'// @generated by tools.schema.regen from schema/proto/{file.name} — do not edit.\n'
    return header + _tidy(text)


def _protoc(
    proto_dir: pathlib.Path, files: list[str], *out: str, python_dir: pathlib.Path | None = None, grpc: bool = False
) -> None:
    if not files:
        return
    args = ['protoc', f'--proto_path={proto_dir}', f'--proto_path={WELL_KNOWN}', *out]
    if python_dir is not None:
        args.append(f'--grpc_python_out={python_dir}' if grpc else f'--python_out={python_dir}')
    if protoc.main([*args, *files]) != 0:
        raise SystemExit(f'protoc failed over the guest contract tree for {files}')


def compile_tree(proto_dir: pathlib.Path, files: list[str]) -> descriptor_pb2.FileDescriptorSet:
    """The cut sources as protoc reads them, source info included."""
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / 'tree.binpb'
        _protoc(proto_dir, files, f'--descriptor_set_out={out}', '--include_source_info', '--include_imports')
        image = descriptor_pb2.FileDescriptorSet()
        image.ParseFromString(out.read_bytes())
        return image


_Comments = tuple[str, str, tuple[str, ...]]


def comments_by_declaration(file: descriptor_pb2.FileDescriptorProto) -> dict[str, _Comments]:
    """Each top-level declaration's and rpc's comments, keyed by what it declares, off the file's source info."""
    found = {}
    for location in file.source_code_info.location:
        path = tuple(location.path)
        key = None
        if len(path) == 2 and path[0] == _MESSAGE_TYPE:
            key = f'message {file.message_type[path[1]].name}'
        elif len(path) == 2 and path[0] == _ENUM_TYPE:
            key = f'enum {file.enum_type[path[1]].name}'
        elif len(path) == 2 and path[0] == _SERVICE:
            key = f'service {file.service[path[1]].name}'
        elif len(path) == 4 and path[0] == _SERVICE and path[2] == _METHOD:
            service = file.service[path[1]]
            key = f'rpc {service.name}.{service.method[path[3]].name}'
        if key is not None:
            found[key] = (
                location.leading_comments,
                location.trailing_comments,
                tuple(location.leading_detached_comments),
            )
    return found


def orphaned_comments(text: str, file: descriptor_pb2.FileDescriptorProto) -> set[str]:
    """The `//` comment lines of `text` that protoc attached to no declaration in `file`, its compiled form.

    A comment protoc attaches to nothing belongs to no declaration the cut can carry it off with, so after a cut
    it stands beside whatever comes next as if it described it. The contract's own text is held to the same rule:
    a comment has to be one protoc attaches to a declaration.
    """
    attached = ''.join(
        location.leading_comments + location.trailing_comments + ''.join(location.leading_detached_comments)
        for location in file.source_code_info.location
    )
    return {
        stripped
        for line in text.splitlines()
        if line.lstrip().startswith('//') and (stripped := line.lstrip()[2:].strip()) and stripped not in attached
    }


def verify(
    image: descriptor_pb2.FileDescriptorSet,
    tree: descriptor_pb2.FileDescriptorSet,
    shipped: Closure,
    cut_sources: Mapping[str, str],
) -> None:
    """Hold the cut sources to the full contract: exactly the kept declarations, each with its own comments.

    Args:
        image: The full module, source info included.
        tree: The cut sources compiled back, source info included.
        shipped: The closure the tree was cut to.
        cut_sources: Each cut file's text, by name; these are the files held to the contract.

    Raises:
        ValueError: On any authored file whose cut declares an rpc, type or service the guest should not get,
            lacks one it should, carries a comment other than the one the full contract puts on it, or leaves a
            comment that a cut declaration carried and protoc now attaches to nothing.
    """
    full = {file.name: file for file in image.file}
    for file in tree.file:
        if file.name not in cut_sources:
            continue
        stranded = orphaned_comments(cut_sources[file.name], file)
        if stranded:
            raise ValueError(f'{file.name}: the cut leaves comments beside nothing they describe: {sorted(stranded)}')
        prefix = f'.{file.package}'
        kept_methods = shipped.methods.get(file.name, {})
        declared = {s.name: {m.name for m in s.method} for s in file.service}
        if declared != {s: set(m) for s, m in kept_methods.items()}:
            raise ValueError(f'{file.name}: the cut declares {declared}, the marks keep {dict(kept_methods)}')
        types = {f'{prefix}.{m.name}' for m in file.message_type} | {f'{prefix}.{e.name}' for e in file.enum_type}
        expected = {t for t in shipped.types if _parent(t) == prefix}
        if types != expected:
            raise ValueError(f'{file.name}: the cut declares {sorted(types)}, the kept rpcs reach {sorted(expected)}')
        before, after = comments_by_declaration(full[file.name]), comments_by_declaration(file)
        for key, comments in after.items():
            if comments != before[key]:
                raise ValueError(f'{file.name}: {key} carries other comments in the cut than in the contract')


def generate(
    image_bytes: bytes,
    export: pathlib.Path,
    root: pathlib.Path = GUEST_CONTRACT,
    services: list[agent_exposed.MarkedService] | None = None,
) -> Closure:
    """Write the guest contract tree under `root` from a `build_image()` descriptor set and its `buf export`.

    Args:
        image_bytes: The module as `buf build` compiled it, source info included.
        export: The directory `buf export` materialised the module's sources and dependencies into.
        root: Where `proto/` and `python/` are written; both are replaced whole.
        services: The marked services, when the caller has read them off the same image already.

    Returns:
        The closure the tree was cut to.

    Raises:
        ValueError: If the cut sources do not declare exactly what the marks keep (see `verify`).
        SystemExit: If protoc cannot compile the cut sources.
    """
    image = agent_exposed.image_of(image_bytes)
    services = services if services is not None else agent_exposed.marked_services(image_bytes)
    shipped = closure(image, services)
    files = {file.name: file for file in image.file}
    proto_dir, python_dir = root / PROTO, root / PYTHON
    for directory in (proto_dir, python_dir):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
    cut_sources: dict[str, str] = {}
    for name in shipped.files:
        source = (export / name).read_text(encoding='utf-8')
        if name.startswith(_AUTHORED_PREFIX):
            source = cut_sources[name] = cut_file(image, files[name], source, services, shipped.types)
        target = proto_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding='utf-8')
    verify(image, compile_tree(proto_dir, sorted(cut_sources)), shipped, cut_sources)
    generated = [name for name in shipped.files if not name.startswith(_WHEEL_PREFIXES)]
    _protoc(proto_dir, generated, python_dir=python_dir)
    _protoc(proto_dir, [name for name in generated if name in shipped.methods], python_dir=python_dir, grpc=True)
    return shipped
