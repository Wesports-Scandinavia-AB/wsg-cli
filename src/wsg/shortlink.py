"""
Short links on go.wesports.io: turn a long URL into a short one that either lasts
for ever or is gone after one use, and which can insist on a click before it
spends that use.

Unlike `wsg secret create`, this needs a credential. secrets.wesports.io is public;
go.wesports.io is not, because an open redirector on a wesports.io hostname is a
phishing tool wearing our domain. Creating a link in the browser goes through
Tribune, and this goes through the service's machine door: `/api/machine/*` behind
its own Cloudflare Access application, with one service token in its policy. Two
headers, no browser.

Every link made this way belongs to the person the service is configured with, so
it turns up in the same list at https://go.wesports.io and can be revoked there as
well as here.

Credentials come from WSG_GO_CLIENT_ID / WSG_GO_CLIENT_SECRET, or from
`go.json` in the config directory (`%LOCALAPPDATA%\\wsg` on Windows,
`~/.config/wsg` otherwise):

    {"client_id": "....access", "client_secret": "..."}
"""

import json
import os
import re
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError

BASE = os.environ.get("WSG_GO_URL", "https://go.wesports.io").rstrip("/")

# The lifetimes the service offers. "forever" is the default because that is what
# somebody shortening a URL almost always means; the interesting one is one flag
# away.
MODES = ("forever", "once", "times", "until")


class ShortLinkError(Exception):
    pass


def _config_dir():
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "wsg" if base else Path.home() / ".config" / "wsg"


def client_path():
    return Path(os.environ.get("WSG_GO_CLIENT") or _config_dir() / "go.json")


def _credentials():
    cid = os.environ.get("WSG_GO_CLIENT_ID")
    secret = os.environ.get("WSG_GO_CLIENT_SECRET")
    if not (cid and secret):
        try:
            c = json.loads(client_path().read_text(encoding="utf-8"))
            cid, secret = c.get("client_id"), c.get("client_secret")
        except (OSError, ValueError):
            pass
    if not (cid and secret):
        raise ShortLinkError(
            f"Ingen service token. Lägg client_id och client_secret i {client_path()}, "
            "eller sätt WSG_GO_CLIENT_ID och WSG_GO_CLIENT_SECRET."
        )
    return cid, secret


def normalise_until(value):
    """What a person types, as something the service will read as UTC.

    A bare date means through the end of that day, not its first second: "--until
    2026-10-23" is nearly always "it should still work on the 23rd". A space
    instead of a T is accepted because that is how a date reads out loud.
    """
    v = (value or "").strip().replace(" ", "T")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v + "T23:59"
    return v


def create(target, *, mode="forever", uses=None, until=None, name=None, title=None,
           confirm=False):
    """Make a link. Returns the service's JSON, whose `url` is the short link.

    `confirm` makes the link always open a page with a button, and nothing but
    that button can spend its use. Without it the service reads `Sec-Fetch-*`
    and lets a request that is unmistakably somebody clicking through in one
    hop, which is right nearly always and is still a judgement. The service
    refuses `confirm` on a link with no use limit: there would be nothing to
    protect.
    """
    if mode not in MODES:
        raise ShortLinkError(f"mode måste vara en av {', '.join(MODES)}")
    if confirm and mode not in ("once", "times"):
        raise ShortLinkError("--confirm går bara ihop med --once eller --times: "
                             "en länk utan användningsgräns har inget att spara.")
    body = {"target": target, "mode": mode}
    if mode == "times":
        body["uses"] = uses
    if mode == "until":
        body["until"] = normalise_until(until)
    if name:
        body["slug"] = name
    if title:
        body["title"] = title
    if confirm:
        body["confirm"] = True
    return _call("POST", "/api/machine/links", body)


def links():
    """Every link belonging to the configured person, newest first."""
    return _call("GET", "/api/machine/links").get("links", [])


def revoke(slug):
    """Withdraw a link. It stops working for everyone, including people who have
    already used it — there is no cached redirect to outlive this."""
    return _call("POST", f"/api/machine/links/{slug}/revoke")


def _call(method, path, body=None):
    cid, secret = _credentials()
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "content-type": "application/json",
            "user-agent": "wsg-cli",
            "CF-Access-Client-Id": cid,
            "CF-Access-Client-Secret": secret,
        },
    )
    try:
        with request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except HTTPError as e:
        raw = e.read()
        try:
            msg = json.loads(raw).get("error") or e.reason
        except ValueError:
            # Access refuses before the Worker is reached, and answers with HTML.
            # Saying so is more use than printing a page of markup.
            msg = (
                "Cloudflare Access släppte inte igenom anropet — fel eller "
                "återkallad service token" if e.code in (401, 403) else e.reason
            )
        raise ShortLinkError(f"{method} {path}: HTTP {e.code}: {msg}") from None
    except URLError as e:
        raise ShortLinkError(f"{BASE} svarar inte: {e.reason}") from None
