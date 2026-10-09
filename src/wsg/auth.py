"""
Token handling: cache, refresh, and the two interactive flows.

Client: "WeSports CLI", our own app registration in the Vartex tenant (public
client, no secret, loopback and device code). Until 2026-10-09 this was Microsoft
Graph Command Line Tools, the client Connect-MgGraph uses. It was replaced because
Entra puts every scope consented for a client into its tokens, whatever the login
asks for, and a shared first-party client collects consents from every tool that
uses it. Our own client is consented for the base set in scopes.py and nothing
more, so a token holds what that list says.

The admin commands (wsg-admin-cli) sign in through a second app, "WeSports Admin
CLI", which only assigned people may use. Each client is a Client object with its
own token cache, and every function here takes `client=` for it.

Cache: %LOCALAPPDATA%\\wsg\\token.json on Windows, ~/.config/wsg/token.json
elsewhere; the admin client caches beside it under its own name. Holds access token, refresh token, expiry and granted scopes. The
refresh token lives 90 days and is extended every time it is used, so after one
login the scripts stay quiet until a password change or 90 idle days.
"""

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib import parse, request
from urllib.error import HTTPError

GRAPH = "https://graph.microsoft.com"
# Vartex. Our apps are single-tenant, and a single-tenant app cannot sign in
# through /organizations, so the tenant is named rather than discovered.
TENANT_ID = "8a11617d-a9b4-42ec-985c-0fb8488747c4"


class Client:
    """One app registration: its id, the file its token is cached in, and the
    command that signs in to it (named in errors, so the fix is in the message)."""

    def __init__(self, client_id, cache_name, login_command):
        self.id = client_id
        self.cache_name = cache_name
        self.login_command = login_command


CORE = Client(os.environ.get("WSG_CLIENT_ID", "6a691e2e-9293-4c35-afda-f235e7d58408"), "token.json", "wsg graph login")


class AuthError(Exception):
    pass


def _tenant():
    return os.environ.get("WSG_TENANT", TENANT_ID)


def _authority():
    return f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0"


def cache_path(client=None):
    client = client or CORE
    if client is CORE and os.environ.get("WSG_TOKEN_CACHE"):
        return Path(os.environ["WSG_TOKEN_CACHE"])
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "wsg" / client.cache_name


# ── cache ─────────────────────────────────────────────────────────────────────

def read_cache(client=None):
    try:
        cached = json.loads(cache_path(client).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # A refresh token is bound to the client that got it; another client cannot use
    # it. Caches written before 2026-10-09 carry no client id and come from the Graph
    # CLI client, so they read as signed out and the error names the login to run.
    if cached.get("client_id") != (client or CORE).id:
        return None
    return cached


def _write_cache(tok, client=None):
    p = cache_path(client)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
        "scope": tok.get("scope", ""),
        "tenant": _tenant(),
        "client_id": (client or CORE).id,
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(p)
    return data


def logout(client=None):
    p = cache_path(client)
    existed = p.exists()
    if existed:
        p.unlink()
    return existed


def granted_scopes(cached):
    return {s.replace(f"{GRAPH}/", "") for s in (cached or {}).get("scope", "").split()}


def decode_jwt(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ── token endpoint ────────────────────────────────────────────────────────────

def _post(url, body):
    req = request.Request(url, data=parse.urlencode(body).encode())
    try:
        with request.urlopen(req) as r:
            return json.loads(r.read()), None
    except HTTPError as e:
        try:
            return None, json.loads(e.read())
        except Exception:
            return None, {"error": str(e)}


def _scope_string(scopes):
    return " ".join(f"{GRAPH}/{s}" for s in scopes) + " offline_access openid profile"


def _refresh(refresh_token, scopes, client=None):
    tok, err = _post(f"{_authority()}/token", {
        "grant_type": "refresh_token",
        "client_id": (client or CORE).id,
        "refresh_token": refresh_token,
        "scope": _scope_string(scopes),
    })
    if tok:
        return _write_cache(tok, client)
    raise AuthError((err or {}).get("error_description") or (err or {}).get("error") or "refresh failed")


def get_token(scopes=None, client=None):
    """Return a valid access token from the cache, refreshing if needed.

    Never prompts. Raises AuthError when a login is required.
    """
    client = client or CORE
    cached = read_cache(client)
    if not cached:
        raise AuthError(f"Not signed in. Run: {client.login_command}")
    have = granted_scopes(cached)
    want = set(scopes or [])
    if cached.get("expires_at", 0) > time.time() and want <= have:
        return cached["access_token"]
    if not cached.get("refresh_token"):
        raise AuthError(f"Cached token expired and no refresh token. Run: {client.login_command}")
    # Refresh for the union, so the cache keeps covering everything granted so far.
    return _refresh(cached["refresh_token"], sorted(have | want), client)["access_token"]


# ── interactive: auth code + PKCE on loopback ─────────────────────────────────

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_DONE_HTML = (
    "<!doctype html><meta charset='utf-8'><title>wsg</title>"
    "<body style='font-family:system-ui;padding:3em'>"
    "<h2>Inloggad.</h2><p>Du kan stänga den här fliken.</p>"
)


def open_in_browser(url, browser=None):
    """browser: None for the system default, or 'edge' / 'chrome' / 'firefox' / a path."""
    if not browser:
        webbrowser.open(url)
        return
    aliases = {"edge": "msedge", "chrome": "chrome", "firefox": "firefox"}
    exe = aliases.get(browser.lower(), browser)
    if sys.platform == "win32":
        import shutil
        import subprocess
        path = shutil.which(exe) or _win_app_path(exe)
        if not path:
            raise AuthError(f"Hittar inte webbläsaren {browser}.")
        # Popen with a list: no shell, so the & in the URL is never interpreted.
        subprocess.Popen([path, url])
    else:
        webbrowser.get(exe).open(url)


def _win_app_path(exe):
    """Resolve e.g. msedge.exe via the App Paths registry key (not on PATH by default)."""
    import winreg
    name = exe if exe.lower().endswith(".exe") else exe + ".exe"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}") as k:
                return winreg.QueryValueEx(k, "")[0]
        except OSError:
            continue
    return None


def login(scopes, open_browser=False, timeout=600, out=sys.stderr, login_hint=None, browser=None,
          client=None):
    """Authorization code flow with PKCE. Prints the URL, waits for the redirect."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    port = _free_port()
    redirect = f"http://localhost:{port}"

    client = client or CORE
    params = {
        "client_id": client.id,
        "response_type": "code",
        "redirect_uri": redirect,
        "response_mode": "query",
        "scope": _scope_string(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = f"{_authority()}/authorize?{parse.urlencode(params)}"

    result = {}
    got = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(parse.parse_qsl(parse.urlsplit(self.path).query))
            if "code" not in q and "error" not in q:
                self.send_response(404)
                self.end_headers()
                return
            result.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_HTML.encode("utf-8"))
            got.set()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 1

    def serve():
        while not got.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    print("Öppna den här adressen i webbläsaren och logga in:", file=out, flush=True)
    print(url, file=out, flush=True)
    if open_browser or browser:
        open_in_browser(url, browser)

    ok = got.wait(timeout)
    got.set()            # stop the serve loop before closing the socket
    thread.join(3)
    server.server_close()
    if not ok:
        raise AuthError(f"Ingen inloggning inom {timeout} sekunder.")

    if result.get("error"):
        raise AuthError(f"{result['error']}: {result.get('error_description', '')}")
    if result.get("state") != state:
        raise AuthError("state mismatch, avbryter.")

    tok, err = _post(f"{_authority()}/token", {
        "grant_type": "authorization_code",
        "client_id": client.id,
        "code": result["code"],
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    if not tok:
        raise AuthError((err or {}).get("error_description") or str(err))
    return _write_cache(tok, client)


# ── interactive: device code (for boxes without a browser) ────────────────────

def login_device(scopes, out=sys.stderr, client=None):
    client = client or CORE
    dc, err = _post(f"{_authority()}/devicecode", {"client_id": client.id, "scope": _scope_string(scopes)})
    if not dc:
        raise AuthError(str(err))
    print(dc["message"], file=out, flush=True)
    deadline = time.time() + int(dc["expires_in"])
    interval = int(dc.get("interval", 5))
    while time.time() < deadline:
        time.sleep(interval)
        tok, err = _post(f"{_authority()}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client.id,
            "device_code": dc["device_code"],
        })
        if tok:
            return _write_cache(tok, client)
        code = (err or {}).get("error")
        if code == "slow_down":
            interval += 5
        elif code != "authorization_pending":
            raise AuthError((err or {}).get("error_description") or code)
    raise AuthError("Device code timed out.")


# ── convenience ───────────────────────────────────────────────────────────────

def graph(method, path, token=None, body=None, scopes=None, client=None):
    """Small Graph helper for scripts: graph('GET', '/me')."""
    token = token or get_token(scopes, client)
    url = path if path.startswith("http") else f"{GRAPH}/v1.0{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, method=method, headers=headers)
    with request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None
