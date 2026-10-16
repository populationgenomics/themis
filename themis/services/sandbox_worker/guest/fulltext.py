"""Reading a paper's full text into a file, for code-mode snippets.

Guest-side: runs inside the postern sandbox, shipped into the guest rootfs as ``themis.agent.fulltext`` alongside
``themis.agent.services``. ``Literature.ReadMarkdown`` streams a paper's canonical rendering whole, however long it is,
and ``capture_markdown`` writes that stream to a file rather than into the snippet's output: a reader then greps and
slices the file for what it needs, and nothing reaches its context that it did not ask for.

The file is the rendering exactly as the store holds it — UTF-8 markdown, byte for byte — and it appears only once
every byte has arrived and hashed to the rendering's key. A read that fails partway leaves no file behind, so a file at
the path is always a whole rendering, never a prefix that reads like one. The key names which rendering a quote taken
from the file came from.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import pathlib
import tempfile
from collections.abc import Iterator
from typing import IO

from themis.rpc import literature_pb2

from . import retry, services


@dataclasses.dataclass(frozen=True)
class Capture:
    """A paper's canonical rendering, written whole to ``path``.

    Attributes:
        doc_id: The paper.
        path: Where the rendering is: UTF-8 markdown, byte for byte as the store holds it.
        size_bytes: The file's length.
        rendering_key: The rendering's record key — the sha256 of its bytes — naming exactly which text this is.
        provenance: How the text entered the store, as its ``TextProvenance`` name (``TEXT_PROVENANCE_SUPPLIED``
            for text a human's institutional access obtained).
    """

    doc_id: str
    path: pathlib.Path
    size_bytes: int
    rendering_key: str
    provenance: str


def capture_markdown(
    doc_id: str,
    path: str | os.PathLike[str],
    *,
    attempts: int = 4,
    timeout: float = retry.DEFAULT_TIMEOUT_S,
) -> Capture | literature_pb2.FullTextUnavailable:
    """Stream a paper's canonical rendering into ``path``, whole, and say which rendering it is.

    The bytes go to a staging file beside ``path`` as they arrive and are renamed onto it once the whole
    rendering has arrived and matched its key, replacing whatever ``path`` held. The read is reissued on a
    transient failure only before its first message, as ``retry.stream`` reissues one.

    Args:
        doc_id: The paper, as ``MaybeIngestPapers`` resolved it.
        path: Where to write the rendering; its directory is created if absent. Keep it under
            ``/workspace/scratch/``: a full text is licensed, and never leaves the sandbox.
        attempts: How many times to issue the read in total (see ``retry.stream``).
        timeout: Seconds the whole read gets, retries included.

    Returns:
        The capture, or — for a paper the store holds but has no text for — the ``FullTextUnavailable``
        the service answered with, whose ``state`` says why. Nothing is written then, and a file already
        at ``path`` is left as it was; so it is on every failure below.

    Raises:
        grpc.RpcError: The read failed: ``NOT_FOUND`` for a ``doc_id`` the store holds no paper for, or a
            failure ``retry.stream`` did not or could not retry. No file is written.
        ValueError: The stream broke the shape ``ReadMarkdownResponse`` states, or its bytes are not the
            rendering its header named. No file is written.
    """
    target = pathlib.Path(path)
    responses = retry.stream(
        services.literature().ReadMarkdown,
        literature_pb2.ReadMarkdownRequest(doc_id=doc_id),
        attempts=attempts,
        timeout=timeout,
    )
    with contextlib.closing(responses):
        first = next(responses, None)
        if first is None:
            raise ValueError(f'ReadMarkdown for {doc_id!r} ended without a message')
        part = first.WhichOneof('part')
        if part == 'unavailable':
            if next(responses, None) is not None:
                raise ValueError(f'ReadMarkdown for {doc_id!r} sent a message after `unavailable`')
            return first.unavailable
        if part != 'header':
            raise ValueError(f'ReadMarkdown for {doc_id!r} opened with {part!r} where its header belongs')
        header = first.header
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = tempfile.NamedTemporaryFile(  # noqa: SIM115 — renamed onto the target, so not a `with` scope
            dir=target.parent, prefix=f'.{target.name}.', suffix='.partial', delete=False
        )
        try:
            with staged:
                _write(doc_id, header, responses, staged)
            os.replace(staged.name, target)
        except BaseException:
            pathlib.Path(staged.name).unlink(missing_ok=True)
            raise
    return Capture(
        doc_id=doc_id,
        path=target,
        size_bytes=header.size_bytes,
        rendering_key=header.rendering_key,
        provenance=literature_pb2.TextProvenance.Name(header.provenance),
    )


def _write(
    doc_id: str,
    header: literature_pb2.RenderingHeader,
    responses: Iterator[literature_pb2.ReadMarkdownResponse],
    staged: IO[bytes],
) -> None:
    """Write the chunks after ``header`` to ``staged``, holding them to the size and key it states."""
    digest = hashlib.sha256()
    written = 0
    for response in responses:
        part = response.WhichOneof('part')
        if part != 'chunk':
            raise ValueError(f'ReadMarkdown for {doc_id!r} sent {part!r} among its chunks')
        written += len(response.chunk)
        if written > header.size_bytes:
            raise ValueError(f'ReadMarkdown for {doc_id!r} sent more than the {header.size_bytes} bytes it stated')
        staged.write(response.chunk)
        digest.update(response.chunk)
    if written != header.size_bytes or digest.hexdigest() != header.rendering_key:
        raise ValueError(
            f'ReadMarkdown for {doc_id!r} sent {written} bytes that are not rendering {header.rendering_key} '
            f'({header.size_bytes} bytes)'
        )
