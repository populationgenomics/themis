"""Widget assets: typed blocks the working document embeds with ``::embed[<path>]`` (document-widgets.md).

``models`` holds the payload schemas, each a message the ``widget`` option marks; ``asset`` is the contract an
asset file keeps — the paths an ``::embed`` may name, and the bytes, a serialized ``google.protobuf.Any`` over a
marked payload that passes its protovalidate rules. The guest's asset helper writes through it and the
working-document linter reads through it, so both apply one set of rules.
"""
