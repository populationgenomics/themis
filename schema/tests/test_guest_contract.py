"""Test the guest contract tree generator (``tools.schema.guest_contract``).

The reachability walk is checked on hand-built descriptors. The cut itself is checked on a hand-written proto
compiled with source info, since every rpc in the module's exposed files is marked and the real tree therefore
exercises no cut of an rpc, type or service: the fixture has an unmarked rpc, a wholly unmarked service, unreached
types with comments, a mark sharing its rpc's line, and an import only a doomed type used. Then the tree is
generated into a temporary root from the same ``buf`` image and export ``regen`` uses, and its properties are read
back off the sources it cut and the stubs it generated. Each property is one a wrong cut would silently break in
the sandbox, where no test runs: a stub offering an rpc the hatch refuses, a type a kept rpc needs but the tree
lacks, a kept rpc carrying another rpc's comment.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from google.protobuf import descriptor_pb2

from themis.rpc import sandbox_options_pb2
from tools.schema import agent_exposed, guest_contract

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_needs_buf = pytest.mark.skipif(shutil.which('buf') is None, reason='buf not on PATH')
_STUB_METHOD = re.compile(r'^\s+self\.(\w+) = channel\.', re.MULTILINE)
_SERVICER_METHOD = re.compile(r'^    def (\w+)\(self, request', re.MULTILINE)


# --- the reachability walk, on hand-built descriptors ---------------------------------------------------------


def _marked(file: str, package: str, service: str, *methods: str) -> agent_exposed.MarkedService:
    return agent_exposed.MarkedService(file, package, service, methods)


def _hand_built_image() -> descriptor_pb2.FileDescriptorSet:
    """Two files: a service whose one marked rpc reaches a nested type, a map value and an imported enum."""
    string, enum, msg = (
        descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
        descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
    )
    shared = descriptor_pb2.FileDescriptorProto(name='themis/shared.proto', package='themis.shared', syntax='proto3')
    shared.enum_type.add(name='Kind').value.add(name='KIND_UNSPECIFIED', number=0)
    shared.message_type.add(name='Unused').field.add(name='x', number=1, type=string)
    papers = descriptor_pb2.FileDescriptorProto(name='themis/papers.proto', package='themis.papers', syntax='proto3')
    papers.dependency.append('themis/shared.proto')
    request = papers.message_type.add(name='ReadRequest')
    request.field.add(name='kind', number=1, type=enum, type_name='.themis.shared.Kind')
    response = papers.message_type.add(name='ReadResponse')
    response.nested_type.add(name='Detail').field.add(name='text', number=1, type=string)
    entry = response.nested_type.add(name='ByIdEntry')
    entry.options.map_entry = True
    entry.field.add(name='key', number=1, type=string)
    entry.field.add(name='value', number=2, type=msg, type_name='.themis.papers.ReadResponse.Detail')
    response.field.add(
        name='by_id',
        number=1,
        type=msg,
        type_name='.themis.papers.ReadResponse.ByIdEntry',
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
    )
    papers.message_type.add(name='LocateRequest').field.add(name='q', number=1, type=string)
    papers.message_type.add(name='LocateResponse')
    service = papers.service.add(name='Papers')
    service.method.add(name='Read', input_type='.themis.papers.ReadRequest', output_type='.themis.papers.ReadResponse')
    service.method.add(
        name='Locate', input_type='.themis.papers.LocateRequest', output_type='.themis.papers.LocateResponse'
    )
    return descriptor_pb2.FileDescriptorSet(file=[shared, papers])


def test_reachability_follows_fields_map_values_and_imports_and_keeps_parents() -> None:
    image = _hand_built_image()
    marked = _marked('themis/papers.proto', 'themis.papers', 'Papers', 'Read')
    reached = guest_contract.reachable_types(image, [marked])
    assert reached == {
        '.themis.papers.ReadRequest',
        '.themis.papers.ReadResponse',
        '.themis.papers.ReadResponse.ByIdEntry',
        '.themis.papers.ReadResponse.Detail',
        '.themis.shared.Kind',
    }


def test_closure_ships_the_files_the_kept_declarations_reach_and_no_other() -> None:
    image = _hand_built_image()
    marked = _marked('themis/papers.proto', 'themis.papers', 'Papers', 'Read')
    shipped = guest_contract.closure(image, [marked])
    assert shipped.files == ('themis/papers.proto', 'themis/shared.proto')
    assert '.themis.shared.Unused' not in shipped.types
    assert dict(shipped.methods) == {'themis/papers.proto': {'Papers': frozenset({'Read'})}}


def test_a_kept_rpc_naming_a_type_outside_the_image_fails_loud() -> None:
    image = _hand_built_image()
    image.file[1].service[0].method[0].input_type = '.elsewhere.Missing'
    marked = _marked('themis/papers.proto', 'themis.papers', 'Papers', 'Read')
    with pytest.raises(ValueError, match='not declared in the image'):
        guest_contract.reachable_types(image, [marked])


# --- the cut, on a hand-written proto compiled with source info ---------------------------------------------

_OPTIONS_PROTO = _REPO_ROOT / 'schema' / 'proto' / 'themis' / 'rpc' / 'sandbox_options.proto'

_SHARED = """\
syntax = "proto3";

package t.shared;

// Only a doomed message uses this.
enum Kind {
  KIND_UNSPECIFIED = 0;
}
"""

_PAPERS = """\
// The papers service, as a fixture.
syntax = "proto3";

package t.papers;

import "google/protobuf/timestamp.proto";
import "t/shared.proto";
import "themis/rpc/sandbox_options.proto";

// The kept request.
message ReadRequest {
  string id = 1;
}

// The kept response: one nested type a field names, one it does not.
message ReadResponse {
  message Detail {
    string text = 1;
  }
  message Extra {
    Aside aside = 1;
  }
  Detail detail = 1;
  google.protobuf.Timestamp at = 2;  // when
}

// Reached only through a nested type nothing names.
message Aside {
  string s = 1;
}

// Doomed: only Locate takes it.
message LocateRequest {
  t.shared.Kind kind = 1;
}

// Doomed too.
message LocateResponse {}  // and its trailing comment with it

// A doomed enum.
enum Mode {
  MODE_UNSPECIFIED = 0;
}

// The service.
service Papers {
  // Read: marked, and carrying a second option.
  rpc Read(ReadRequest) returns (ReadResponse) {
    option (themis.rpc.agent_exposed) = true;
    option deprecated = true;
  }
  // Peek: the mark shares the rpc's line.
  rpc Peek(ReadRequest) returns (ReadResponse) { option (themis.rpc.agent_exposed) = true; }
  // Locate: the browser's, not the agent's.
  rpc Locate(LocateRequest) returns (LocateResponse);
}

// A service the agent never reaches.
service Admin {
  rpc Reset(ReadRequest) returns (ReadResponse);
}
"""

_MARKED = agent_exposed.MarkedService('t/papers.proto', 't.papers', 'Papers', ('Read', 'Peek'))


def _fixture(tmp_path: pathlib.Path, papers: str = _PAPERS) -> tuple[descriptor_pb2.FileDescriptorSet, pathlib.Path]:
    """The fixture protos written under `tmp_path` and compiled with source info, plus the include root."""
    include = tmp_path / 'src'
    (include / 't').mkdir(parents=True, exist_ok=True)
    (include / 'themis' / 'rpc').mkdir(parents=True, exist_ok=True)
    (include / 't' / 'shared.proto').write_text(_SHARED, 'utf-8')
    (include / 't' / 'papers.proto').write_text(papers, 'utf-8')
    (include / 'themis' / 'rpc' / 'sandbox_options.proto').write_bytes(_OPTIONS_PROTO.read_bytes())
    return guest_contract.compile_tree(include, ['t/papers.proto']), include


class _Cut(NamedTuple):
    """A fixture cut: the full image, the guest's text, and that text compiled back."""

    image: descriptor_pb2.FileDescriptorSet
    text: str
    tree: descriptor_pb2.FileDescriptorSet

    def verify(self) -> None:
        shipped = guest_contract.closure(self.image, [_MARKED])
        guest_contract.verify(self.image, self.tree, shipped, {'t/papers.proto': self.text})


def _cut(tmp_path: pathlib.Path, papers: str = _PAPERS) -> _Cut:
    """`papers.proto` as the guest would get it, and the cut source compiled back."""
    image, _ = _fixture(tmp_path, papers)
    shipped = guest_contract.closure(image, [_MARKED])
    file = next(f for f in image.file if f.name == 't/papers.proto')
    text = guest_contract.cut_file(image, file, papers, [_MARKED], shipped.types)
    out = tmp_path / 'out'
    (out / 't').mkdir(parents=True, exist_ok=True)
    (out / 't' / 'papers.proto').write_text(text, 'utf-8')
    return _Cut(image, text, guest_contract.compile_tree(out, ['t/papers.proto']))


def test_the_cut_keeps_exactly_the_marked_rpcs_and_what_they_reach(tmp_path: pathlib.Path) -> None:
    cut = _cut(tmp_path)
    text = cut.text
    (papers,) = [f for f in cut.tree.file if f.name == 't/papers.proto']
    assert {s.name: [m.name for m in s.method] for s in papers.service} == {'Papers': ['Read', 'Peek']}
    assert {m.name for m in papers.message_type} == {'ReadRequest', 'ReadResponse', 'Aside'}
    assert not papers.enum_type, 'the doomed enum survived'
    assert list(papers.dependency) == ['google/protobuf/timestamp.proto'], 'an import nothing kept names survived'
    assert 'agent_exposed' not in text
    assert 'option deprecated = true;' in text, 'a second option on a marked rpc was cut with the mark'
    assert 'rpc Peek(ReadRequest) returns (ReadResponse);' in text, 'the inline mark did not collapse to `;`'
    for gone in ('Locate', 'Mode', 'Admin', 'Reset', 'its trailing comment with it', 'shared'):
        assert gone not in text, f'{gone!r} survived the cut'


def test_every_kept_declaration_keeps_its_own_comments(tmp_path: pathlib.Path) -> None:
    cut = _cut(tmp_path)
    before = guest_contract.comments_by_declaration(next(f for f in cut.image.file if f.name == 't/papers.proto'))
    after = guest_contract.comments_by_declaration(next(f for f in cut.tree.file if f.name == 't/papers.proto'))
    assert set(after) == {
        'message ReadRequest',
        'message ReadResponse',
        'message Aside',
        'service Papers',
        'rpc Papers.Read',
        'rpc Papers.Peek',
    }
    for key, comments in after.items():
        assert comments == before[key], key


def test_the_cut_is_held_to_the_contract(tmp_path: pathlib.Path) -> None:
    _cut(tmp_path).verify()


def test_a_comment_the_cut_would_re_attach_is_refused(tmp_path: pathlib.Path) -> None:
    """A comment protoc attaches to nothing in the contract is one a cut cannot carry off with its declaration.

    Left behind, it stands beside the next kept declaration, and protoc then reads it as that declaration's; the
    verification sees the declaration carrying a comment the contract never put on it.
    """
    papers = _PAPERS.replace(
        'message LocateResponse {}  // and its trailing comment with it',
        'message LocateResponse {}\n// A comment protoc attaches to nothing.',
    )
    with pytest.raises(ValueError, match='carries other comments'):
        _cut(tmp_path, papers).verify()


def test_a_detached_comment_above_a_doomed_declaration_is_refused(tmp_path: pathlib.Path) -> None:
    papers = _PAPERS.replace(
        '// Doomed too.\nmessage LocateResponse {}', '// A section header.\n\nmessage LocateResponse {}'
    )
    with pytest.raises(ValueError, match='detached comment'):
        _cut(tmp_path, papers)


def test_a_block_comment_above_a_doomed_declaration_is_refused(tmp_path: pathlib.Path) -> None:
    papers = _PAPERS.replace(
        '// Doomed too.\nmessage LocateResponse {}', '/* Doomed too. */\nmessage LocateResponse {}'
    )
    with pytest.raises(ValueError, match='not `//` lines'):
        _cut(tmp_path, papers)


# --- the cut, against the real module ------------------------------------------------------------------------


@pytest.fixture(scope='module')
def tree(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """The guest contract tree, generated into a temporary root from the module as `regen` sees it."""
    if shutil.which('buf') is None:
        pytest.skip('buf not on PATH')
    image_bytes = agent_exposed.build_image()
    export = tmp_path_factory.mktemp('export')
    subprocess.run(['buf', 'export', '.', '--output', str(export)], cwd=_REPO_ROOT, check=True)  # noqa: S603, S607
    root = tmp_path_factory.mktemp('guest_contract')
    shipped = guest_contract.generate(image_bytes, export, root)
    filtered = guest_contract.compile_tree(
        root / guest_contract.PROTO, [f for f in shipped.files if f.startswith('themis/')]
    )
    return SimpleNamespace(
        image_bytes=image_bytes,
        image=agent_exposed.image_of(image_bytes),
        services=agent_exposed.marked_services(image_bytes),
        export=export,
        root=root,
        shipped=shipped,
        filtered=filtered,
    )


def _by_file(image: descriptor_pb2.FileDescriptorSet) -> dict[str, descriptor_pb2.FileDescriptorProto]:
    return {file.name: file for file in image.file}


def _leading_comments(file: descriptor_pb2.FileDescriptorProto) -> dict[str, str]:
    """Each rpc's leading comment, keyed by `Service.Method`, off the file's source info."""
    comments = {}
    for location in file.source_code_info.location:
        path = tuple(location.path)
        if len(path) == 4 and path[0] == 6 and path[2] == 2:
            service = file.service[path[1]]
            comments[f'{service.name}.{service.method[path[3]].name}'] = location.leading_comments
    return comments


@_needs_buf
def test_every_stub_offers_exactly_the_marked_rpcs(tree: SimpleNamespace) -> None:
    """Per service, the methods on the guest's stub equal the allowlist's — no stub file for an unmarked service."""
    python = tree.root / guest_contract.PYTHON
    stubs = {path.relative_to(python).as_posix(): path.read_text('utf-8') for path in python.rglob('*_pb2_grpc.py')}
    expected = {}
    for marked in tree.services:
        module = agent_exposed.module_of(marked.file, '_pb2_grpc')
        expected[f'{module.package.replace(".", "/")}/{module.name}.py'] = set(marked.methods)
    assert set(stubs) == set(expected), f'stub files {sorted(stubs)} vs marked services {sorted(expected)}'
    for path, text in stubs.items():
        offered = set(_STUB_METHOD.findall(text))
        assert offered == expected[path], f'{path}: offers {sorted(offered)}, allowlist says {sorted(expected[path])}'
        assert set(_SERVICER_METHOD.findall(text)) == expected[path], f'{path}: servicer base names other rpcs'


@_needs_buf
def test_every_shipped_type_is_reachable_and_every_reachable_type_is_shipped(tree: SimpleNamespace) -> None:
    """In each authored file the top-level types are exactly the ones a kept rpc reaches: none unused, none missing."""
    reachable_now = guest_contract.reachable_types(tree.filtered, tree.services)
    assert reachable_now == tree.shipped.types, 'the cut sources reach a different type set than the full module did'
    for name, file in _by_file(tree.filtered).items():
        if not name.startswith('themis/'):
            continue
        prefix = f'.{file.package}'
        declared = {f'{prefix}.{m.name}' for m in file.message_type} | {f'{prefix}.{e.name}' for e in file.enum_type}
        depth = prefix.count('.') + 1
        reachable = {t for t in tree.shipped.types if t.startswith(prefix + '.') and t.count('.') == depth}
        assert declared == reachable, f'{name}: declared {sorted(declared)} vs reachable {sorted(reachable)}'


@_needs_buf
def test_no_mark_and_no_option_import_survives(tree: SimpleNamespace) -> None:
    for path in (tree.root / guest_contract.PROTO).rglob('*.proto'):
        text = path.read_text('utf-8')
        assert 'agent_exposed' not in text, f'{path.name} still carries a mark'
        assert 'sandbox_options' not in text, f'{path.name} still imports the options proto'


@_needs_buf
def test_every_import_in_a_cut_file_resolves_inside_the_tree_or_the_runtime(tree: SimpleNamespace) -> None:
    proto = tree.root / guest_contract.PROTO
    for name, file in _by_file(tree.filtered).items():
        for dependency in file.dependency:
            resolves = dependency.startswith('google/protobuf/') or (proto / dependency).is_file()
            assert resolves, f'{name} imports {dependency}, which the tree does not hold'


@_needs_buf
def test_a_kept_rpc_keeps_its_own_comment(tree: SimpleNamespace) -> None:
    """The cut is by position, so a shifted position would hand one rpc another's comment; each is held to its own."""
    full, filtered = _by_file(tree.image), _by_file(tree.filtered)
    checked = 0
    for marked in tree.services:
        before, after = _leading_comments(full[marked.file]), _leading_comments(filtered[marked.file])
        for method in marked.methods:
            key = f'{marked.name}.{method}'
            assert after[key] == before[key], f'{marked.file}: {key} carries a different comment in the guest copy'
            checked += 1
    assert checked, 'no kept rpc compared'


@_needs_buf
def test_no_unmarked_rpc_is_declared_in_the_cut_sources(tree: SimpleNamespace) -> None:
    full, filtered = _by_file(tree.image), _by_file(tree.filtered)
    for marked in tree.services:
        before = {m.name for s in full[marked.file].service if s.name == marked.name for m in s.method}
        unmarked = before - set(marked.methods)
        declared = {m.name for s in filtered[marked.file].service if s.name == marked.name for m in s.method}
        assert declared == set(marked.methods), f'{marked.file}: {marked.name} declares {sorted(declared)}'
        assert not (unmarked & declared)


@_needs_buf
def test_files_this_repo_does_not_author_ship_intact(tree: SimpleNamespace) -> None:
    shipped_intact = [f for f in tree.shipped.files if not f.startswith('themis/')]
    assert shipped_intact, 'no upstream copy in the closure — the check would be vacuous'
    for name in shipped_intact:
        shipped_bytes = (tree.root / guest_contract.PROTO / name).read_bytes()
        assert shipped_bytes == (tree.export / name).read_bytes(), name


@_needs_buf
def test_generation_is_deterministic(tree: SimpleNamespace, tmp_path: pathlib.Path) -> None:
    guest_contract.generate(tree.image_bytes, tree.export, tmp_path)
    first = {
        p.relative_to(tree.root): p.read_bytes() for p in tree.root.rglob('*') if p.is_file() and p.suffix != '.binpb'
    }
    second = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert first == second


def test_the_mark_is_cut_by_the_option_file_the_stub_registers() -> None:
    assert guest_contract.agent_exposed_file() == sandbox_options_pb2.DESCRIPTOR.name
