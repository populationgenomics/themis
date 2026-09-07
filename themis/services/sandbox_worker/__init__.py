"""The self-hosted sandbox worker: the trusted `EnvironmentWorker` loop over postern (sandbox-worker.md).

One trusted process per session. It holds the Anthropic environment key and the session token and calls Anthropic
directly; every `shell` command is marshaled into a `postern.Sandbox` whose exits are a method-allowlisted gRPC
hatch and two git hatches onto a bare mirror of the Analysis repository, which the worker keeps through the Sheaf
service with the session token as its one credential. No untrusted code ever shares this process.
"""

from __future__ import annotations
