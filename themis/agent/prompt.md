# Working in the sandbox

## Calling internal services

Internal Themis services you can call in code mode. Get a stub with `services.<name>()`, assign it, and reuse it across
calls. You hold no credentials or service URLs — the connection is handled for you; a `grpc.RpcError` surfaces loudly
rather than a silent empty result.

- **hello** — echoes your note against the Analysis your session is bound to (a connectivity check).

```python
from themis.agent import services
from themis.rpc import hello_pb2

stub = services.hello()  # assign once; reuse across calls
reply = stub.SayHello(hello_pb2.SayHelloRequest(note="hello"))
print(reply.greeting, reply.analysis_id, reply.project_id)
```

## Linting the working document

Your working document is `/workspace/document.md`. Before ending a turn, lint it and fix what it reports — a convenience
for self-correction, not a gate (the frontend renderer is the arbiter at render time):

```python
import pathlib

from themis.document_linter import linter

for issue in linter.lint(pathlib.Path('/workspace/document.md').read_text()):
    print(issue)
```

An empty result means the document is structurally well-formed (non-empty, exactly one top-level title).
