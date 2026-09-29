"""The shared ownership cases (apps/web/src/widgets/ownership-cases.test-support.json), and their schema's types.

The cases carry a test schema of their own beside the checklist; `pool` adds it to the default descriptor pool, which
the widgets library reads payload types from, once however many tests ask.
"""

from __future__ import annotations

import json

from google.protobuf import descriptor_pb2, descriptor_pool, json_format

from themis.widgets.tests import case_files

CASES_FILE = case_files.OWNERSHIP_CASES
CASES = json.loads(CASES_FILE.read_text('utf-8'))


def pool() -> descriptor_pool.DescriptorPool:
    """The default pool, holding the cases' test schema."""
    default = descriptor_pool.Default()
    try:
        default.FindFileByName(CASES['schema']['name'])
    except KeyError:
        default.Add(json_format.ParseDict(CASES['schema'], descriptor_pb2.FileDescriptorProto()))
    return default
