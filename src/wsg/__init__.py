"""wsg: one sign-in per service, shared by all your scripts.

    from wsg import graph, arena
    graph.get_token(["User.Read"])     # Microsoft Graph, Entra
    arena.get("/api/me")               # WeSports Arena, Cloudflare Access
"""

__version__ = "0.6.0"

from . import auth as graph  # noqa: E402,F401
from . import arena  # noqa: E402,F401
from .auth import AuthError, get_token, login, logout, read_cache  # noqa: E402,F401
