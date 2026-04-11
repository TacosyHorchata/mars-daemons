"""Magic-link authentication for the Mars control plane.

Three layers:

* :mod:`auth.magic_link` — token issuance + verification.
* :mod:`auth.session` — JWT session cookie lifecycle.
* :mod:`auth.middleware` — FastAPI dependency that resolves the
  current user from a request.
* :mod:`auth.email` — transport shim for sending magic-link emails
  via Resend (or a test fake).

The public ``create_control_app`` factory wires all four together
when the caller supplies a ``magic_link_config``.
"""
