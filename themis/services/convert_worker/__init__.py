"""The full-text convert worker: the pushed Cloud Task `/convert` handler.

A separate deployable from the read service (architecture B, `docs/design/evidence-fulltext.md`): the
read image stays a lean GCS reader, while this worker carries the fetch/convert ladder (litcache) and
runs one paper's full-text production per pushed task.
"""
