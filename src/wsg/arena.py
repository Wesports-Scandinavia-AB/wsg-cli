"""
WeSports Arena: sign in through Cloudflare Access and call the Arena API.

Arena trusts Cloudflare Access, not Entra directly. The browser login goes
through Access (which federates to Entra), and Access hands back a JWT scoped
to the app. That JWT is what the API accepts, sent as the `cf-access-token`
header. The Access CLI flow (NaCl box against login.cloudflareaccess.org) is
not worth reimplementing without dependencies, so this module drives the
`cloudflared` binary, which also owns the token cache in ~/.cloudflared.

Session length is set by the Access app (hours, not the 90 days of the Graph
refresh token), so expect to run `wsg arena login` more often than `wsg graph login`.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib import parse, request
from urllib.error import HTTPError

from . import __version__
from .auth import AuthError, decode_jwt

DEFAULT_URL = "https://arena.wesports.io"
# Cloudflare's bot rules on the zone answer 403 (error 1010, browser_signature_banned)
# to the default "Python-urllib/x.y" agent, so identify as ourselves.
USER_AGENT = f"wsg/{__version__} (+https://github.com/Wesports-Scandinavia-AB/wsg-cli)"


def app_url():
    return os.environ.get("WSG_ARENA_URL", DEFAULT_URL).rstrip("/")


def _host():
    return parse.urlsplit(app_url()).netloc


def cloudflared():
    exe = os.environ.get("WSG_CLOUDFLARED") or shutil.which("cloudflared")
    if not exe and sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
            if base and (Path(base) / "cloudflared" / "cloudflared.exe").exists():
                exe = str(Path(base) / "cloudflared" / "cloudflared.exe")
                break
    if not exe:
        raise AuthError("cloudflared hittades inte. Installera: winget install Cloudflare.cloudflared "
                        "(eller sätt WSG_CLOUDFLARED till sökvägen)")
    return exe


def cache_dir():
    return Path(os.environ.get("TUNNEL_ORIGIN_CERT_DIR") or Path.home() / ".cloudflared")


def _cached_files(token=None):
    """Token files for this app: <host>-<aud>-token plus .url and .lock sidecars.

    The host in the file name is whatever URL was used at login (a pages.dev alias
    counts), so match on the AUD from the token when we have one.
    """
    if token is None:
        try:
            token = get_token()
        except AuthError:
            return sorted(cache_dir().glob(f"{_host()}-*-token*"))
    auds = decode_jwt(token).get("aud") or []
    auds = [auds] if isinstance(auds, str) else auds
    files = set()
    for aud in auds:
        files.update(cache_dir().glob(f"*-{aud}-token*"))
    return sorted(files)


def login(timeout=600):
    """Run `cloudflared access login`. cloudflared prints the URL on stderr; the JWT is never printed.

    cloudflared always tries to open the default browser itself; there is no flag
    to stop it. The URL is printed as well so a headless box or a second browser
    works too. Returns the JWT.
    """
    cmd = [cloudflared(), "access", "login", "--no-verbose", app_url()]
    try:
        # stderr carries the URL and progress, so stream it straight through.
        # stdout carries only the token because of --no-verbose.
        r = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        raise AuthError(f"Ingen inloggning inom {timeout} s")
    if r.returncode != 0:
        raise AuthError(f"cloudflared avslutades med kod {r.returncode}")
    tok = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if tok.count(".") != 2:
        raise AuthError("cloudflared gav ingen token")
    return tok


def get_token():
    """Return the cached Access JWT, or raise AuthError if login is needed."""
    r = subprocess.run([cloudflared(), "access", "token", "-app", app_url()],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    tok = r.stdout.strip()
    if r.returncode != 0 or tok.count(".") != 2:
        raise AuthError(f"Inte inloggad mot {app_url()}. Kör: wsg arena login")
    return tok


def logout():
    files = _cached_files()
    files = [f for f in files if not f.name.endswith(".lock")] + [f for f in files if f.name.endswith(".lock")]
    for f in files:
        try:
            f.unlink()
        except OSError:
            pass
    return bool(files)


def status():
    """Claims of the cached token, or None. Does not call the network."""
    try:
        tok = get_token()
    except AuthError:
        return None
    c = decode_jwt(tok)
    c["_files"] = [str(f) for f in _cached_files(tok)]
    return c


def api(method, path, body=None, token=None, timeout=60):
    """Call the Arena API. `path` is like '/api/me'. Returns parsed JSON (or text)."""
    tok = token or get_token()
    url = app_url() + (path if path.startswith("/") else "/" + path)
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(url, method=method, data=data, headers={
        "cf-access-token": tok,
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
        if e.code in (401, 403) and b"cloudflareaccess" in raw.lower():
            raise AuthError(f"Access släppte inte igenom ({e.code}). Kör: wsg arena login")
        try:
            detail = json.loads(raw).get("error") or raw.decode(errors="replace")
        except (ValueError, AttributeError):
            detail = raw.decode(errors="replace")
        raise AuthError(f"{method} {path}: HTTP {e.code}: {detail[:500]}")
    if "json" in ctype:
        return json.loads(raw) if raw else None
    head = raw.lstrip()[:200].lower()
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):
        if b"cloudflareaccess" in raw.lower():
            # Access redirected to its login page instead of letting us through.
            raise AuthError("Fick Access inloggningssida istället för JSON. Kör: wsg arena login")
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
