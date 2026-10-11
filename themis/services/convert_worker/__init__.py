"""The full-text convert worker: the pushed Cloud Task `/convert` handler.

A separate deployable from the evidence service (architecture B, `docs/design/evidence-fulltext.md`):
that image carries only the metadata probe its ingest path needs, while this worker carries the
body-fetch and conversion ladder (litcache) and runs one paper's full-text production per pushed task.
"""
