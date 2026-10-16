"""The guest's full-text capture: a paper's rendering streamed into a file, whole or not at all.

Driven against a real streaming server on the hatch's Unix socket, so the stub, the channel's deadline and the
retry rule between them are the ones a session uses. What has to hold is that a file at the path is always a whole
rendering: a failure partway, a stream of the wrong shape, or bytes that are not the rendering named leave nothing.
"""

from __future__ import annotations

import contextlib
import hashlib
import pathlib

import grpc
import pytest

from themis.rpc import literature_pb2
from themis.services.sandbox_worker.guest import fulltext
from themis.services.sandbox_worker.tests import fakes

# A rendering whose first chunk ends inside a two-byte character, as a chunk boundary is free to.
_TEXT = '# A paper\n\nThe assay showed reduced channel activity — é.\n'
_DATA = _TEXT.encode('utf-8')
_SPLIT = _DATA.index('é'.encode()) + 1


def _header(data: bytes = _DATA, **overrides: object) -> literature_pb2.ReadMarkdownResponse:
    header = literature_pb2.RenderingHeader(
        rendering_key=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        provenance=literature_pb2.TEXT_PROVENANCE_SUPPLIED,
    )
    for name, value in overrides.items():
        setattr(header, name, value)
    return literature_pb2.ReadMarkdownResponse(header=header)


def _chunks(data: bytes = _DATA) -> tuple[literature_pb2.ReadMarkdownResponse, ...]:
    return (
        literature_pb2.ReadMarkdownResponse(chunk=data[:_SPLIT]),
        literature_pb2.ReadMarkdownResponse(chunk=data[_SPLIT:]),
    )


def _capture(
    attempts: list[fakes.StreamAttempt], target: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[fulltext.Capture | literature_pb2.FullTextUnavailable, fakes.ScriptedReadMarkdown]:
    upstream = fakes.ScriptedReadMarkdown(attempts)
    with fakes.serving_read_markdown(upstream) as socket:
        monkeypatch.setenv('POSTERN_HATCH', socket)
        return fulltext.capture_markdown('doc-1', target, timeout=10), upstream


def _left_behind(directory: pathlib.Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def test_a_rendering_is_written_whole_and_named(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / 'captured' / 'doc-1.md'
    result, _ = _capture([fakes.StreamAttempt(responses=(_header(), *_chunks()))], target, monkeypatch)
    assert result == fulltext.Capture(
        doc_id='doc-1',
        path=target,
        size_bytes=len(_DATA),
        rendering_key=hashlib.sha256(_DATA).hexdigest(),
        provenance='TEXT_PROVENANCE_SUPPLIED',
    )
    assert target.read_bytes() == _DATA
    assert _left_behind(target.parent) == ['doc-1.md']  # the staging file was renamed, not copied


def test_a_paper_without_text_writes_nothing_and_says_why(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unavailable = literature_pb2.FullTextUnavailable(state=literature_pb2.FULL_TEXT_STATE_PENDING)
    script = [fakes.StreamAttempt(responses=(literature_pb2.ReadMarkdownResponse(unavailable=unavailable),))]
    result, _ = _capture(script, tmp_path / 'doc-1.md', monkeypatch)
    assert result == unavailable
    assert _left_behind(tmp_path) == []


@pytest.mark.parametrize(
    'attempt',
    [
        fakes.StreamAttempt(
            responses=(literature_pb2.ReadMarkdownResponse(unavailable=literature_pb2.FullTextUnavailable()),)
        ),
        fakes.StreamAttempt(responses=(_header(rendering_key='0' * 64), *_chunks())),
    ],
    ids=['unavailable', 'mismatch'],
)
def test_a_capture_that_writes_nothing_leaves_an_earlier_file_as_it_was(
    attempt: fakes.StreamAttempt, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / 'doc-1.md'
    target.write_bytes(b'an earlier capture')
    with contextlib.suppress(ValueError):
        _capture([attempt], target, monkeypatch)
    assert target.read_bytes() == b'an earlier capture'
    assert _left_behind(tmp_path) == ['doc-1.md']


def test_a_read_that_fails_partway_leaves_no_file_and_is_not_reissued(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A prefix written to the path would read as the paper, ending wherever the stream broke.
    script = [
        fakes.StreamAttempt(responses=(_header(), _chunks()[0]), fail=grpc.StatusCode.UNAVAILABLE),
        fakes.StreamAttempt(responses=(_header(), *_chunks())),
    ]
    upstream = fakes.ScriptedReadMarkdown(script)
    with fakes.serving_read_markdown(upstream) as socket:
        monkeypatch.setenv('POSTERN_HATCH', socket)
        with pytest.raises(grpc.RpcError) as raised:
            fulltext.capture_markdown('doc-1', tmp_path / 'doc-1.md', timeout=10)
    assert raised.value.code() is grpc.StatusCode.UNAVAILABLE
    assert upstream.calls == 1
    assert _left_behind(tmp_path) == []


def test_a_read_that_fails_before_any_message_is_reissued(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        fakes.StreamAttempt(fail=grpc.StatusCode.UNAVAILABLE),
        fakes.StreamAttempt(responses=(_header(), *_chunks())),
    ]
    upstream = fakes.ScriptedReadMarkdown(script)
    with fakes.serving_read_markdown(upstream) as socket:
        monkeypatch.setenv('POSTERN_HATCH', socket)
        result = fulltext.capture_markdown('doc-1', tmp_path / 'doc-1.md', timeout=30)
    assert isinstance(result, fulltext.Capture)
    assert upstream.calls == 2
    assert (tmp_path / 'doc-1.md').read_bytes() == _DATA


@pytest.mark.parametrize(
    'responses',
    [
        (_header(rendering_key='0' * 64), *_chunks()),
        (_header(size_bytes=len(_DATA) + 1), *_chunks()),
        (_header(), _chunks()[0]),
        (*_chunks(), _header()),
        (_header(), _header(), *_chunks()),
        (
            literature_pb2.ReadMarkdownResponse(unavailable=literature_pb2.FullTextUnavailable()),
            *_chunks(),
        ),
        (),
    ],
    ids=['wrong-key', 'wrong-size', 'short', 'chunk-before-header', 'two-headers', 'unavailable-then-more', 'empty'],
)
def test_a_stream_that_is_not_the_rendering_its_header_names_leaves_no_file(
    responses: tuple[literature_pb2.ReadMarkdownResponse, ...], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match='ReadMarkdown'):
        _capture([fakes.StreamAttempt(responses=responses)], tmp_path / 'doc-1.md', monkeypatch)
    assert _left_behind(tmp_path) == []


def test_an_unknown_paper_is_the_service_s_not_found(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(grpc.RpcError) as raised:
        _capture([fakes.StreamAttempt(fail=grpc.StatusCode.NOT_FOUND)], tmp_path / 'doc-1.md', monkeypatch)
    assert raised.value.code() is grpc.StatusCode.NOT_FOUND
    assert _left_behind(tmp_path) == []
