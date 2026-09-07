"""
Token handling: cache, refresh, and the two interactive flows.

Client: Microsoft Graph Command Line Tools (first-party public client, no secret,
loopback redirect allowed). Same client Connect-MgGraph uses.

Cache: %LOCALAPPDATA%\\wsg\\token.json on Windows, ~/.config/wsg/token.json
elsewhere. Holds access token, refresh token, expiry and granted scopes. The
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

CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH = "https://graph.microsoft.com"


class AuthError(Exception):
    pass


def _tenant():
    return os.environ.get("WSG_TENANT", "organizations")


def _authority():
    return f"https://login.microsoftonline.com/{_tenant()}/oauth2/v2.0"


def cache_path():
    if os.environ.get("WSG_TOKEN_CACHE"):
        return Path(os.environ["WSG_TOKEN_CACHE"])
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "wsg" / "token.json"


# ── cache ─────────────────────────────────────────────────────────────────────

def read_cache():
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(tok):
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
        "scope": tok.get("scope", ""),
        "tenant": _tenant(),
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(p)
    return data


def logout():
    p = cache_path()
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


def _refresh(refresh_token, scopes):
    tok, err = _post(f"{_authority()}/token", {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": _scope_string(scopes),
    })
    if tok:
        return _write_cache(tok)
    raise AuthError((err or {}).get("error_description") or (err or {}).get("error") or "refresh failed")


def get_token(scopes=None):
    """Return a valid access token from the cache, refreshing if needed.

    Never prompts. Raises AuthError when a login is required.
    """
    cached = read_cache()
    if not cached:
        raise AuthError("Not signed in. Run: wsg login")
    have = granted_scopes(cached)
    want = set(scopes or [])
    if cached.get("expires_at", 0) > time.time() and want <= have:
        return cached["access_token"]
    if not cached.get("refresh_token"):
        raise AuthError("Cached token expired and no refresh token. Run: wsg login")
    # Refresh for the union, so the cache keeps covering everything granted so far.
    return _refresh(cached["refresh_token"], sorted(have | want))["access_token"]


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


def login(scopes, open_browser=False, timeout=300, out=sys.stderr, login_hint=None):
    """Authorization code flow with PKCE. Prints the URL, waits for the redirect."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    port = _free_port()
    redirect = f"http://localhost:{port}"

    params = {
        "client_id": CLIENT_ID,
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

    threading.Thread(target=serve, daemon=True).start()

    print("Öppna den här adressen i webbläsaren och logga in:", file=out, flush=True)
    print(url, file=out, flush=True)
    if open_browser:
        webbrowser.open(url)

    ok = got.wait(timeout)
    server.server_close()
    if not ok:
        raise AuthError("Ingen inloggning inom tidsgränsen.")

    if result.get("error"):
        raise AuthError(f"{result['error']}: {result.get('error_description', '')}")
    if result.get("state") != state:
        raise AuthError("state mismatch, avbryter.")

    tok, err = _post(f"{_authority()}/token", {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": result["code"],
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    if not tok:
        raise AuthError((err or {}).get("error_description") or str(err))
    return _write_cache(tok)


# ── interactive: device code (for boxes without a browser) ────────────────────

def login_device(scopes, out=sys.stderr):
    dc, err = _post(f"{_authority()}/devicecode", {"client_id": CLIENT_ID, "scope": _scope_string(scopes)})
    if not dc:
        raise AuthError(str(err))
    print(dc["message"], file=out, flush=True)
    deadline = time.time() + int(dc["expires_in"])
    interval = int(dc.get("interval", 5))
    while time.time() < deadline:
        time.sleep(interval)
        tok, err = _post(f"{_authority()}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": CLIENT_ID,
            "device_code": dc["device_code"],
        })
        if tok:
            return _write_cache(tok)
        code = (err or {}).get("error")
        if code == "slow_down":
            interval += 5
        elif code != "authorization_pending":
            raise AuthError((err or {}).get("error_description") or code)
    raise AuthError("Device code timed out.")


# ── convenience ───────────────────────────────────────────────────────────────

def graph(method, path, token=None, body=None, scopes=None):
    """Small Graph helper for scripts: graph('GET', '/me')."""
    token = token or get_token(scopes)
    url = path if path.startswith("http") else f"{GRAPH}/v1.0{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, method=method, headers=headers)
    with request.urlopen(req) as r:
        raw = r.read()
        return json.loads(raw) if raw else None
