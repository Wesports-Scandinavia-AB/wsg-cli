import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import arena, auth, mscal, onetime, planner, shortlink
from .scopes import DEFAULT_SCOPES


def _scopes_arg(s, default=DEFAULT_SCOPES):
    return [x.strip() for x in s.split(",") if x.strip()] if s else list(default)


def _plugins(group):
    """Entry points in `group`, from packages installed next to this one.

    wsg-admin-cli registers its commands (`wsg.commands`) this way, so they keep
    their names without wsg-cli knowing they exist. entry_points() grew its
    `group=` keyword in 3.10; 3.9 returns a dict instead.
    """
    from importlib.metadata import entry_points
    eps = entry_points()
    return list(eps.select(group=group) if hasattr(eps, "select") else eps.get(group, []))


def _stdin_text():
    r"""Read stdin, minus the byte order mark PowerShell puts in front of a pipe.

    `@'...'@ | wsg ...` writes a UTF-8 BOM before the first real byte, so the value
    arrives as '﻿SELECT ...' or '﻿hunter2'. Nothing shows it: it is a
    zero-width character, it survives .strip(), and every command here reads a
    value whose exact bytes matter. A secret stored with one in front is quietly
    the wrong secret, a password set that way cannot be typed at a login prompt,
    and a mail body carries an invisible first character into every inbox.

    Only the leading BOM goes. Trailing whitespace is each caller's business, and
    they already disagree about it on purpose — a password may end in a space.
    """
    return sys.stdin.read().lstrip("﻿")


def cmd_graph_login(a):
    scopes = _scopes_arg(a.scopes, a.default_scopes)
    try:
        if a.device:
            data = auth.login_device(scopes, client=a.client)
        else:
            data = auth.login(scopes, open_browser=a.open, timeout=a.timeout, login_hint=a.user, browser=a.browser,
                              client=a.client)
    except auth.AuthError as e:
        print(f"Inloggningen misslyckades: {e}", file=sys.stderr)
        return 1
    claims = auth.decode_jwt(data["access_token"])
    print(f"Inloggad som {claims.get('upn') or claims.get('preferred_username', '?')}", file=sys.stderr)
    missing = set(scopes) - auth.granted_scopes(data)
    if missing:
        print("Ej beviljade scopes: " + ", ".join(sorted(missing)), file=sys.stderr)
    print(f"Token sparad i {auth.cache_path(a.client)}", file=sys.stderr)
    return 0


def cmd_graph_token(a):
    try:
        tok = auth.get_token(_scopes_arg(a.scopes) if a.scopes else None, a.client)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"Bearer {tok}" if a.header else tok)
    return 0


def cmd_graph_status(a):
    c = auth.read_cache(a.client)
    if not c:
        print(f"Inte inloggad. Kör: {a.client.login_command}")
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
            "cache": str(auth.cache_path(a.client)),
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"Användare:     {user} ({claims.get('name', '')})")
    print(f"Tenant:        {claims.get('tid', '?')}")
    print(f"Access token:  {'giltig till ' + exp.strftime('%Y-%m-%d %H:%M') if fresh else 'utgången, förnyas vid nästa anrop'}")
    print(f"Refresh token: {'ja' if c.get('refresh_token') else 'nej'}")
    print(f"Cache:         {auth.cache_path(a.client)}")
    print("Scopes:")
    for s in sorted(auth.granted_scopes(c)):
        print(f"  {s}")
    return 0


def cmd_graph_whoami(a):
    try:
        me = auth.graph("GET", "/me", client=a.client)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{me.get('displayName')} <{me.get('userPrincipalName')}>")
    return 0


def cmd_graph_logout(a):
    print("Cache borttagen." if auth.logout(a.client) else "Ingen cache fanns.")
    return 0


def cmd_graph_sendmail(a):
    """Send mail as the signed-in user via POST /me/sendMail.

    NOT a draft and not the M365 connector: sendMail delivers, and a copy lands in
    Sent Items unless --no-save. The body is read from stdin, the same way secret-set
    reads a value — a message worth sending is longer than an argument should be, and
    a here-doc keeps line breaks intact where a shell argument mangles them.

    Attachments go inline as #microsoft.graph.fileAttachment, base64 in contentBytes,
    which is why Graph caps the whole request at roughly 3 MB. Above that the file has
    to go up through createUploadSession against a draft, or into OneDrive as a link —
    neither of which this command pretends to do, so it refuses up front instead of
    failing halfway with an error nobody can act on.

    --send-at hands the message to Exchange now with PidTagDeferredSendTime set, so
    it waits in Outbox and leaves at that time even if this machine is off. Deleting
    it from Outbox before then cancels it.
    """
    import base64
    import mimetypes

    recipients = [x.strip() for x in (a.to or "").split(",") if x.strip()]
    cc = [x.strip() for x in (a.cc or "").split(",") if x.strip()]
    if not recipients:
        print("--to kraver minst en adress (kommaseparerat for flera)", file=sys.stderr)
        return 2
    send_at = None
    if a.send_at:
        try:
            # A time without an offset is local time, the way a person says "08:00".
            send_at = datetime.fromisoformat(a.send_at).astimezone()
        except ValueError:
            print(f"--send-at forstas inte: {a.send_at!r}. Skriv t.ex. 2026-10-15T08:00",
                  file=sys.stderr)
            return 2
        if send_at <= datetime.now().astimezone():
            print(f"--send-at {send_at:%Y-%m-%d %H:%M} har redan passerat.", file=sys.stderr)
            return 2
    if sys.stdin.isatty():
        print("Brodtexten lases fran stdin. Pipa in den, t.ex. med en here-doc.", file=sys.stderr)
        return 2
    body_text = _stdin_text()
    if not body_text.strip():
        print("Tom brodtext, avbryter.", file=sys.stderr)
        return 2

    attachments = []
    total = 0
    for path in a.attach or []:
        f = Path(path)
        if not f.is_file():
            print(f"Bilagan finns inte: {path}", file=sys.stderr)
            return 2
        raw = f.read_bytes()
        total += len(raw)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        attachments.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": f.name,
            "contentType": ctype,
            "contentBytes": base64.b64encode(raw).decode(),
        })
    # Base64 costs a third on top, and Graph rejects the whole request rather than the
    # attachment, so the limit is checked against the ENCODED size before sending.
    if total * 4 // 3 > 3 * 1024 * 1024:
        print(f"Bilagorna ar {total // 1024} kB, for stort for ett inline-utskick "
              "(~3 MB efter base64). Lagg filen i OneDrive och skicka en lank.",
              file=sys.stderr)
        return 2

    # Markdown by default: a text body shows **bold** with its asterisks and code
    # like the rest of the text. --html sends HTML as it is, --plain text as it is.
    if a.plain:
        body = {"contentType": "Text", "content": body_text}
    elif a.html:
        body = {"contentType": "HTML", "content": body_text}
    else:
        from .markdown import to_mail_html
        body = {"contentType": "HTML", "content": to_mail_html(body_text)}
    message = {
        "subject": a.subject or "",
        "body": body,
        "toRecipients": [{"emailAddress": {"address": x}} for x in recipients],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": x}} for x in cc]
    if attachments:
        message["attachments"] = attachments
    if send_at:
        utc = send_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        message["singleValueExtendedProperties"] = [
            {"id": "SystemTime 0x3FEF", "value": utc}]
    payload = {"message": message, "saveToSentItems": not a.no_save}

    if a.dry_run:
        preview = dict(message)
        if attachments:
            # The base64 never helps a human read the plan; the size does.
            preview["attachments"] = [
                {"name": att["name"], "contentType": att["contentType"],
                 "base64Bytes": len(att["contentBytes"])}
                for att in attachments
            ]
        print("[torrkorning] POST /me/sendMail - inget skickat")
        if send_at:
            print(f"Skulle ga {send_at:%Y-%m-%d %H:%M} ({utc} UTC)")
        print(json.dumps({"message": preview, "saveToSentItems": payload["saveToSentItems"]},
                         indent=2, ensure_ascii=False))
        return 0

    try:
        auth.graph("POST", "/me/sendMail", body=payload)
    except Exception as e:  # urllib raises HTTPError; its body carries Graph's reason
        detail = ""
        read = getattr(e, "read", None)
        if callable(read):
            try:
                detail = ": " + read().decode(errors="replace")[:400]
            except Exception:
                detail = ""
        print(f"Sandningen misslyckades{detail or ': ' + str(e)}", file=sys.stderr)
        return 2
    if send_at:
        print(f"Lagt i Utkorgen till {', '.join(recipients)}, gar {send_at:%Y-%m-%d %H:%M}."
              " Ta bort det ur Utkorgen for att stoppa det.")
        return 0
    print(f"Skickat till {', '.join(recipients)}"
          + ("" if a.no_save else " (kopia i Skickat)"))
    return 0


def cmd_arena_login(a):
    try:
        arena.login(timeout=a.timeout)
        me = arena.get("/api/me")
    except auth.AuthError as e:
        print(f"Inloggningen misslyckades: {e}", file=sys.stderr)
        return 1
    m = (me or {}).get("member") or {}
    print(f"Inloggad mot {arena.app_url()} som {m.get('email', '?')}", file=sys.stderr)
    return 0


def cmd_arena_token(a):
    try:
        tok = arena.get_token()
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"Authorization: Bearer {tok}" if a.header else tok)
    return 0


def cmd_arena_status(a):
    c = arena.status()
    if not c:
        print(f"Inte inloggad mot {arena.app_url()}. Kör: wsg arena login")
        return 2
    exp = datetime.fromtimestamp(c["access_expires_at"])
    since = datetime.fromtimestamp(c["signed_in_at"]) if c.get("signed_in_at") else None
    live = c["access_expires_at"] > time.time()
    if a.json:
        print(json.dumps({
            "url": c["url"],
            "access_expires_at": exp.isoformat(timespec="seconds"),
            "access_valid": live,
            "renewable": c["renewable"],
            "token_issued_at": since.isoformat(timespec="seconds") if since else None,
            "cache": c["cache"],
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"App:      {c['url']}")
    print(f"Token:    {'giltig till ' + exp.strftime('%Y-%m-%d %H:%M') if live else 'utgången'}"
          f"{', förnyas av sig själv' if c['renewable'] else ', kör: wsg arena login'}")
    if since:
        until = datetime.fromtimestamp(c["signed_in_at"] + arena.SIGN_IN_TTL_SECONDS)
        print(f"Inloggad: {since.strftime('%Y-%m-%d %H:%M')} (gäller till {until.strftime('%Y-%m-%d %H:%M')}, sedan loggar wsg in igen)")
    print(f"Cache:    {c['cache']}")
    return 0


def cmd_arena_whoami(a):
    try:
        me = arena.get("/api/me")
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    m = me.get("member") or {}
    print(f"{m.get('displayName')} <{m.get('email')}>  status={m.get('status')}  assurance={me.get('assurance')}")
    n = len(me.get("companies") or [])
    print(f"{n} bolag. Hela svaret: wsg arena get /api/me")
    return 0


def cmd_arena_get(a):
    try:
        data = arena.get(a.path)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, indent=None if a.compact else 2, ensure_ascii=False))
    return 0


def cmd_arena_companies(a):
    try:
        rows = arena.companies()
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.filter:
        rows = [c for c in rows if a.filter.lower() in json.dumps(c, ensure_ascii=False).lower()]
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for c in rows:
        print(f"{c['id']:<24} {c.get('legal_name') or '':<38} {c.get('org_no') or '':<12} {c.get('group_role') or ''}")
    print(f"{len(rows)} bolag", file=sys.stderr)
    return 0


def cmd_arena_integrations(a):
    try:
        co = arena.resolve_company(a.company)
        rows = arena.integrations(co)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(f"Inga integrationer på {co}.")
        return 0
    for i in rows:
        cfg = json.dumps(i.get("config") or {}, ensure_ascii=False)
        flag = "" if i.get("enabled", True) else "  [avstängd]"
        print(f"{i.get('key'):<18} {i.get('instance'):<14} {cfg}{flag}")
        if a.verbose:
            print(f"{'':18} id={i.get('id')}")
    return 0


def cmd_arena_integration_add(a):
    try:
        co = arena.resolve_company(a.company)
        config = json.loads(a.config) if a.config else None
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"--config är inte giltig JSON: {e}", file=sys.stderr)
        return 2
    if config is not None and not isinstance(config, dict):
        print("--config måste vara ett JSON-objekt", file=sys.stderr)
        return 2
    try:
        r = arena.create_integration(co, a.key, a.instance, config=config, label=a.label)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    i = r.get("integration") or {}
    print(f"Skapad: {i.get('key')} · {i.get('instance')} på {co}")
    print(f"id={i.get('id')}")
    return 0


def cmd_arena_secrets(a):
    try:
        co = arena.resolve_company(a.company)
        rows = arena.secrets(co)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(f"Inga secrets på {co}.")
        return 0
    for r in rows:
        # No value is ever returned by the API, so there is nothing here to redact.
        link = r.get("integration_id") or r.get("integrationId") or ""
        print(f"{r.get('name'):<34} {(r.get('hint') or ''):<14} {(r.get('updated_at') or r.get('updatedAt') or '')[:16]}  {link}")
    return 0


def cmd_arena_secret_set(a):
    """Store or rotate one secret. The value is read from STDIN and never from argv:
    an argument lands in shell history and in the process list, and this is a
    credential. There is deliberately no --value flag."""
    try:
        co = arena.resolve_company(a.company)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if sys.stdin.isatty():
        import getpass
        value = getpass.getpass(f"Värde för {a.name} på {co} (visas inte): ")
    else:
        # A trailing newline from a pipe or heredoc is the shell's, not the secret's.
        # Anything else is kept: some credentials legitimately contain whitespace.
        value = _stdin_text()
        # Exactly one trailing line ending is removed, because it belongs to the
        # pipe or heredoc rather than to the secret. Anything beyond that is kept:
        # a credential may legitimately end in whitespace, and silently trimming it
        # would store something that does not authenticate.
        for eol in (chr(13) + chr(10), chr(10), chr(13)):
            if value.endswith(eol):
                value = value[: -len(eol)]
                break
    if not value:
        print("Tomt värde, avbryter.", file=sys.stderr)
        return 2
    existing = {}
    try:
        existing = {r.get("name"): r for r in arena.secrets(co)}
    except auth.AuthError:
        pass
    verb = "Roterar" if a.name in existing else "Skapar"
    if a.dry_run:
        print(f"[torrkörning] {verb} {a.name} på {co}"
              + (f", länkad till integration {a.integration}" if a.integration else "")
              + f" ({len(value)} tecken, inget skickat)")
        return 0
    try:
        r = arena.set_secret(co, a.name, value,
                             description=a.description, integration_id=a.integration)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    sec = r.get("secret") or {}
    print(f"{verb[:-1]}d: {a.name} på {co}"
          + (f"  hint={sec.get('hint')}" if sec.get("hint") else ""))
    print("Värdet kan inte läsas tillbaka. Rotera genom att köra kommandot igen.", file=sys.stderr)
    return 0


def cmd_arena_consumers(a):
    try:
        rows = arena.consumers()
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for c in rows:
        if a.consumer and a.consumer not in (c.get("slug"), c.get("id")):
            continue
        print(f"{c.get('slug')}  id={c.get('id')}")
        for g in c.get("grants") or []:
            print(f"    secret    {g.get('company_id')}  {g.get('secret_name')}")
        for g in c.get("platformGrants") or c.get("platform_grants") or []:
            # Group-wide by construction: this shape has no company column.
            print(f"    platform  every company  key={g.get('integration_key')}")
    return 0


def cmd_arena_grant(a):
    remove = a.fn_action == "remove"
    shapes = [bool(a.key and a.all_companies), bool(a.key and a.company), bool(a.secret and a.company)]
    if sum(shapes) != 1:
        print("Ange exakt en form: --key med --all-companies, --key med --company, "
              "eller --secret med --company.", file=sys.stderr)
        return 2
    try:
        consumer = arena.resolve_consumer(a.consumer)
        company = arena.resolve_company(a.company) if a.company else None
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    what = (f"plattformsgrant {a.key} för ALLA bolag" if a.all_companies
            else f"integrationsgrant {a.key} på {company}" if a.key
            else f"secret {a.secret} på {company}")
    if a.dry_run:
        print(f"[torrkörning] Skulle {'ta bort' if remove else 'ge'} {consumer['slug']}: {what}")
        return 0
    try:
        r = arena.grant(consumer["id"], integration_key=a.key, secret_name=a.secret,
                        company_id=company, all_companies=a.all_companies, remove=remove)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    changed = r.get("added") if not remove else r.get("removed")
    verb = "Borttagen" if remove else "Given"
    if changed is False:
        print(f"Oförändrad — {consumer['slug']} hade redan detta läge: {what}")
    else:
        print(f"{verb}: {consumer['slug']} → {what}")
    if a.all_companies and not remove:
        print("Plattformsgrant gäller även bolag som läggs till senare, utan ny åtgärd.",
              file=sys.stderr)
    return 0


def cmd_arena_incidents(a):
    try:
        if a.id:
            print(json.dumps(arena.incident(a.id), indent=2, ensure_ascii=False))
            return 0
        sev = [x.strip() for x in a.severity.split(",")] if a.severity else None
        rows = arena.incidents(status=a.status, severity=sev, company_id=a.company, days=a.days)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Inga incidenter matchar.")
        return 0
    label = {"acute": "INCIDENT", "watch": "WARNING", "info": "info"}
    w = max(len(r.get("domain") or r.get("platform") or "-") for r in rows)
    for r in rows:
        who = r.get("domain") or (f"[{r.get('platform')}]" if r.get("platform") else "-")
        ack = f" ack: {r['acknowledgedByName']}" if r.get("acknowledgedByName") else ""
        print(f"{label.get(r.get('severity'), r.get('severity')):8} {r.get('status'):12} "
              f"{(r.get('openedAt') or '')[:16]}  {who:{w}}  {r.get('title')}{ack}")
        if a.verbose and r.get("summary"):
            print(f"{'':8} {'':12} {'':16}  {'':{w}}  {r['summary']}")
            print(f"{'':8} {'':12} {'':16}  {'':{w}}  id={r.get('id')}")
    n_ac = sum(1 for r in rows if r.get("severity") == "acute")
    n_wa = sum(1 for r in rows if r.get("severity") == "watch")
    print(f"\n{len(rows)} st: {n_ac} incidenter (acute), {n_wa} varningar (watch), {len(rows) - n_ac - n_wa} info")
    return 0


def _incident_action(a, action):
    """Shared driver for ack / resolve / reopen: resolve the reference, act, report."""
    try:
        want = "resolved" if action == "reopen" else "active"
        iid = arena.resolve_incident_ref(a.incident, status=want)
        before = (arena.incident(iid) or {}).get("incident") or {}
        if action == "ack":
            r = arena.ack(iid)
        elif action == "reopen":
            r = arena.reopen(iid)
        else:
            r = arena.resolve(iid, false_positive=a.false_positive)
            if a.cause or a.learning:
                arena.note(iid, cause=a.cause, learning=a.learning)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    title = before.get("title") or iid
    if not (r or {}).get("ok"):
        was = before.get("status") or "?"
        print(f"Ingen ändring: {title} är redan {was}.", file=sys.stderr)
        return 1
    done = {"ack": "acknowledgad", "resolve": "stängd", "reopen": "återöppnad"}[action]
    extra = " som falsklarm" if action == "resolve" and a.false_positive else ""
    print(f"{title}: {done}{extra}. id={iid}")
    return 0


def cmd_arena_ack(a):
    return _incident_action(a, "ack")


def cmd_arena_resolve(a):
    return _incident_action(a, "resolve")


def cmd_arena_reopen(a):
    return _incident_action(a, "reopen")


def cmd_arena_logout(a):
    print("Arena-session borttagen." if arena.logout() else "Ingen Arena-session fanns.")
    return 0


def _never(s):
    """True for a timestamp that stands for "never".

    Graph writes 0001-01-01T00:00:00Z rather than omitting the field: a device
    that has not checked in yet, a record nobody has booted. It is a sentinel,
    not a date, and astimezone() raises OSError on year 1 on Windows, so it has
    to be caught before anything tries to convert it."""
    return not s or s.startswith("0001-01-01")


def _ts(s, with_time=True):
    """Graph timestamps are ISO 8601 in UTC; show them as local wall clock."""
    if _never(s):
        return "-"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except (ValueError, OSError):
        return s
    return d.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def _add_graph_commands(sub, visible=True, client=None, scopes=DEFAULT_SCOPES, sendmail=True):
    """Graph subcommands. Registered under `wsg graph` and, for compatibility with
    scripts written against 0.1 (`wsg token --header`), as hidden top-level aliases.
    wsg-admin-cli registers them a third time as `wsg admin`, against its own app
    (client) and scopes, without sendmail: mail goes out as yourself, from the
    ordinary login."""
    # argparse lists a subcommand in --help only when the `help` kwarg is present,
    # so the aliases are added without one.
    h = (lambda t: {"help": t}) if visible else (lambda t: {})
    client = client or auth.CORE

    def add(name, **kw):
        s = sub.add_parser(name, **kw)
        s.set_defaults(client=client, default_scopes=scopes)
        return s

    s = add("login", **h("sign in interactively and cache the token"))
    s.add_argument("--scopes", help="comma separated, default is the whole set this login is for")
    s.add_argument("--open", action="store_true", help="open the default browser instead of just printing the URL")
    s.add_argument("--browser", metavar="NAME", help="open in this browser: edge, chrome, firefox, or an executable")
    s.add_argument("--device", action="store_true", help="use device code flow (for machines without a browser)")
    s.add_argument("--user", help="login hint, e.g. name@domain")
    s.add_argument("--timeout", type=int, default=600, help="seconds to wait for the browser, default 600")
    s.set_defaults(fn=cmd_graph_login)

    s = add("token", **h("print a valid access token, refreshing silently"))
    s.add_argument("--scopes", help="require these scopes (comma separated)")
    s.add_argument("--header", action="store_true", help="print as 'Bearer <token>'")
    s.set_defaults(fn=cmd_graph_token)

    s = add("status", **h("show who is signed in and what was granted"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_graph_status)

    s = add("whoami", **h("call Graph /me"))
    s.set_defaults(fn=cmd_graph_whoami)

    if not sendmail:
        s = add("logout", **h("delete the cached token"))
        s.set_defaults(fn=cmd_graph_logout)
        return

    s = add("sendmail", **h("send mail as yourself via /me/sendMail; markdown body read from stdin"))
    s.add_argument("--to", required=True, help="recipient, or several comma-separated")
    s.add_argument("--cc", help="cc recipients, comma-separated")
    s.add_argument("--subject", required=True)
    s.add_argument("--attach", action="append", metavar="FILE",
                   help="attach a file inline (repeatable; ~3 MB total after base64)")
    fmt = s.add_mutually_exclusive_group()
    fmt.add_argument("--html", action="store_true", help="the body is HTML already, send it as it is")
    fmt.add_argument("--plain", action="store_true", help="send the body as plain text, not rendered from markdown")
    s.add_argument("--no-save", action="store_true", help="do not keep a copy in Sent Items")
    s.add_argument("--send-at", metavar="TIME",
                   help="send later, e.g. 2026-10-15T08:00 (local time); waits in Outbox")
    s.add_argument("--dry-run", action="store_true", help="print the exact request and send nothing")
    s.set_defaults(fn=cmd_graph_sendmail)

    s = add("logout", **h("delete the cached token"))
    s.set_defaults(fn=cmd_graph_logout)


def _add_arena_commands(sub):
    s = sub.add_parser("login", help="sign in to Arena (OAuth in the browser, prints the URL)")
    s.add_argument("--timeout", type=int, default=600, help="seconds to wait for the browser, default 600")
    s.set_defaults(fn=cmd_arena_login)

    s = sub.add_parser("token", help="print a live Arena access token (renews it when needed)")
    s.add_argument("--header", action="store_true", help="print as 'Authorization: Bearer <token>'")
    s.set_defaults(fn=cmd_arena_token)

    s = sub.add_parser("status", help="the Arena session on this machine and until when")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_arena_status)

    s = sub.add_parser("whoami", help="call Arena /api/me")
    s.set_defaults(fn=cmd_arena_whoami)

    s = sub.add_parser("get", help="GET an Arena API path and print the JSON, e.g. wsg arena get /api/me")
    s.add_argument("path")
    s.add_argument("--compact", action="store_true", help="one-line JSON")
    s.set_defaults(fn=cmd_arena_get)

    s = sub.add_parser("companies", help="list the companies in the registry (id, legal name, org no)")
    s.add_argument("--filter", help="substring match anywhere in the row")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_arena_companies)

    co_help = "company id (co_vartex) or a unique fragment of its legal name"

    s = sub.add_parser("integrations", help="integration rows for one company (config is cleartext, credentials are not here)")
    s.add_argument("--company", required=True, metavar="REF", help=co_help)
    s.add_argument("-v", "--verbose", action="store_true", help="also print the row id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_arena_integrations)

    s = sub.add_parser("integration-add", help="create an integration row (needs company.secrets.write)")
    s.add_argument("--company", required=True, metavar="REF", help=co_help)
    s.add_argument("--key", required=True, help="platform key from the catalog, e.g. garp")
    s.add_argument("--instance", required=True, help="instance slug, unique per (company, key)")
    s.add_argument("--config", help="JSON object of the non-secret config fields")
    s.add_argument("--label", help="free-text label shown in the UI")
    s.set_defaults(fn=cmd_arena_integration_add)

    s = sub.add_parser("secrets", help="secret NAMES and metadata for one company — values are never returned")
    s.add_argument("--company", required=True, metavar="REF", help=co_help)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_arena_secrets)

    s = sub.add_parser("secret-set", help="store or rotate a secret; the VALUE is read from stdin, never from an argument")
    s.add_argument("--company", required=True, metavar="REF", help=co_help)
    s.add_argument("--name", required=True, help="secret name, env-var style, e.g. GARP_DB_PASSWORD")
    s.add_argument("--integration", metavar="ID", help="link it to an integration row id")
    s.add_argument("--description", help="what this credential is, shown in the UI")
    s.add_argument("--dry-run", action="store_true", help="say what would be written, send nothing")
    s.set_defaults(fn=cmd_arena_secret_set)

    s = sub.add_parser("consumers", help="machine consumers (Access service tokens) and what they may read")
    s.add_argument("--consumer", metavar="SLUG", help="show only this one")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_arena_consumers)

    def _grant_args(s):
        s.add_argument("--consumer", required=True, metavar="SLUG", help="consumer slug or id")
        s.add_argument("--key", help="platform key, e.g. garp")
        s.add_argument("--company", metavar="REF", help=co_help)
        s.add_argument("--secret", metavar="NAME", help="a single secret name")
        s.add_argument("--all-companies", action="store_true",
                       help="with --key: every company that has this platform, including future ones")
        s.add_argument("--dry-run", action="store_true", help="say what would change, send nothing")

    s = sub.add_parser("grant-add", help="let a machine consumer read secrets (needs company.secrets.write)")
    _grant_args(s)
    s.set_defaults(fn=cmd_arena_grant, fn_action="add")

    s = sub.add_parser("grant-remove", help="withdraw a machine consumer's grant")
    _grant_args(s)
    s.set_defaults(fn=cmd_arena_grant, fn_action="remove")

    s = sub.add_parser("incidents", help="Site Watch incidents (acute) and warnings (watch); open ones by default")
    s.add_argument("--status", default="active", help="active (default, = not resolved), open, acknowledged, resolved, all")
    s.add_argument("--severity", help="acute, watch, info, or a comma list")
    s.add_argument("--company", metavar="ID", help="narrow to one company id, e.g. co_rawbike")
    s.add_argument("--days", type=int, help="look back N days (server default 90, max 365)")
    s.add_argument("--id", help="show one incident with its event timeline (JSON)")
    s.add_argument("-v", "--verbose", action="store_true", help="also print summary and id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_arena_incidents)

    ref_help = "incident id, or a domain that has exactly one matching incident"

    s = sub.add_parser("ack", help="acknowledge an incident (stops escalation; sitewatch.read is enough)")
    s.add_argument("incident", help=ref_help)
    s.set_defaults(fn=cmd_arena_ack)

    s = sub.add_parser("resolve", help="close an incident (needs sitewatch.write)")
    s.add_argument("incident", help=ref_help)
    s.add_argument("--false-positive", action="store_true", help="mark it as a false alarm (feeds threshold tuning)")
    s.add_argument("--cause", help="what it was, saved on the incident")
    s.add_argument("--learning", help="what we take with us, saved on the incident")
    s.set_defaults(fn=cmd_arena_resolve)

    s = sub.add_parser("reopen", help="undo a resolve (needs sitewatch.write)")
    s.add_argument("incident", help=ref_help)
    s.set_defaults(fn=cmd_arena_reopen)

    s = sub.add_parser("logout", help="delete the cached Access token for Arena")
    s.set_defaults(fn=cmd_arena_logout)


# ── Shared time formatting ───────────────────────────────────────────────────
# Used by the Ongoing commands; kept general because timestamps from any of these
# APIs arrive in the same handful of shapes.


def _dt(s):
    """Parse an API timestamp. .NET writes up to 7 fractional digits, which
    fromisoformat rejects before 3.11, so trim to microseconds."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    m = re.match(r"^(.*\.\d{6})\d*(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _for(s):
    """How long ago, as a duration a human reads at a glance."""
    d = _dt(s)
    if not d:
        return "-"
    secs = (datetime.now(d.tzinfo) - d).total_seconds()
    if secs < 0:
        return "nyss"
    if secs < 3600:
        return f"{int(secs // 60)} min"
    if secs < 86400:
        return f"{int(secs // 3600)} h {int(secs % 3600 // 60)} min"
    return f"{int(secs // 86400)} d {int(secs % 86400 // 3600)} h"


def _clock(s):
    d = _dt(s)
    return d.astimezone().strftime("%H:%M") if d else "-"





# ── The work calendar ─────────────────────────────────────────────────────────
# REKONSTRUERAD 2026-09-13 efter att originalet raderats av misstag. Logiken i
# mscal.py är orörd; det här är bara CLI-lagret, återskapat från README:ns
# beskrivning och mscal:s API. Jämför med vad du hade innan du litar på det.


def _event_ts(block, with_time=True):
    """One side of a Graph event as local wall clock.

    Graph puts the zone in a sibling field rather than on the timestamp, so the
    naive string reaching _ts was taken for local time and printed unconverted -
    a booking at 09:15 read as 07:15. Only UTC is resolved here: that is what
    mirroring writes for a timed block, and an all-day one already carries the
    mailbox's own wall clock, which is the right thing to show as it stands.
    """
    when = (block or {}).get("dateTime")
    if not when:
        return "-"
    if (block.get("timeZone") or "").upper() == "UTC" and not when.endswith(("Z", "+00:00")):
        when += "+00:00"
    return _ts(when, with_time)


def _block_when(ev):
    """The when-column for one block on the work calendar, both ends of it.

    `_event_ts(start) + _event_ts(end)[11:]` was right for the ordinary case, a
    booking inside one day, and wrong for everything else. The slice throws away
    the end's date, so a block over four days printed as a block of no length at
    all - "2027-05-20 02:00 - 02:00" for Thursday through Sunday. And an all-day
    block comes back from Graph normalised to UTC midnight however it was
    written, so resolving it to local wall clock walked it to 02:00 and hung its
    last day over into a fifth. An all-day block is its dates: Graph ends it at
    the midnight after the last one, the same exclusive shape Google uses.
    """
    start, end = ev.get("start") or {}, ev.get("end") or {}
    if ev.get("isAllDay"):
        first, last = _dt(start.get("dateTime")), _dt(end.get("dateTime"))
        if not first or not last:
            return "-"
        last -= timedelta(days=1)
        if first.date() == last.date():
            return f"{first:%Y-%m-%d} heldag"
        return f"{first:%Y-%m-%d} – {last:%Y-%m-%d} heldag"
    lo, hi = _event_ts(start), _event_ts(end)
    if lo == "-" or hi == "-":
        return f"{lo} – {hi}"
    # Same day is the common case and reads better without the date repeated.
    return f"{lo} – {hi[11:]}" if lo[:10] == hi[:10] else f"{lo} – {hi}"


def _minutes(text):
    """'30m', '1h', '1h30m', '90min', or a bare number of minutes."""
    s = (text or "").strip().lower().replace(" ", "").replace("min", "m")
    if not s:
        raise ValueError("ingen tid angiven")
    if s.isdigit():
        return int(s)
    total, digits = 0, ""
    for ch in s:
        if ch.isdigit():
            digits += ch
        elif ch in "hm":
            if not digits:
                raise ValueError(f"{text!r}: saknar siffra före {ch!r}")
            total += int(digits) * (60 if ch == "h" else 1)
            digits = ""
        else:
            raise ValueError(f"{text!r}: förstår inte {ch!r}")
    if digits:
        raise ValueError(f"{text!r}: {digits} utan enhet")
    return total


def cmd_calendar_mirror(a):
    """Block the time on the work calendar from a Google event read on stdin.

    The event comes in as JSON rather than as arguments because the booking
    already exists on the other calendar, and retyping it into arguments is
    exactly where two calendars start to drift.
    """
    if sys.stdin.isatty():
        print("Eventet läses som JSON från stdin, t.ex.:\n"
              "  pkj calendar show <id> --json | wsg calendar mirror",
              file=sys.stderr)
        return 2
    try:
        ev = json.loads(_stdin_text() or "{}")
    except ValueError as e:
        print(f"Kunde inte tolka JSON från stdin: {e}", file=sys.stderr)
        return 2
    if not isinstance(ev, dict) or not ev.get("id"):
        print("Ingen Google-event i indatan (saknar 'id').", file=sys.stderr)
        return 2

    try:
        travel = 0 if a.no_restid else _minutes(a.restid)
    except ValueError as e:
        print(f"--restid {e}", file=sys.stderr)
        return 2

    try:
        bodies = mscal.bodies_for(ev, travel=travel)
        if a.dry_run:
            print(f"[torrkörning] {len(bodies)} × POST /me/events — inget bokat")
            print(json.dumps(bodies, indent=2, ensure_ascii=False))
            return 0
        made = mscal.mirror(ev, travel=travel)
    except mscal.MirrorError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"{ev.get('summary') or 'namnlös bokning'} speglad:")
    for block in made:
        print(f"  {block.get('subject', '?'):12} {_block_when(block)}"
              f"  id={block.get('id', '')[:24]}")
    # Travel was asked for but the event could not take it - say which, because
    # "speglad" on its own would read as if the half hours were there too.
    if travel and len(made) == 1:
        print("  (heldag, så ingen restid)")
    return 0


def cmd_calendar_show(a):
    """Whether a Google event has a block on the work calendar, and which."""
    try:
        rows = mscal.find(a.google_id)
    except mscal.MirrorError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Ingen spegling av det eventet.")
        return 1
    # Graph hands them back in its own order; a booking flanked by two Restid
    # blocks only reads as one thing when they are in the order they happen.
    rows.sort(key=lambda e: (e.get("start") or {}).get("dateTime") or "")
    for ev in rows:
        print(f"  {ev.get('subject'):12} {_block_when(ev)}  "
              f"{ev.get('showAs')}/{ev.get('sensitivity')}")
        print(f"  {'':4}id={ev.get('id')}")
    # A booking plus its two Restid blocks is three rows and entirely normal, so
    # only a repeated subject means an earlier run got past transactionId.
    seen = {}
    for ev in rows:
        seen[ev.get("subject")] = seen.get(ev.get("subject"), 0) + 1
    doubled = [s for s, n in seen.items()
               if n > (2 if s == mscal.TRAVEL_TITLE else 1)]
    if doubled:
        print(f"\n{', '.join(doubled)} finns i fler exemplar än det ska — "
              f"övertaligt, städa med unmirror och spegla om.")
    return 0


def cmd_calendar_unmirror(a):
    """Remove the block; the Google event is left alone."""
    try:
        rows = mscal.find(a.google_id)
    except (mscal.MirrorError, auth.AuthError) as e:
        print(str(e), file=sys.stderr)
        return 2
    if not rows:
        print("Ingen spegling att ta bort.")
        return 0
    if not a.yes:
        for ev in rows:
            # The list read before deleting, so it shows what is going in full
            # rather than a start time that hides three of the four days.
            print(f"  {ev.get('subject'):12} {_block_when(ev)}")
        print(f"\n{len(rows)} bokning(ar) skulle tas bort. Kör om med --yes.")
        return 1
    try:
        gone = mscal.unmirror(a.google_id)
    except mscal.MirrorError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"{gone} spegling(ar) borttagna. Google-eventet är orört.")
    return 0


def _add_calendar_commands(sub):
    s = sub.add_parser("mirror", help="block the time on the work calendar; reads a Google event on stdin")
    s.add_argument("--restid", metavar="LEN", default="30m",
                   help="travel time blocked either side: 30m, 1h, 45 (default: 30m)")
    s.add_argument("--no-restid", action="store_true", help="the booking only, no travel time")
    s.add_argument("--dry-run", action="store_true", help="print the Graph bodies and book nothing")
    s.set_defaults(fn=cmd_calendar_mirror)

    s = sub.add_parser("show", help="whether a Google event is mirrored, and where")
    s.add_argument("google_id", metavar="GOOGLE-ID")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_calendar_show)

    s = sub.add_parser("unmirror", help="remove the block; the Google event is left alone")
    s.add_argument("google_id", metavar="GOOGLE-ID")
    s.add_argument("--yes", action="store_true", help="actually delete; without it, only lists")
    s.set_defaults(fn=cmd_calendar_unmirror)


# ── Planner ───────────────────────────────────────────────────────────────────
# Read-only: which boards exist, what is on them, and what is assigned to me.
# Everything prints in board order, because a Planner board is a thing people
# have arranged on purpose and an alphabetical list of it is unrecognisable.

# ○ not started, ◐ in progress, ● done: the three states percentComplete has.
_MARKS = {"ej påbörjad": "○", "pågår": "◐", "klar": "●"}


def _due(s):
    """The date Planner itself shows, as a plain date.

    A due date is set without a time and stored as midnight UTC, so running it
    through the local zone converts something that was never a moment. West of
    Greenwich it lands on the day before, and the task then reads as overdue a
    day early — which is why this takes the date part rather than calling _ts.
    """
    return (s or "")[:10]


def _due_note(s):
    """' förfaller 2026-09-30 (om 5 dagar)', or the same in red-hot past tense."""
    day = _due(s)
    if not day:
        return ""
    try:
        left = (datetime.strptime(day, "%Y-%m-%d").date() - datetime.now().date()).days
    except ValueError:
        return f"  förfaller {day}"
    if left < 0:
        return f"  FÖRSENAD sedan {day} ({-left} dagar)"
    when = "idag" if left == 0 else ("imorgon" if left == 1 else f"om {left} dagar")
    return f"  förfaller {day} ({when})"


def _task_line(t, indent="  "):
    mark = _MARKS.get(planner.progress(t), "·")
    who = ", ".join(t.get("_assignees") or []) or "ingen tilldelad"
    return f"{indent}{mark} {t.get('title')}\n{indent}    {who}{_due_note(t.get('dueDateTime'))}"


def cmd_planner_plans(a):
    try:
        rows = planner.plans()
    except planner.PlannerError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Inga planer. Planner visar bara boards i grupper du är medlem i —\n"
              "en board du fått en länk till syns inte här.")
        return 0
    by_group = {}
    for p in rows:
        by_group.setdefault(p.get("_owner_name") or "-", []).append(p)
    for group in sorted(by_group, key=str.casefold):
        print(group)
        for p in sorted(by_group[group], key=lambda x: (x.get("title") or "").casefold()):
            trailer = planner.url(p["id"], legacy=a.legacy) if a.link else p.get("id")
            print(f"  {p.get('title')}  {trailer}")
    return 0


def cmd_planner_link(a):
    """The address of one board, to paste into a mail or a chat."""
    try:
        plan = planner.resolve_plan(a.plan)
        link = planner.url(plan["id"], legacy=a.legacy)
    except planner.PlannerError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.bare:
        print(link)
        return 0
    print(f"{plan.get('title')}  ({plan.get('_owner_name')})")
    print(link)
    return 0


def cmd_planner_tasks(a):
    try:
        plan = planner.resolve_plan(a.plan)
        rows = planner.tasks(plan["id"], include_done=a.all)
        cols = planner.buckets(plan["id"])
    except planner.PlannerError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    print(f"{plan.get('title')}  ({plan.get('_owner_name')})\n")
    if not rows:
        print("Inget öppet på den planen." if not a.all else "Planen är tom.")
        return 0
    names = {b["id"]: b.get("name") or "-" for b in cols}
    order = [b["id"] for b in cols] + [None]        # None: tasks with no bucket
    for bucket_id in order:
        here = [t for t in rows if (t.get("bucketId") or None) == bucket_id]
        if not here:
            continue
        print(names.get(bucket_id, "(ingen hink)"))
        for t in here:
            print(_task_line(t))
        print()
    return 0


def cmd_planner_mine(a):
    try:
        rows = planner.my_tasks(include_done=a.all)
        titles = {p["id"]: p.get("title") for p in planner.plans()}
    except planner.PlannerError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Inget tilldelat dig." if not a.all else "Inget tilldelat dig, klart eller ej.")
        return 0
    for t in rows:
        mark = _MARKS.get(planner.progress(t), "·")
        plan = titles.get(t.get("planId"), t.get("planId") or "-")
        print(f"{mark} {t.get('title')}\n    {plan}{_due_note(t.get('dueDateTime'))}")
    return 0


def cmd_planner_task(a):
    try:
        t = planner.task(a.task_id)
    except planner.PlannerError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(t, indent=2, ensure_ascii=False))
        return 0
    print(f"{t.get('title')}")
    print(f"  status     {planner.progress(t)} ({t.get('percentComplete') or 0} %)")
    print(f"  tilldelad  {', '.join(t.get('_assignees') or []) or 'ingen'}")
    if t.get("dueDateTime"):
        print(f"  {_due_note(t.get('dueDateTime')).strip()}")
    print(f"  skapad     {_ts(t.get('createdDateTime'), with_time=False)}")
    desc = (t.get("_details") or {}).get("description")
    if desc:
        print("\n" + desc.strip())
    items = planner.checklist(t)
    if items:
        print()
        for done, title in items:
            print(f"  [{'x' if done else ' '}] {title}")
    if a.link:
        # The card's own address needs the board it sits on, which the task
        # carries; the legacy form opens a task without one.
        print("\n" + planner.url(t.get("planId"), task_id=t.get("id"), legacy=a.legacy))
    return 0


def _add_planner_commands(sub):
    s = sub.add_parser("plans", help="the Planner boards you can see, by group")
    s.add_argument("--link", action="store_true", help="print each board's address instead of its id")
    s.add_argument("--legacy", action="store_true", help="link through tasks.office.com instead")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_planner_plans)

    s = sub.add_parser("link", help="the address of one board, to paste somewhere")
    s.add_argument("plan", metavar="PLAN", help="plan id or a fragment of its title")
    s.add_argument("--bare", action="store_true", help="the URL alone, nothing else — for piping")
    s.add_argument("--legacy", action="store_true", help="link through tasks.office.com instead")
    s.set_defaults(fn=cmd_planner_link)

    s = sub.add_parser("tasks", help="one board, in board order, grouped by bucket")
    s.add_argument("plan", metavar="PLAN", help="plan id or a fragment of its title")
    s.add_argument("--all", action="store_true", help="include completed tasks")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_planner_tasks)

    s = sub.add_parser("mine", help="tasks assigned to you, across every board, nearest due first")
    s.add_argument("--all", action="store_true", help="include completed tasks")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_planner_mine)

    s = sub.add_parser("task", help="one task with its description and checklist")
    s.add_argument("task_id", metavar="TASK-ID")
    s.add_argument("--link", action="store_true", help="also print the address of the card itself")
    s.add_argument("--legacy", action="store_true", help="link through tasks.office.com instead")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_planner_task)


# ── Teams ─────────────────────────────────────────────────────────────────────
# Finding, reading and posting in chats as yourself. Posted text is markdown and goes out as the HTML
# Teams shows; see teams.py for why Graph needs it that way. Before 0.6.0 every
# script that wrote to Teams carried its own copy of the token call, the POST and
# a markdown converter on a third-party package.

def _message_text(a):
    """The message from the arguments, or from stdin when there are none."""
    if a.text:
        return " ".join(a.text)
    if sys.stdin.isatty():
        return ""
    return _stdin_text()


def cmd_teams_send(a):
    from . import teams

    text = _message_text(a).strip()
    if not text:
        print("Tom text, inget skickat. Ge texten som argument eller på stdin.", file=sys.stderr)
        return 2
    content = text if a.html else teams.to_html(text)
    target = a.chat or f"1:1-chatten med {a.to}"

    if a.dry_run:
        print(f"[torrkörning] till {target} - inget skickat")
        print(text)
        print("--- som HTML:")
        print(content)
        return 0

    try:
        chat = a.chat or teams.chat_with(a.to)
        r = teams.send(chat, content)
    except Exception as e:  # urllib raises HTTPError; its body carries Graph's reason
        detail = ""
        read = getattr(e, "read", None)
        if callable(read):
            try:
                detail = ": " + read().decode(errors="replace")[:400]
            except Exception:
                detail = ""
        print(f"Sändningen misslyckades{detail or ': ' + str(e)}", file=sys.stderr)
        return 2
    if a.sent_log:
        # A loop that answers in a chat tells its own messages from the ones the
        # person typed by these ids.
        with open(a.sent_log, "a", encoding="utf-8") as f:
            print(r["id"], file=f)
    if a.json:
        print(json.dumps({"chatId": chat, "id": r["id"], "createdDateTime": r["createdDateTime"]}))
    else:
        print(f"Skickat till {target}: {r['id']} {_ts(r['createdDateTime'])}")
    return 0


def cmd_teams_md(a):
    from . import teams

    sys.stdout.write(teams.to_html(_message_text(a)))
    print()
    return 0


def _chat_line(c, me):
    from . import teams

    last = (c.get("lastMessagePreview") or {}).get("createdDateTime")
    kind = {"oneOnOne": "1:1", "group": "grupp", "meeting": "möte"}.get(c.get("chatType"), c.get("chatType") or "?")
    return f"{_ts(last)}  {kind:5}  {teams.chat_name(c, me)}\n                   {c['id']}"


def cmd_teams_chats(a):
    from . import teams

    me = teams.me_id()
    found = teams.find_chats(a.filter, a.limit) if a.filter else teams.chats(a.limit)
    if a.json:
        print(json.dumps([{"id": c["id"], "type": c.get("chatType"), "name": teams.chat_name(c, me),
                           "members": [{"name": m.get("displayName"), "email": m.get("email")}
                                       for m in c.get("members", [])],
                           "last": (c.get("lastMessagePreview") or {}).get("createdDateTime")}
                          for c in found], ensure_ascii=False, indent=2))
        return 0
    if not found:
        print(f"Ingen chatt bland de {a.limit} senaste matchar \"{a.filter}\"." if a.filter
              else "Inga chattar.")
        return 1
    for c in found:
        print(_chat_line(c, me))
    return 0


def _resolve_chat(a):
    """The chat id from --chat, --to or --name; prints why not and returns None."""
    from . import teams

    if a.chat:
        return a.chat
    if a.to:
        chat = teams.one_on_one(a.to)
        if not chat:
            print(f"Ingen 1:1-chatt med {a.to} bland de senaste 500 chattarna.", file=sys.stderr)
        return chat
    found = teams.find_chats(a.name)
    if len(found) == 1:
        return found[0]["id"]
    if not found:
        print(f"Ingen chatt matchar \"{a.name}\". Prova wsg teams chats.", file=sys.stderr)
        return None
    # A person's name means the 1:1 chat with them, even when the same person is
    # in ten group chats too - but only a whole name: "Erik" is not "Erikson".
    q = a.name.casefold().split()
    exact = [c for c in found if c.get("chatType") == "oneOnOne" and any(
        all(w in (m.get("displayName") or "").casefold().split() for w in q)
        for m in c.get("members", []))]
    if len(exact) == 1:
        return exact[0]["id"]
    me = teams.me_id()
    print(f"{len(found)} chattar matchar \"{a.name}\", välj med --chat:", file=sys.stderr)
    for c in found[:15]:
        print(_chat_line(c, me), file=sys.stderr)
    return None


def _message_json(m, images, saved=None):
    from . import teams

    name, uid = teams.sender(m)
    saved = saved or []
    return {"id": m["id"], "createdDateTime": m["createdDateTime"], "from": name, "fromId": uid,
            "text": teams.text_of(m), "images": images,
            "files": [{"name": n, "url": u, **({"path": saved[i]} if i < len(saved) else {})}
                      for i, (n, u) in enumerate(teams.files_of(m))]}


def cmd_teams_read(a):
    from . import teams

    chat = _resolve_chat(a)
    if not chat:
        return 1
    since = None
    if a.since:
        since = datetime.fromisoformat(a.since).astimezone().astimezone(
            timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    msgs = teams.messages(chat, a.n, since)
    out = []
    for m in msgs:
        images = teams.save_images(m, a.images) if a.images else []
        saved = teams.save_files(m, a.files) if a.files else []
        out.append(_message_json(m, images, saved))
    if a.json:
        print(json.dumps({"chatId": chat, "messages": out}, ensure_ascii=False, indent=2))
        return 0
    for m in out:
        print(f"--- {_ts(m['createdDateTime'])} {m['from']}")
        if m["text"]:
            print(m["text"])
        for p in m["images"]:
            print(f"  bild: {p}")
        for f in m["files"]:
            print(f"  fil: {f['name']} {f['url'] or ''}".rstrip())
            if "path" in f:
                print(f"  sparad: {f['path']}")
    if not out:
        print("Inga meddelanden" + (f" sedan {a.since}." if a.since else "."))
    return 0


def cmd_teams_find(a):
    from urllib.error import HTTPError

    from . import teams

    if a.recent:
        return _find_recent(a)
    try:
        hits = teams.search(a.query, a.n)
    except (auth.AuthError, HTTPError) as e:
        # A login from before 0.8.0 may lack ChannelMessage.Read.All; read the
        # recent chats rather than fail.
        print(f"Sökningen gick inte ({e}); letar i de senaste chattarna i stället. "
              "wsg graph login en gång till ger sökning i hela historiken.", file=sys.stderr)
        return _find_recent(a)
    if a.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print(f"Inget om \"{a.query}\" i Teams.")
        return 1
    # Chat names cost one list call; channel names would need another scope, so a
    # channel hit is shown as a channel with its link.
    me = teams.me_id()
    names = {c["id"]: teams.chat_name(c, me) for c in teams.chats(200)}
    for h in hits:
        where = names.get(h["chatId"], "chatt") if h["chatId"] else "kanal"
        print(f"--- {_ts(h['createdDateTime'])}  {where}  ·  {h['from']}")
        print(f"    {h['summary']}")
        print(f"    {h['chatId'] or h['webLink']}")
    return 0


def _find_recent(a):
    """Read the most recent chats and match here; the fallback without search."""
    from . import teams

    since =(datetime.now() - timedelta(days=a.days)).astimezone(
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    hits = teams.find(a.query, a.chats, since)
    me = teams.me_id()
    if a.json:
        print(json.dumps([dict(_message_json(m, []), chatId=c["id"], chat=teams.chat_name(c, me))
                          for c, m in hits[:a.n]], ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print(f"Inget om \"{a.query}\" i de {a.chats} senaste chattarna, senaste {a.days} dagarna.")
        return 1
    for c, m in hits[:a.n]:
        text = re.sub(r"\s+", " ", teams.text_of(m))
        i = text.casefold().find(a.query.casefold())
        snippet = ("…" if i > 60 else "") + text[max(0, i - 60):i + 140] + ("…" if len(text) > i + 140 else "")
        print(f"--- {_ts(m['createdDateTime'])}  {teams.chat_name(c, me)}  ·  {teams.sender(m)[0]}")
        print(f"    {snippet}")
        print(f"    {c['id']}")
    if len(hits) > a.n:
        print(f"(+{len(hits) - a.n} till, -n för fler)")
    return 0


def _add_teams_commands(sub):
    s = sub.add_parser("send", help="post a message in a chat as yourself; markdown in, Teams HTML out")
    who = s.add_mutually_exclusive_group(required=True)
    who.add_argument("--to", metavar="ADDRESS", help="the 1:1 chat with this person, found or made by address")
    who.add_argument("--chat", metavar="CHAT-ID", help="a chat by id, e.g. a group chat 19:...@thread.v2")
    s.add_argument("text", nargs="*", help="the message; read from stdin when left out")
    s.add_argument("--html", action="store_true", help="the text is HTML already, send it as it is")
    s.add_argument("--dry-run", action="store_true", help="print the text and its HTML, send nothing")
    s.add_argument("--sent-log", metavar="FILE", help="append the sent message's id to this file")
    s.add_argument("--json", action="store_true", help="print chatId, id and createdDateTime as JSON")
    s.set_defaults(fn=cmd_teams_send)

    s = sub.add_parser("chats", help="your chats, most recent first; a filter matches topic, member name or address")
    s.add_argument("filter", nargs="?", help="e.g. a person's name or a group chat's topic")
    s.add_argument("-n", "--limit", type=int, default=30, help="how many recent chats to look through, default 30")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_teams_chats)

    s = sub.add_parser("read", help="the messages in one chat, oldest first")
    which = s.add_mutually_exclusive_group(required=True)
    which.add_argument("--chat", metavar="CHAT-ID")
    which.add_argument("--to", metavar="ADDRESS", help="the 1:1 chat with this person")
    which.add_argument("--name", metavar="TEXT", help="the one chat whose topic or member matches")
    s.add_argument("-n", type=int, default=30, help="the last N messages, default 30")
    s.add_argument("--since", metavar="DATE", help="only messages from this local date/time on, e.g. 2026-10-12 or 2026-10-12T09:00")
    s.add_argument("--images", metavar="DIR", help="save pasted images here and print their paths")
    s.add_argument("--files", metavar="DIR", help="save shared files here and print their paths")
    s.add_argument("--json", action="store_true", help="raw createdDateTime, sender id, text, images and files")
    s.set_defaults(fn=cmd_teams_read)

    s = sub.add_parser("find", help="search your chats and channels for a text, the whole history")
    s.add_argument("query", help="words, or KQL such as 'from:anna invoice'")
    s.add_argument("--recent", action="store_true", help="read recent chats and match here instead of searching")
    s.add_argument("--chats", type=int, default=25, help="with --recent: how many recent chats to read, default 25")
    s.add_argument("--days", type=int, default=30, help="with --recent: how far back, default 30 days")
    s.add_argument("-n", type=int, default=20, help="show at most N hits, default 20")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_teams_find)

    s = sub.add_parser("md", help="print the Teams HTML for a markdown text, send nothing")
    s.add_argument("text", nargs="*", help="the text; read from stdin when left out")
    s.set_defaults(fn=cmd_teams_md)


# ── Mail ──────────────────────────────────────────────────────────────────────
# Answering in the thread and archiving the thread; see mail.py. sendmail stays
# under graph, where it has always been.

def _graph_error(prefix, e):
    detail = ""
    read = getattr(e, "read", None)
    if callable(read):
        try:
            detail = ": " + read().decode(errors="replace")[:400]
        except Exception:
            detail = ""
    print(f"{prefix}{detail or ': ' + str(e)}", file=sys.stderr)
    return 2


def _print_thread(msgs):
    from . import mail

    for m in msgs:
        print(f"  {_ts(m.get('receivedDateTime'))}  {mail._addr(m.get('from'))}  {m.get('subject') or ''}")


def cmd_mail_reply(a):
    from . import mail

    if sys.stdin.isatty():
        print("Svarstexten läses från stdin. Pipa in den, t.ex. med en here-doc.", file=sys.stderr)
        return 2
    text = _stdin_text().strip()
    if not text:
        print("Tom svarstext, inget skickat.", file=sys.stderr)
        return 2
    mid = mail.message_id(a.message)
    try:
        attachments = mail.file_attachments(a.attach)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    try:
        msg = mail.get(mid)
        to, cc = mail.reply_recipients(msg, a.all, mail.me_address())
    except Exception as e:
        return _graph_error("Kunde inte läsa mailet", e)

    if a.dry_run:
        print(f"[torrkörning] {'svara alla' if a.all else 'svara'} på \"{msg.get('subject') or ''}\" - inget skickat")
        print(f"Till: {', '.join(to) or '-'}")
        if cc:
            print(f"Kopia: {', '.join(cc)}")
        for att in attachments:
            print(f"Bilaga: {att['name']} ({len(att['contentBytes']) * 3 // 4 // 1024} kB)")
        print("---" if a.plain else "--- (markdown, skickas formaterad; se HTML:en med wsg mail md)")
        print(text)
        if a.archive:
            print("--- arkiveras sedan ur inkorgen:")
            _print_thread(mail.thread_in_inbox(mid))
        return 0

    try:
        mail.reply(mid, text, a.all, attachments, a.plain)
    except Exception as e:
        return _graph_error("Svaret gick inte iväg", e)
    print(f"Svarat till {', '.join(to)}" + (f" (kopia {', '.join(cc)})" if cc else "")
          + f" på \"{msg.get('subject') or ''}\"")
    if a.archive:
        try:
            n = mail.archive(mail.thread_in_inbox(mid))
        except Exception as e:
            return _graph_error("Svaret gick iväg, men arkiveringen misslyckades", e)
        print(f"Tråden arkiverad: {n} meddelanden flyttade från inkorgen.")
    return 0


def cmd_mail_md(a):
    from .markdown import to_mail_html

    out = to_mail_html(_message_text(a))
    if a.out:
        Path(a.out).write_text(f'<!doctype html><meta charset="utf-8">{out}\n', encoding="utf-8")
        print(f"Skrev {a.out}")
        return 0
    sys.stdout.write(out)
    print()
    return 0


def cmd_mail_archive(a):
    from . import mail

    mid = mail.message_id(a.message)
    try:
        msgs = mail.thread_in_inbox(mid)
    except Exception as e:
        return _graph_error("Kunde inte läsa tråden", e)
    if not msgs:
        print("Inget från den tråden ligger i inkorgen.")
        return 0
    if a.dry_run:
        print(f"[torrkörning] {len(msgs)} meddelanden skulle flyttas till Arkiv:")
        _print_thread(msgs)
        return 0
    try:
        n = mail.archive(msgs)
    except Exception as e:
        return _graph_error("Arkiveringen misslyckades", e)
    print(f"Tråden arkiverad: {n} meddelanden flyttade från inkorgen.")
    _print_thread(msgs)
    return 0


def _add_mail_commands(sub):
    s = sub.add_parser("reply", help="answer a mail in its thread, text from stdin; --dry-run first")
    s.add_argument("message", help="the message id, or the connector's mail:///messages/<id>")
    s.add_argument("--all", action="store_true", help="reply to everyone on it, not only the sender")
    s.add_argument("--attach", action="append", metavar="FILE", help="attach a file (repeat; ~3 MB in total)")
    s.add_argument("--archive", action="store_true", help="then move the whole thread out of the inbox")
    s.add_argument("--plain", action="store_true", help="send the text as it is, not rendered from markdown")
    s.add_argument("--dry-run", action="store_true", help="show who it goes to and the text, send nothing")
    s.set_defaults(fn=cmd_mail_reply)

    s = sub.add_parser("md", help="print the mail HTML for a markdown text, send nothing")
    s.add_argument("text", nargs="*", help="the text; read from stdin when left out")
    s.add_argument("-o", "--out", metavar="FILE", help="write it to FILE as a page to open in a browser")
    s.set_defaults(fn=cmd_mail_md)

    s = sub.add_parser("archive", help="move every message in a mail's thread from the inbox to Archive")
    s.add_argument("message", help="any message in the thread: its id, or mail:///messages/<id>")
    s.add_argument("--dry-run", action="store_true", help="list what would move, move nothing")
    s.set_defaults(fn=cmd_mail_archive)


# ── One-time links (secrets.wesports.io) ──────────────────────────────────────
# How a credential leaves this machine for someone else: as a link that burns on
# first read, never as text in a mail or a Teams thread, which are archived,
# forwarded and indexed. Encryption happens here; see onetime.py.


def cmd_secret_create(a):
    import getpass
    if a.file:
        # A file goes as it is, trailing newline and all: a JSON key file or a PEM
        # is compared byte for byte by whatever reads it at the other end.
        try:
            with open(a.file, encoding="utf-8") as f:
                value = f.read()
        except OSError as e:
            print(f"Kan inte läsa {a.file}: {e.strerror}", file=sys.stderr)
            return 2
    elif sys.stdin.isatty():
        value = getpass.getpass("Hemlighet (visas inte): ")
    else:
        value = _stdin_text()
        # The pipe's line ending, not the secret's — same rule as arena secret-set.
        for eol in (chr(13) + chr(10), chr(10), chr(13)):
            if value.endswith(eol):
                value = value[: -len(eol)]
                break
    if not value:
        print("Tomt värde, avbryter.", file=sys.stderr)
        return 2
    passphrase = None
    if a.passphrase:
        passphrase = getpass.getpass("Lösenfras (skickas separat till mottagaren): ")
        if passphrase != getpass.getpass("Igen: "):
            print("Lösenfraserna skiljer sig, avbryter.", file=sys.stderr)
            return 2
    try:
        r = onetime.create(value, ttl=a.ttl, views=a.views, title=a.title, passphrase=passphrase)
    except onetime.OnetimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    # The link alone on stdout so it pipes; the rest is for the person at the prompt.
    print(r["link"])
    plural = "visning" if r["views"] == 1 else "visningar"
    print(f"Går ut {r['expiresAt']}, {r['views']} {plural}"
          f"{', kräver lösenfras' if passphrase else ''}.", file=sys.stderr)
    print(f"Ångra innan den öppnats: wsg secret delete {r['id']} {r['burnToken']}", file=sys.stderr)
    return 0


def cmd_secret_delete(a):
    try:
        onetime.delete(a.id, a.burn_token)
    except onetime.OnetimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("Borttagen. Länken fungerar inte längre.")
    return 0


def _add_secret_commands(sub):
    s = sub.add_parser("create", help="encrypt a value here and print a one-time link; the "
                                      "value comes from stdin or --file, never an argument")
    s.add_argument("--file", metavar="PATH", help="send this file's contents, unchanged")
    s.add_argument("--ttl", choices=onetime.TTLS, default="1d",
                   help="how long the link lives if nobody opens it (default 1d)")
    s.add_argument("--views", type=int, default=1, metavar="N",
                   help=f"how many times it can be opened, 1-{onetime.MAX_VIEWS} (default 1)")
    s.add_argument("--title", help="shown before opening, readable by anyone with the link "
                                   "— never the secret itself")
    s.add_argument("--passphrase", action="store_true",
                   help="prompt for a passphrase the recipient must also type; send it "
                        "another way than the link")
    s.add_argument("--json", action="store_true",
                   help="print link, id, burn token and expiry as JSON")
    s.set_defaults(fn=cmd_secret_create)

    s = sub.add_parser("delete", help="destroy a link before it is opened, with its burn token")
    s.add_argument("id", metavar="ID")
    s.add_argument("burn_token", metavar="BURN-TOKEN")
    s.set_defaults(fn=cmd_secret_delete)


# ── Short links (go.wesports.io) ──────────────────────────────────────────────
# A long URL as a short one. The neighbouring `secret` commands hand over a value
# that burns on first read; these hand over a destination, and it can burn the
# same way or last for ever. This one needs a service token, because an open
# redirector on a wesports.io hostname is a phishing tool wearing our domain —
# see shortlink.py.


def _link_mode(a):
    """The one lifetime flag that was given, as (mode, uses)."""
    if a.once:
        return "once", None
    if a.times is not None:
        return "times", a.times
    if a.until:
        return "until", None
    return "forever", None


def cmd_link_create(a):
    mode, uses = _link_mode(a)
    try:
        r = shortlink.create(a.url, mode=mode, uses=uses, until=a.until, name=a.name,
                             title=a.title, confirm=a.confirm)
    except shortlink.ShortLinkError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    # The link alone on stdout so it pipes; the rest is for the person at the prompt.
    print(r["url"])
    print(f"{r['lifetime']} → {r['target']}", file=sys.stderr)
    if r.get("maxUses") == 1:
        print("Engångslänk: den som öppnar den först kommer fram, nästa får veta att den är använd.",
              file=sys.stderr)
    if r.get("confirm"):
        print("Frågar först: mottagaren får en sida med en knapp, och bara knappen "
              "förbrukar användningen.", file=sys.stderr)
    print(f"Ta tillbaka den: wsg link revoke {r['slug']}", file=sys.stderr)
    return 0


def cmd_link_list(a):
    try:
        rows = shortlink.links()
    except shortlink.ShortLinkError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Inga länkar än.")
        return 0
    if not a.all:
        rows = [r for r in rows if r.get("state") == "live"]
        if not rows:
            print("Inga aktiva länkar. Visa de använda och återkallade med --all.")
            return 0
    width = max(len(r["url"]) for r in rows)
    for r in rows:
        # State as a word, never as a colour: this is read in whatever terminal
        # the person happens to have.
        print(f"{r['url']:<{width}}  {r['state']:<8}  {r['lifetime']}")
        print(f"{'':<{width}}  → {r['target']}")
    return 0


def cmd_link_revoke(a):
    try:
        shortlink.revoke(a.slug)
    except shortlink.ShortLinkError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"Återkallad. {shortlink.BASE}/{a.slug} skickar ingen vidare längre.")
    return 0


def _add_link_commands(sub):
    s = sub.add_parser("create", help="shorten a URL; it lasts for ever unless you say otherwise")
    s.add_argument("url", metavar="URL", help="the destination, http:// or https://")
    life = s.add_mutually_exclusive_group()
    life.add_argument("--once", action="store_true",
                      help="gone after one use — the first person through gets it, nobody else")
    life.add_argument("--times", type=int, metavar="N", help="works N times, then it is spent")
    life.add_argument("--until", metavar="TIME",
                      help="works until this moment, UTC: 2026-10-23T17:00, or a bare date "
                           "for the end of that day")
    s.add_argument("--name", metavar="SLUG",
                   help="choose the short name instead of getting a random one. Only for a link "
                        "with no use limit: a name that can be guessed is a one-use link somebody "
                        "else can spend")
    s.add_argument("--confirm", action="store_true",
                   help="open a page with a button instead of redirecting, and spend the use "
                        "only when it is pressed. Without this the service reads Sec-Fetch-* "
                        "and lets an obvious click through in one hop. Needs --once or --times")
    s.add_argument("--title", help="a note to yourself, shown only in your own list")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_link_create)

    s = sub.add_parser("list", help="your short links, newest first")
    s.add_argument("--all", action="store_true", help="include spent, expired and revoked links")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_link_list)

    s = sub.add_parser("revoke", help="withdraw a link; it stops working for everyone")
    s.add_argument("slug", metavar="SLUG", help="the short name, e.g. arena or j93ccdcp04")
    s.set_defaults(fn=cmd_link_revoke)


# ── skill and update ──────────────────────────────────────────────────────────
# The same pair e37-cli has (2026-10-07): a colleague's Claude installs wsg from
# INSTALL-FOR-CLAUDE.md, writes the skill, and later updates both with one command.

REPO = "Wesports-Scandinavia-AB/wsg-cli"


def _skill_text():
    from importlib.resources import files
    return files("wsg").joinpath("skill/SKILL.md").read_text(encoding="utf-8")


def cmd_skill_show(a):
    print(_skill_text())
    return 0


def cmd_skill_install(a):
    """Put SKILL.md where Claude Code finds it, so later conversations know wsg.

    Overwritten on every install: the file comes from this package, and an
    upgrade should bring the instructions along with the code they describe.
    """
    target = Path(a.dir).expanduser() if a.dir else Path.home() / ".claude" / "skills" / "wsg"
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(_skill_text(), encoding="utf-8")
    print(f"Skill installerad: {target / 'SKILL.md'}")
    return 0


def cmd_update(a):
    """Reinstall from GitHub, then refresh the skill with the NEW code.

    --force-reinstall because pip treats a URL whose version did not change as
    already satisfied, and main moves between version bumps. --user only outside
    a virtualenv, where it is refused. The skill step runs in a fresh process so
    it writes the new SKILL.md, not the one this old process has loaded.

    An editable install (pip install -e, a clone you work in) is left alone: the
    clone is the version, and `git pull` is how it moves. Reinstalling over it
    would swap the clone for a copy and stop your edits from taking effect.
    """
    import shutil
    import subprocess
    from importlib.metadata import distribution
    from . import __version__

    def editable(name):
        try:
            raw = distribution(name).read_text("direct_url.json") or "{}"
        except Exception:
            return False
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))

    if editable("wsg-cli"):
        print("wsg-cli är installerat från en lokal klon (pip install -e). Uppdatera med git pull där.",
              file=sys.stderr)
        return 1
    url = (f"git+https://github.com/{REPO}" if shutil.which("git")
           else f"https://github.com/{REPO}/archive/refs/heads/main.zip")
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps",
           "--quiet", "--disable-pip-version-check", f"wsg-cli @ {url}"]
    if sys.prefix == sys.base_prefix:
        cmd.insert(4, "--user")
    print(f"Hämtar senaste wsg-cli (har {__version__}) ...", file=sys.stderr)
    if subprocess.run(cmd).returncode:
        print("Uppdateringen misslyckades, se pip-utskriften ovan.", file=sys.stderr)
        return 1
    new = subprocess.run([sys.executable, "-c", "import wsg; print(wsg.__version__)"],
                         capture_output=True, text=True).stdout.strip()
    subprocess.run([sys.executable, "-m", "wsg", "skill", "install"])
    print(f"wsg-cli {__version__} -> {new or '?'}")
    return 0


def _add_skill_commands(sub):
    s = sub.add_parser("install", help="write SKILL.md to ~/.claude/skills/wsg/ for Claude Code")
    s.add_argument("--dir", help="another directory than ~/.claude/skills/wsg")
    s.set_defaults(fn=cmd_skill_install)
    s = sub.add_parser("show", help="print the skill")
    s.set_defaults(fn=cmd_skill_show)


def main(argv=None):
    # stdin is in here too: what gets piped in is UTF-8 from every shell we use,
    # while Python would otherwise decode it in the locale code page and turn a
    # piped å into two characters. See _stdin_text for the other half of this.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(
        prog="wsg",
        description="Sign in once per service, reuse the token from any script. "
                    "Services: graph (Microsoft Graph via Entra), arena (WeSports Arena), "
                    "secret (one-time links).",
        epilog="If you are an assistant: run `wsg skill show` for how to use this tool. Never ask\n"
               "for a password or token in the chat; every sign-in prints a URL the person opens\n"
               "themselves.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from . import __version__
    p.add_argument("--version", action="version", version=f"wsg-cli {__version__}")
    plugins = _plugins("wsg.commands")
    # metavar hides the compatibility aliases from the usage line. Plugin commands are
    # not in it; they show in --help under their own names.
    sub = p.add_subparsers(dest="cmd", required=True,
                           metavar="{graph,arena,calendar,planner,teams,mail,secret,skill,update" + (",..." if plugins else "") + "}")

    g = sub.add_parser("graph", help="Microsoft Graph: sign in as yourself with Entra, get a Graph token")
    _add_graph_commands(g.add_subparsers(dest="graph_cmd", required=True))

    ar = sub.add_parser("arena", help="WeSports Arena, signed in with Arena's own OAuth")
    _add_arena_commands(ar.add_subparsers(dest="arena_cmd", required=True))

    ca = sub.add_parser("calendar", help="mirror a private booking onto the work calendar as busy")
    _add_calendar_commands(ca.add_subparsers(dest="calendar_cmd", required=True))

    pl = sub.add_parser("planner", help="Planner boards, read-only: what is on them and what is mine")
    _add_planner_commands(pl.add_subparsers(dest="planner_cmd", required=True))

    te = sub.add_parser("teams", help="Teams chats: find, read and post as yourself, markdown in")
    _add_teams_commands(te.add_subparsers(dest="teams_cmd", required=True))

    ma = sub.add_parser("mail", help="answer a mail in its thread, archive a thread")
    _add_mail_commands(ma.add_subparsers(dest="mail_cmd", required=True))

    se = sub.add_parser("secret", help="one-time links on secrets.wesports.io, encrypted before they leave")
    _add_secret_commands(se.add_subparsers(dest="secret_cmd", required=True))

    li = sub.add_parser("link", help="short links on go.wesports.io, for ever or gone after one use")
    _add_link_commands(li.add_subparsers(dest="link_cmd", required=True))

    sk = sub.add_parser("skill", help="the Claude skill for wsg: install, show")
    _add_skill_commands(sk.add_subparsers(dest="skill_cmd", required=True))

    s = sub.add_parser("update", help="get the latest wsg-cli from GitHub and refresh the Claude skill")
    s.set_defaults(fn=cmd_update)

    for ep in plugins:
        ep.load()(sub)

    _add_graph_commands(sub, visible=False)   # wsg login / token / status / whoami / logout

    a = p.parse_args(argv)
    # Most commands catch a missing login themselves; the ones that do not used to
    # end in a traceback. The message already names the login to run.
    try:
        return a.fn(a)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
