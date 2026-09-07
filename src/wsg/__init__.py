"""wsg: one Microsoft Graph sign-in shared by all your scripts."""

from .auth import AuthError, get_token, login, logout, read_cache  # noqa: F401

__version__ = "0.1.0"
