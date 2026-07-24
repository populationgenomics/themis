"""The self-hosted sandbox worker: the trusted `EnvironmentWorker` loop over postern (postern-sandbox-swap.md).

One trusted process per session. It holds the Anthropic environment key and calls Anthropic directly; every
`run_python` tool call is marshaled into a `postern.Sandbox` whose only exit is a method-allowlisted gRPC hatch. The
worker restores and checkpoints `/workspace` against the store directly (the tar hardening ported from the former
credential proxy). No untrusted code ever shares this process.
"""

from __future__ import annotations
