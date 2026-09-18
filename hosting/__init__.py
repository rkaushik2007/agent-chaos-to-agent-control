"""Local process hosting for the demo.

Neutral ground: both the tool servers and the governance toolbox need to run an
ASGI app on loopback for the length of an act, and neither should have to import
the other to do it.
"""

from hosting.serving import serve_asgi

__all__ = ["serve_asgi"]
