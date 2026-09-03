"""Load an index's JSON record into its mirror proto, strictly.

A mirror (docs/design/proto.md, "Mirrored upstream schemas") is hand-authored against the JSON an
index publishes, so it can lag the index. The parse rejects a key the mirror lacks rather than
dropping it: no record is stored thinned, and the paper is charged as schema drift with the
parser's message naming the key, for the mirror to gain it. A key spelled as a field's proto name
rather than its `json_name` is accepted, as proto3-JSON specifies; the round-trip gate is what
tells the two spellings apart.
"""

from __future__ import annotations

from google.protobuf import json_format, message


class SchemaDriftError(Exception):
    """A record carries a key, or a value of a shape, its mirror does not declare.

    The fault is the mirror's lag behind the index, not the record's, so the remedy is the mirror's
    field; until then the paper is dead-lettered with `detail`, the parser's message naming the key.
    """

    def __init__(self, index: str, detail: str) -> None:
        super().__init__(f'{index} record does not fit its mirror: {detail}')
        self.index = index
        self.detail = detail


def parse_strict[M: message.Message](document: dict[str, object], target: M, *, index: str) -> M:
    """Load ``document`` into ``target``, rejecting any key the mirror does not declare.

    Args:
        document: The record as the index published it, after the loader has wrapped the shapes
            the mirror's header names (an array of arrays, an object whose values are arrays).
        target: An empty message of the mirror's record type.
        index: The index's name, for the error.

    Returns:
        ``target``, populated.

    Raises:
        SchemaDriftError: If the document carries a key the mirror lacks, or a value of a shape
            the field cannot hold.
    """
    try:
        return json_format.ParseDict(document, target, ignore_unknown_fields=False)
    except json_format.ParseError as e:
        # The parser's first line names the key; the rest lists every field the message declares.
        raise SchemaDriftError(index, str(e).splitlines()[0]) from e
