"""The widget browser's examples (apps/web/src/widgets/examples) are standard textproto payloads an embed could hold.

The browser reads them with its own text-format parser. Parsing them here with protobuf's reference one holds each
file to the standard format: a file the browser reads and the reference refuses fails here. The two parsers are
compared on one example only: the reference printer wrote the FBN1 file from the web app's FBN1 fixture, and
apps/web/src/server/widget-examples.test.tsx checks that the browser reads it back as that fixture.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from google.protobuf import descriptor_pool, message_factory, text_format

from themis.widgets import asset

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_EXAMPLES = _REPO_ROOT / 'apps' / 'web' / 'src' / 'widgets' / 'examples'
_FILES = sorted(_EXAMPLES.glob('*/*.txtpb'))
_MESSAGE_HEADER = re.compile(r'^# proto-message: (\S+)$', re.MULTILINE)


def test_the_examples_are_found() -> None:
    assert _FILES


@pytest.mark.parametrize('path', _FILES, ids=lambda path: f'{path.parent.name}/{path.name}')
def test_an_example_is_a_payload_an_embed_could_hold(path: pathlib.Path) -> None:
    text = path.read_text('utf-8')
    header = _MESSAGE_HEADER.search(text)
    assert header is not None, f'{path.name} names no "# proto-message:"'
    desc = descriptor_pool.Default().FindMessageTypeByName(header.group(1))
    assert asset.is_payload_type(desc), f'{header.group(1)} is not a widget payload type'
    payload = message_factory.GetMessageClass(desc)()
    text_format.Parse(text, payload)
    # `encode` refuses what an embed would not draw: a payload failing its rules.
    asset.decode(asset.encode(payload))
