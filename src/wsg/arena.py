"""
WeSports Arena: sign in with Arena's own OAuth server and call the Arena API.

Cloudflare Access came off arena.wesports.io on 2026-09-20, and with it the JWT
this module used to fetch through `cloudflared`. A command-line tool cannot carry
the browser's __Host- session cookie, so it signs in the way the Claude connector
does: authorization code + PKCE against /oauth/authorize, with a loopback
redirect to a port on this machine. The page that asks is Arena's own, behind
the normal Arena sign-in (Tribune), so you prove yourself exactly as in the
browser and the token carries the assurance of that sign-in.

The client is registered in Arena with audience 'api' (Arena:
scripts/mcp-client.mjs create --api). Its tokens open /api as you, with your
rights and no more, and never /mcp. Its id and secret live in
%LOCALAPPDATA%\\wsg\\arena.json (or ~/.config/wsg/arena.json), or in
WSG_ARENA_CLIENT_ID / WSG_ARENA_CLIENT_SECRET.

An access token lives an hour and is renewed with the refresh token, which lives
30 days and rotates on every use, so `wsg arena login` is needed about monthly.
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
from pathlib import Path
from urllib import parse, request
from urllib.error import HTTPError

from . import __version__
from .auth import AuthError

DEFAULT_URL = "https://arena.wesports.io"
# Cloudflare's bot rules on the zone answer 403 (error 1010, browser_signature_banned)
# to the default "Python-urllib/x.y" agent, so identify as ourselves.
USER_AGENT = f"wsg/{__version__} (+https://github.com/Wesports-Scandinavia-AB/wsg-cli)"


def app_url():
    return os.environ.get("WSG_ARENA_URL", DEFAULT_URL).rstrip("/")


def _config_dir():
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "wsg" if base else Path.home() / ".config" / "wsg"


def client_path():
    return Path(os.environ.get("WSG_ARENA_CLIENT") or _config_dir() / "arena.json")


def cache_path():
    return Path(os.environ.get("WSG_ARENA_TOKEN_CACHE") or _config_dir() / "arena-token.json")


def client():
    """(client_id, client_secret) for the registered 'api' client."""
    cid, secret = os.environ.get("WSG_ARENA_CLIENT_ID"), os.environ.get("WSG_ARENA_CLIENT_SECRET")
    if not (cid and secret):
        try:
            c = json.loads(client_path().read_text(encoding="utf-8"))
            cid, secret = c.get("client_id"), c.get("client_secret")
        except (OSError, ValueError):
            pass
    if not (cid and secret):
        raise AuthError(
            f"Ingen Arena-klient. Lägg client_id och client_secret i {client_path()} "
            "(registreras i Arena med: node scripts/mcp-client.mjs create --name \"wsg — <namn>\" --api)")
    return cid, secret


# ── token cache ───────────────────────────────────────────────────────────────

def _read_cache():
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(tok):
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "url": app_url(),
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
        "signed_in_at": int(time.time()),
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(p)
    return data


def _token_endpoint(body):
    """POST /oauth/token with the client's credentials. Returns (json, error_json)."""
    cid, secret = client()
    data = parse.urlencode({**body, "client_id": cid, "client_secret": secret}).encode()
    req = request.Request(f"{app_url()}/oauth/token", data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), None
    except HTTPError as e:
        try:
            return None, json.loads(e.read())
        except ValueError:
            return None, {"error": f"HTTP {e.code}"}


# ── sign-in ───────────────────────────────────────────────────────────────────

_DONE_HTML = (
    "<!doctype html><meta charset='utf-8'><title>wsg</title>"
    "<body style='font-family:system-ui;padding:3em'>"
    "<h2>Inloggad i Arena.</h2><p>Du kan stänga den här fliken.</p>"
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def login(timeout=600, out=sys.stderr):
    """Authorization code + PKCE over a loopback redirect. Prints the URL, never opens a browser."""
    cid, _ = client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    port = _free_port()
    # Arena matches a loopback redirect on host and path, any port (RFC 8252).
    redirect = f"http://127.0.0.1:{port}/callback"
    url = f"{app_url()}/oauth/authorize?" + parse.urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    result = {}
    got = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = parse.urlsplit(self.path)
            q = dict(parse.parse_qsl(u.query))
            if u.path != "/callback" or ("code" not in q and "error" not in q):
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

    tok, err = _token_endpoint({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect,
        "code_verifier": verifier,
    })
    if not tok:
        err = err or {}
        raise AuthError(err.get("error_description") or err.get("error") or "tokenutbytet misslyckades")
    return _write_cache(tok)


def get_token():
    """A live access token, refreshed when it has run out. Raises AuthError when login is needed."""
    c = _read_cache()
    if not c or c.get("url") != app_url():
        raise AuthError(f"Inte inloggad mot {app_url()}. Kör: wsg arena login")
    if c.get("expires_at", 0) > time.time():
        return c["access_token"]
    if not c.get("refresh_token"):
        raise AuthError("Arena-sessionen har gått ut. Kör: wsg arena login")
    tok, err = _token_endpoint({"grant_type": "refresh_token", "refresh_token": c["refresh_token"]})
    if not tok:
        # A rotated-away or revoked refresh token cannot be retried: it is spent.
        raise AuthError(f"Arena-sessionen kunde inte förnyas ({(err or {}).get('error', '?')}). Kör: wsg arena login")
    return _write_cache(tok)["access_token"]


def logout():
    """Forget the tokens on this machine. Arena revokes them on offboarding, and they lapse after 30 days."""
    p = cache_path()
    if p.exists():
        p.unlink()
        return True
    return False


def status():
    """What the cache holds, or None. Does not call the network."""
    c = _read_cache()
    if not c or c.get("url") != app_url():
        return None
    return {
        "url": c["url"],
        "access_expires_at": c.get("expires_at", 0),
        "signed_in_at": c.get("signed_in_at"),
        "renewable": bool(c.get("refresh_token")),
        "cache": str(cache_path()),
    }


def api(method, path, body=None, token=None, timeout=60):
    """Call the Arena API. `path` is like '/api/me'. Returns parsed JSON (or text)."""
    tok = token or get_token()
    url = app_url() + (path if path.startswith("/") else "/" + path)
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, method=method, data=data, headers={
        "Authorization": f"Bearer {tok}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except HTTPError as e:
        raw = e.read()
        if e.code == 401:
            raise AuthError(f"Arena tog inte emot inloggningen ({method} {path}: 401). Kör: wsg arena login")
        try:
            detail = json.loads(raw).get("error") or raw.decode(errors="replace")
        except (ValueError, AttributeError):
            detail = raw.decode(errors="replace")
        raise AuthError(f"{method} {path}: HTTP {e.code}: {detail[:500]}")
    if "json" in ctype:
        return json.loads(raw) if raw else None
    head = raw.lstrip()[:200].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        # Pages serves the SPA shell for any unknown path, so a typo lands here.
        raise AuthError(f"{method} {path}: ingen API-route, fick appens HTML")
    return raw.decode(errors="replace")


def get(path, **kw):
    return api("GET", path, **kw)


# ── Companies, integrations and the secrets vault ─────────────────────────────
# The vault is the most sensitive surface this CLI touches: company.secrets.write at
# group scope, every write audited server-side. Two rules hold the whole design.
#
# A VALUE NEVER PASSES THROUGH ARGV. `set_secret` takes it as a parameter and the CLI
# only ever fills that from stdin, because an argument is visible in shell history and,
# on a shared box, in the process list. That is also why there is no --value flag to
# forget: the safe path is the only path.
#
# A VALUE NEVER COMES BACK. The API returns metadata only — id, name, hint, timestamps
# — so nothing here can print a credential even by accident, and rotation is the same
# call as creation rather than a read-modify-write.


def companies():
    """Every company in the registry: id, legal_name, org_no, group_role."""
    return get("/api/companies").get("companies") or []


def resolve_company(ref):
    """Accept a company id or a case-insensitive fragment of its legal name.

    An id is taken as given — the caller may know one this listing does not show.
    A fragment must match exactly one company, otherwise the error names the
    candidates rather than picking for you: these calls write to a named company's
    vault, and the wrong one is not a typo you notice afterwards.
    """
    rows = companies()
    by_id = {c["id"]: c for c in rows}
    if ref in by_id:
        return ref
    hits = [c for c in rows if ref.lower() in (c.get("legal_name") or "").lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        raise AuthError(f"Inget bolag matchar {ref!r}. Lista med: wsg arena companies")
    lines = chr(10).join(f"  {c['id']}  {c.get('legal_name')}" for c in hits[:8])
    raise AuthError(f"{len(hits)} bolag matchar {ref!r}, ange id:" + chr(10) + lines)


def integrations(company_id):
    """Integration rows for one company. Config is cleartext; credentials are not here."""
    return get("/api/companies/integrations?" + parse.urlencode({"companyId": company_id})).get("integrations") or []


def create_integration(company_id, key, instance, config=None, label=None):
    """Create one integration row. `key` must be a platform the catalog knows —
    an unknown key is accepted by the API's regex and then never pulled by anything."""
    body = {"action": "create", "companyId": company_id, "key": key, "instance": instance}
    if config is not None:
        body["config"] = config
    if label is not None:
        body["label"] = label
    return api("POST", "/api/companies/integrations", body=body)


def update_integration(company_id, integration_id, config=None, label=None, enabled=None):
    """Partial patch: an omitted field is left alone, which is also the API's contract."""
    body = {"action": "update", "companyId": company_id, "id": integration_id}
    if config is not None:
        body["config"] = config
    if label is not None:
        body["label"] = label
    if enabled is not None:
        body["enabled"] = bool(enabled)
    return api("POST", "/api/companies/integrations", body=body)


def secrets(company_id):
    """Secret METADATA for one company — never a value. Names, hints, who and when."""
    return get("/api/companies/secrets?" + parse.urlencode({"companyId": company_id})).get("secrets") or []


def set_secret(company_id, name, value, description=None, integration_id=None):
    """Store or rotate one secret. Same call either way, because a stored value cannot
    be read back to modify.

    `integration_id` is a PARTIAL PATCH on the server: omitting it leaves an existing
    link alone, and passing None explicitly detaches. So rotating a credential from a
    script cannot silently orphan it, which is a bug the API itself once had.
    """
    if not value:
        raise AuthError("tomt värde — vägrar skriva en tom secret")
    body = {"action": "set", "companyId": company_id, "name": name, "value": value}
    if description is not None:
        body["description"] = description
    if integration_id is not None:
        body["integrationId"] = integration_id
    return api("POST", "/api/companies/secrets", body=body)


def attach_secret(company_id, name, integration_id):
    """Re-parent an existing secret without knowing its value. `None` detaches."""
    return api("POST", "/api/companies/secrets",
               body={"action": "attach", "companyId": company_id, "name": name,
                     "integrationId": integration_id})


# ── Machine consumers and their grants ────────────────────────────────────────
# Who may read the vault over the machine API, and how widely. Three shapes exist
# server-side, narrowest first: one secret, one company's integration, or a PLATFORM
# grant covering every company that has an integration with that key.
#
# A platform grant is the wide one and it keeps widening: it covers companies
# onboarded AFTER it was given, with no edit and no notification. That is the point
# for a pipeline that runs group-wide, and the reason this function makes you pass
# all_companies explicitly rather than inferring it from a missing company.


def consumers():
    """Machine consumers with their grants. Metadata only — no secret values."""
    return get("/api/machine-admin/consumers").get("consumers") or []


def resolve_consumer(ref):
    """Accept a consumer id or its slug."""
    rows = consumers()
    for c in rows:
        if c.get("id") == ref or c.get("slug") == ref:
            return c
    known = ", ".join(sorted(c.get("slug") or "?" for c in rows))
    raise AuthError(f"Ingen consumer {ref!r}. Finns: {known}")


def grant(consumer_id, *, integration_key=None, secret_name=None, company_id=None,
          all_companies=False, remove=False):
    """Add or remove one grant. Exactly one shape per call, which the API enforces."""
    body = {"action": "remove" if remove else "add", "consumerId": consumer_id}
    if all_companies:
        body["allCompanies"] = True
    if company_id:
        body["companyId"] = company_id
    if integration_key:
        body["integrationKey"] = integration_key
    if secret_name:
        body["name"] = secret_name
    return api("POST", "/api/machine-admin/grants", body=body)


# ── Site Watch ────────────────────────────────────────────────────────────────
# monitor_incident: severity acute|watch|info, status open|acknowledged|resolved.
# "active" is the API's shorthand for status <> resolved.

def incidents(status="active", severity=None, company_id=None, days=None):
    """Site Watch incidents, newest first. severity may be one value or a list."""
    q = {"status": status} if status and status != "all" else {}
    if company_id:
        q["companyId"] = company_id
    if days:
        q["days"] = str(days)
    rows = get("/api/site-watch/incidents" + ("?" + parse.urlencode(q) if q else "")).get("incidents") or []
    if severity:
        want = {severity} if isinstance(severity, str) else set(severity)
        rows = [r for r in rows if r.get("severity") in want]
    return rows


def incident(incident_id):
    """One incident with its event timeline."""
    return get("/api/site-watch/incidents?" + parse.urlencode({"id": incident_id}))


def resolve_incident_ref(ref, status="active"):
    """Accept an incident id (26-char ULID) or a domain. A domain must match exactly
    one incident with the given status, otherwise AuthError says which ones."""
    if len(ref) == 26 and ref.isalnum():
        return ref
    hits = [r for r in incidents(status=status) if (r.get("domain") or "").lower() == ref.lower()]
    if len(hits) == 1:
        return hits[0]["id"]
    if not hits:
        raise AuthError(f"Ingen {status} incident för {ref}. Lista med: wsg arena incidents")
    shown = hits[:6]
    lines = "\n".join(f"  {h['id']}  {(h.get('openedAt') or '')[:16]}  {h.get('severity')}/{h.get('status')}  {h.get('title')}" for h in shown)
    more = f"\n  ... och {len(hits) - len(shown)} till (wsg arena incidents --status {status} --json)" if len(hits) > len(shown) else ""
    raise AuthError(f"{len(hits)} incidenter för {ref}, ange id:\n{lines}{more}")


def ack(incident_id):
    """Acknowledge: stops escalation. Needs sitewatch.read only."""
    return api("POST", "/api/site-watch/ack", body={"action": "ack", "id": incident_id})


def note(incident_id, cause=None, learning=None):
    """After-the-fact cause / learning note. Needs sitewatch.read only."""
    return api("POST", "/api/site-watch/ack", body={"action": "note", "id": incident_id, "cause": cause, "learning": learning})


def resolve(incident_id, false_positive=False):
    """Close an incident. Needs sitewatch.write."""
    return api("POST", "/api/site-watch/mutate", body={"action": "incident.resolve", "id": incident_id, "falsePositive": bool(false_positive)})


def reopen(incident_id):
    """Undo a resolve. Needs sitewatch.write."""
    return api("POST", "/api/site-watch/mutate", body={"action": "incident.reopen", "id": incident_id})
