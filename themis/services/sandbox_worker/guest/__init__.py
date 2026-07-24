"""Guest-side code that runs *inside* the postern sandbox, not in the trusted worker.

The Dockerfile ships this into the guest rootfs (see themis/services/sandbox_worker/Dockerfile). The trusted worker
never imports it; it exists only for the model's code-mode calls within the sandbox.
"""

from __future__ import annotations
