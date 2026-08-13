"""The evidence service: one Cloud Run image serving the data plane's evidence interfaces.

An interface is a subpackage carrying its own proto contract, servicer, backend port and prefixed
``THEMIS_<INTERFACE>_*`` env vars, which the entrypoint attaches through its ``interface.register``. No
interface imports another. Rationale and the anatomy: docs/design/services.md.
"""
