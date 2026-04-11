"""Mars control-plane package.

Wrapped under ``mars_control`` so it does not shadow the runtime-side
``events`` / ``session`` packages on the shared pytest ``pythonpath``.
The schema module (``schema.agent``) stays at the src root because it
is shared between control and runtime.
"""
