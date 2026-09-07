import argparse
import json
import sys
import time
from datetime import datetime

from . import auth
from .scopes import DEFAULT_SCOPES


def _scopes_arg(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else list(DEFAULT_SCOPES)


def cmd_login(a):
    scopes = _scopes_arg(a.scopes)
    try:
        if a.device:
            data = auth.login_device(scopes)
        else:
            data = auth.login(scopes, open_browser=a.open, timeout=a.timeout, login_hint=a.user, browser=a.browser)
    except auth.AuthError as e:
        print(f"Inloggningen misslyckades: {e}", file=sys.stderr)
        return 1
    claims = auth.decode_jwt(data["access_token"])
    print(f"Inloggad som {claims.get('upn') or claims.get('preferred_username', '?')}", file=sys.stderr)
    missing = set(scopes) - auth.granted_scopes(data)
    if missing:
        print("Ej beviljade scopes: " + ", ".join(sorted(missing)), file=sys.stderr)
    print(f"Token sparad i {auth.cache_path()}", file=sys.stderr)
    return 0


def cmd_token(a):
    try:
        tok = auth.get_token(_scopes_arg(a.scopes) if a.scopes else None)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"Bearer {tok}" if a.header else tok)
    return 0


def cmd_status(a):
    c = auth.read_cache()
    if not c:
        print("Inte inloggad. Kör: wsg login")
        return 2
    claims = auth.decode_jwt(c["access_token"])
    exp = datetime.fromtimestamp(c.get("expires_at", 0))
    fresh = c.get("expires_at", 0) > time.time()
    user = claims.get("upn") or claims.get("preferred_username") or "?"
    if a.json:
        print(json.dumps({
            "user": user,
            "name": claims.get("name"),
            "tenant": claims.get("tid"),
            "expires_at": exp.isoformat(timespec="seconds"),
            "access_token_valid": fresh,
            "has_refresh_token": bool(c.get("refresh_token")),
            "scopes": sorted(auth.granted_scopes(c)),
            "cache": str(auth.cache_path()),
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"Användare:     {user} ({claims.get('name', '')})")
    print(f"Tenant:        {claims.get('tid', '?')}")
    print(f"Access token:  {'giltig till ' + exp.strftime('%Y-%m-%d %H:%M') if fresh else 'utgången, förnyas vid nästa anrop'}")
    print(f"Refresh token: {'ja' if c.get('refresh_token') else 'nej'}")
    print(f"Cache:         {auth.cache_path()}")
    print("Scopes:")
    for s in sorted(auth.granted_scopes(c)):
        print(f"  {s}")
    return 0


def cmd_whoami(a):
    try:
        me = auth.graph("GET", "/me")
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{me.get('displayName')} <{me.get('userPrincipalName')}>")
    return 0


def cmd_logout(a):
    print("Cache borttagen." if auth.logout() else "Ingen cache fanns.")
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="wsg", description="Sign in to Microsoft Graph once, reuse the token everywhere.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", help="sign in interactively and cache the token")
    s.add_argument("--scopes", help="comma separated, default is the full list in wsg/scopes.py")
    s.add_argument("--open", action="store_true", help="open the default browser instead of just printing the URL")
    s.add_argument("--browser", metavar="NAME", help="open in this browser: edge, chrome, firefox, or an executable")
    s.add_argument("--device", action="store_true", help="use device code flow (for machines without a browser)")
    s.add_argument("--user", help="login hint, e.g. name@domain")
    s.add_argument("--timeout", type=int, default=600, help="seconds to wait for the browser, default 600")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("token", help="print a valid access token, refreshing silently")
    s.add_argument("--scopes", help="require these scopes (comma separated)")
    s.add_argument("--header", action="store_true", help="print as 'Bearer <token>'")
    s.set_defaults(fn=cmd_token)

    s = sub.add_parser("status", help="show who is signed in and what was granted")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("whoami", help="call Graph /me")
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("logout", help="delete the cached token")
    s.set_defaults(fn=cmd_logout)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
