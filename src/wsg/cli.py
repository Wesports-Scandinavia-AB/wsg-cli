import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import arena, auth, garp, garpdb, intune, mscal, ongoing, onprem, planner, users
from .scopes import DEFAULT_SCOPES


def _scopes_arg(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else list(DEFAULT_SCOPES)


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


def cmd_graph_token(a):
    try:
        tok = auth.get_token(_scopes_arg(a.scopes) if a.scopes else None)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"Bearer {tok}" if a.header else tok)
    return 0


def cmd_graph_status(a):
    c = auth.read_cache()
    if not c:
        print("Inte inloggad. Kör: wsg graph login")
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


def cmd_graph_whoami(a):
    try:
        me = auth.graph("GET", "/me")
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{me.get('displayName')} <{me.get('userPrincipalName')}>")
    return 0


def cmd_graph_logout(a):
    print("Cache borttagen." if auth.logout() else "Ingen cache fanns.")
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
    """
    import base64
    import mimetypes

    recipients = [x.strip() for x in (a.to or "").split(",") if x.strip()]
    cc = [x.strip() for x in (a.cc or "").split(",") if x.strip()]
    if not recipients:
        print("--to kraver minst en adress (kommaseparerat for flera)", file=sys.stderr)
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

    message = {
        "subject": a.subject or "",
        "body": {"contentType": "HTML" if a.html else "Text", "content": body_text},
        "toRecipients": [{"emailAddress": {"address": x}} for x in recipients],
    }
    if cc:
        message["ccRecipients"] = [{"emailAddress": {"address": x}} for x in cc]
    if attachments:
        message["attachments"] = attachments
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
    print(f"Skickat till {', '.join(recipients)}"
          + ("" if a.no_save else " (kopia i Skickat)"))
    return 0


def cmd_arena_login(a):
    try:
        tok = arena.login(timeout=a.timeout)
    except auth.AuthError as e:
        print(f"Inloggningen misslyckades: {e}", file=sys.stderr)
        return 1
    c = auth.decode_jwt(tok)
    print(f"Inloggad mot {arena.app_url()} som {c.get('email', '?')}", file=sys.stderr)
    return 0


def cmd_arena_token(a):
    try:
        tok = arena.get_token()
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"cf-access-token: {tok}" if a.header else tok)
    return 0


def cmd_arena_status(a):
    c = arena.status()
    if not c:
        print(f"Inte inloggad mot {arena.app_url()}. Kör: wsg arena login")
        return 2
    exp = datetime.fromtimestamp(c.get("exp", 0))
    idp = c.get("idp")
    idp = idp.get("type") if isinstance(idp, dict) else idp
    if a.json:
        print(json.dumps({
            "url": arena.app_url(),
            "email": c.get("email"),
            "idp": idp,
            "amr": c.get("amr"),
            "expires_at": exp.isoformat(timespec="seconds"),
            "valid": c.get("exp", 0) > time.time(),
            "cache": c.get("_files"),
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"App:      {arena.app_url()}")
    print(f"E-post:   {c.get('email', '?')}")
    print(f"IdP:      {idp or '?'}  amr={','.join(c.get('amr') or []) or '-'}")
    print(f"Session:  {'giltig till ' + exp.strftime('%Y-%m-%d %H:%M') if c.get('exp', 0) > time.time() else 'utgången, kör: wsg arena login'}")
    for f in c.get("_files") or []:
        print(f"Cache:    {f}")
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


def cmd_intune_apps(a):
    try:
        rows = intune.apps(a.name)
    except intune.IntuneError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Inga appar matchade.")
        return 0
    for app in sorted(rows, key=lambda x: (x.get("displayName") or "").casefold()):
        kind = (app.get("@odata.type") or "").split(".")[-1]
        print(f"{app['id']}  {app.get('displayName')}  [{kind}]  {app.get('publishingState')}")
        for asg in app.get("assignments", []):
            t = asg.get("target") or {}
            who = t.get("groupId") or (t.get("@odata.type") or "").split(".")[-1]
            print(f"    {asg.get('intent')} -> {who}")
    return 0


def cmd_intune_app_status(a):
    try:
        rows = intune.app_status(a.app_id)
    except intune.IntuneError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Ingen klient har rapporterat än. Det betyder inte att tilldelningen saknas -\n"
              "kör 'wsg intune intent' för att se vad tjänsten avser för en användare.")
        return 0
    for r in rows:
        print(" | ".join(f"{k}={v}" for k, v in r.items() if v not in (None, "")))
    return 0


def cmd_intune_intent(a):
    try:
        rows = intune.intent(a.user, a.device)
    except intune.IntuneError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for x in rows:
        print(f"{x.get('displayName')}  |  {x.get('mobileAppIntent')}  |  {x.get('installState')}")
    return 0


def cmd_intune_store_app(a):
    try:
        app_id = intune.add_store_app(a.package_id, a.name, a.publisher,
                                      description=a.description, run_as=a.run_as)
        print(f"Appen skapad: {app_id}")
        if a.assign:
            intune.assign(app_id, a.assign, a.intent,
                          "#microsoft.graph.winGetAppAssignmentSettings")
            print(f"Tilldelad {a.assign} som {a.intent}")
    except intune.IntuneError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def cmd_intune_win32(a):
    def progress(done, total):
        print(f"\r  laddar upp {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", file=sys.stderr, flush=True)

    try:
        app_id = intune.publish_win32(
            a.package, a.name, a.publisher, a.install, a.uninstall,
            a.detect_path, a.detect_file, description=a.description,
            run_as=a.run_as, arch=a.arch, min_os=a.min_os, setup_file=a.setup_file,
            on_progress=None if a.quiet else progress,
        )
        if not a.quiet:
            print(file=sys.stderr)
        print(f"Appen publicerad: {app_id}")
        if a.assign:
            intune.assign(app_id, a.assign, a.intent,
                          "#microsoft.graph.win32LobAppAssignmentSettings")
            print(f"Tilldelad {a.assign} som {a.intent}")
    except intune.IntuneError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def cmd_intune_sync(a):
    try:
        intune.sync_device(a.device)
    except intune.IntuneError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("Synk begärd. Enheten hämtar tilldelningar vid nästa incheckning.")
    return 0


def _add_intune_commands(sub):
    s = sub.add_parser("apps", help="list managed apps with their assignments")
    s.add_argument("name", nargs="?", help="only apps whose display name contains this")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_intune_apps)

    s = sub.add_parser("app-status", help="per-user install status for one app")
    s.add_argument("app_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_intune_app_status)

    s = sub.add_parser("intent", help="what the service intends for a user on a device")
    s.add_argument("user", help="user object id or UPN")
    s.add_argument("device", help="Intune managed device id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_intune_intent)

    s = sub.add_parser("store-app", help="add a Microsoft Store app (new) by Store id")
    s.add_argument("package_id", help="Store id, e.g. 9PLM9XGG6VKS")
    s.add_argument("--name", required=True)
    s.add_argument("--publisher", required=True)
    s.add_argument("--description")
    s.add_argument("--run-as", choices=("user", "system"), default="user")
    s.add_argument("--assign", metavar="GROUP", help="group id, or 'all' for all licensed users")
    s.add_argument("--intent", choices=("available", "required"), default="available")
    s.set_defaults(fn=cmd_intune_store_app)

    s = sub.add_parser("win32", help="publish an .intunewin package")
    s.add_argument("package", help="path to the .intunewin file")
    s.add_argument("--name", required=True)
    s.add_argument("--publisher", required=True)
    s.add_argument("--install", required=True, metavar="CMD", help="install command line")
    s.add_argument("--uninstall", required=True, metavar="CMD", help="uninstall command line")
    s.add_argument("--detect-path", required=True, help=r"e.g. %%LOCALAPPDATA%%\AnthropicClaude")
    s.add_argument("--detect-file", required=True, help="file whose existence means installed")
    s.add_argument("--description")
    s.add_argument("--setup-file", help="default: the SetupFile from Detection.xml")
    s.add_argument("--run-as", choices=("user", "system"), default="user",
                   help="user (default) for per-user installers under %%LOCALAPPDATA%%")
    s.add_argument("--arch", default="x64")
    s.add_argument("--min-os", default="1809")
    s.add_argument("--assign", metavar="GROUP", help="group id, or 'all' for all licensed users")
    s.add_argument("--intent", choices=("available", "required"), default="required")
    s.add_argument("-q", "--quiet", action="store_true", help="no upload progress")
    s.set_defaults(fn=cmd_intune_win32)

    s = sub.add_parser("sync", help="ask a device to check in now")
    s.add_argument("device", help="Intune managed device id")
    s.set_defaults(fn=cmd_intune_sync)


def _ts(s, with_time=True):
    """Graph timestamps are ISO 8601 in UTC; show them as local wall clock."""
    if not s:
        return "-"
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return s
    return d.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def _age(s):
    if not s:
        return ""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return ""
    days = (datetime.now(d.tzinfo) - d).days
    return f" ({days} dagar sedan)" if days >= 1 else " (idag)"


def cmd_user_find(a):
    try:
        rows = users.find(a.query, limit=a.top)
    except users.UserError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Ingen användare matchade.")
        return 0
    for u in sorted(rows, key=lambda x: (x.get("displayName") or "").casefold()):
        state = "aktivt" if u.get("accountEnabled") else "BLOCKERAT"
        print(f"{state:<11}{u.get('displayName')}  <{u.get('userPrincipalName')}>  {u.get('id')}")
    return 0


def cmd_user_show(a):
    try:
        u = users.resolve(a.user)
        lic = users.licenses(u["id"])
        mgr = users.manager(u["id"])
        methods = users.auth_methods(u["id"]) if a.auth_methods else None
        recent, window = ([], None) if a.no_audit else users.audits(u["id"], days=a.days, limit=5)
    except users.UserError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps({"user": u, "licenses": lic, "manager": mgr,
                          "authMethods": methods, "recentAudits": recent},
                         indent=2, ensure_ascii=False))
        return 0

    sia = u.get("signInActivity") or {}
    synced = u.get("onPremisesSyncEnabled")
    rows = [
        ("konto", "AKTIVERAT" if u.get("accountEnabled") else "BLOCKERAT (inloggning spärrad)"),
        ("id", u.get("id")),
        ("upn", u.get("userPrincipalName")),
        ("mail", u.get("mail")),
        ("typ", f"{u.get('userType') or '?'}, "
                + ("synkat från lokalt AD" if synced else "molnkonto")),
        ("titel", " / ".join(x for x in (u.get("jobTitle"), u.get("department"),
                                         u.get("companyName")) if x)),
        ("chef", f"{mgr.get('displayName')} <{mgr.get('userPrincipalName')}>" if mgr else "-"),
        ("anställd", u.get("employeeId")),
        ("slutdatum", _ts(u.get("employeeLeaveDateTime"), False)
                      if u.get("employeeLeaveDateTime") else None),
        ("skapat", _ts(u.get("createdDateTime"))),
        ("lösenord ändrat", _ts(u.get("lastPasswordChangeDateTime"))
                            + _age(u.get("lastPasswordChangeDateTime"))),
        # lastSignIn* count attempts, failed ones included; only lastSuccessful*
        # says the account actually let someone in.
        ("senast lyckad inloggning", _ts(sia.get("lastSuccessfulSignInDateTime"))
                                     + _age(sia.get("lastSuccessfulSignInDateTime"))),
        ("senaste försök", _ts(sia.get("lastSignInDateTime"))
                           + _age(sia.get("lastSignInDateTime"))),
        ("  varav token/app", _ts(sia.get("lastNonInteractiveSignInDateTime"))
                              + _age(sia.get("lastNonInteractiveSignInDateTime"))),
        ("licenser", ", ".join(x.get("skuPartNumber") or "?" for x in lic) or "inga"),
        ("licenser borttagna", _ts(users.offboarded(u)) + _age(users.offboarded(u))
                               if users.offboarded(u) else None),
        ("sessioner spärrade", _ts(u.get("signInSessionsValidFromDateTime"))
                               + _age(u.get("signInSessionsValidFromDateTime"))),
        ("sam-konto", u.get("onPremisesSamAccountName")),
        ("senaste synk", _ts(u.get("onPremisesLastSyncDateTime")) if synced else None),
        ("ou", u.get("onPremisesDistinguishedName")),
    ]
    if u.get("proxyAddresses") and a.verbose:
        rows.append(("adresser", ", ".join(u["proxyAddresses"])))
    if methods is not None:
        kinds = sorted({(m.get("@odata.type") or "").split(".")[-1] for m in methods})
        rows.append(("mfa-metoder", ", ".join(kinds) or "inga"))
    elif a.auth_methods:
        rows.append(("mfa-metoder", "kunde inte läsas - kräver scope "
                                    "UserAuthenticationMethod.Read.All"))

    rows = [(k, v) for k, v in rows if v not in (None, "", "-")]
    w = max(len(k) for k, _ in rows) + 2
    print(f"{u.get('displayName')}")
    for label, value in rows:
        print(f"  {label:<{w}}{value}")

    if not u.get("accountEnabled") and synced:
        print("\nKontot är synkat från lokalt AD. Spärren sattes då nästan säkert i AD -\n"
              "Entra-loggen visar bara AD Connect som skrev över värdet.")
    if recent:
        print(f"\nSenaste ändringar (sedan {_ts(window, False)}):")
        _print_audits(recent, verbose=False)
        print(f"\n  Hela historiken: wsg user audit {u.get('userPrincipalName')}")
    elif not a.no_audit:
        print(f"\nInga katalogändringar loggade sedan {_ts(window, False)}.")
    return 0


def _print_audits(rows, verbose=True):
    for r in rows:
        head = (f"{_ts(r.get('activityDateTime'))}  {r.get('activityDisplayName')}"
                f"  av {users.audit_initiator(r)}")
        result = r.get("result")
        if result and result != "success":
            head += f"  [{result}]"
        print(f"  {head}")
        for target, prop, old, new in users.audit_changes(r):
            if not verbose and prop in ("Included Updated Properties", "TargetId.UserType"):
                continue
            arrow = f"{old or '(tomt)'} -> {new or '(tomt)'}" if old != new else (new or "")
            # A consent record carries every scope twice; unabridged it buries the
            # rest of the trail.
            if not verbose and len(arrow) > 160:
                arrow = arrow[:160] + f"… (+{len(arrow) - 160} tecken, -v för allt)"
            print(f"      {prop}: {arrow}")
        if verbose and r.get("additionalDetails"):
            extra = ", ".join(f"{d.get('key')}={d.get('value')}"
                              for d in r["additionalDetails"] if d.get("value"))
            if extra:
                print(f"      ({extra})")


def cmd_user_audit(a):
    try:
        u = users.resolve(a.user)
        rows, window = users.audits(u["id"], days=a.days, limit=a.top,
                                    as_initiator=a.by_user)
    except users.UserError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    what = "Ändringar gjorda AV" if a.by_user else "Ändringar PÅ"
    print(f"{what} {u.get('displayName')} <{u.get('userPrincipalName')}>, "
          f"sedan {_ts(window, False)}: {len(rows)} poster")
    if not rows:
        print("Inga poster. Katalogloggen sparas 30 dagar med Entra ID P1/P2, annars 7 -\n"
              "en äldre ändring finns inte kvar att hämta ur Entra.")
        return 0
    _print_audits(rows, verbose=a.verbose)
    return 0


def cmd_user_signins(a):
    try:
        u = users.resolve(a.user)
        rows, window = users.signins(u["id"], days=a.days, limit=a.top,
                                     failed_only=a.failed, kinds=a.kinds)
    except users.UserError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(f"Inga inloggningar loggade sedan {_ts(window, False)}.")
        return 0
    for r in rows:
        st = r.get("status") or {}
        loc = r.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("countryOrRegion")) if x)
        verdict = "ok" if st.get("errorCode") == 0 else f"FEL {st.get('errorCode')}"
        kind = "interaktiv" if "interactiveUser" in (r.get("signInEventTypes") or []) else "token"
        print(f"{_ts(r.get('createdDateTime'))}  {verdict:<9} {kind:<11}"
              f"{r.get('appDisplayName')}  {r.get('ipAddress') or '-'}  {where or '-'}")
        if st.get("errorCode") and (st.get("failureReason") or "").lower() != "other.":
            print(f"      {st.get('failureReason')}")
    return 0


def cmd_user_groups(a):
    try:
        u = users.resolve(a.user)
        rows = users.groups(u["id"])
    except users.UserError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for g in sorted(rows, key=lambda x: (x.get("displayName") or "").casefold()):
        kind = (g.get("@odata.type") or "").split(".")[-1]
        tag = "roll" if kind == "directoryRole" else (
            "M365" if "Unified" in (g.get("groupTypes") or []) else
            "distribution" if g.get("mailEnabled") and not g.get("securityEnabled") else
            "säkerhet")
        src = " (AD)" if g.get("onPremisesSyncEnabled") else ""
        print(f"{tag:<13}{g.get('displayName')}{src}  {g.get('id')}")
    return 0


def _read_password(a, upn):
    """The new password, from the generator, a terminal, or stdin - never argv.

    An argument lands in the shell history and in the process list of every other
    user on the machine, and this is a credential. Same reasoning as
    `wsg arena secret-set`, which is why there is no --value here either.
    """
    if a.generate:
        return users.generate_password(a.length)
    if sys.stdin.isatty():
        import getpass
        pw = getpass.getpass(f"Nytt lösenord för {upn} (visas inte): ")
        if pw and getpass.getpass("En gång till: ") != pw:
            return None
        return pw
    # One trailing line ending belongs to the pipe or heredoc, not to the
    # password. Anything beyond that is kept: a password may end in a space.
    pw = _stdin_text()
    for eol in ("\r\n", "\n", "\r"):
        if pw.endswith(eol):
            return pw[: -len(eol)]
    return pw


def _confirm(a, question):
    if a.yes or not sys.stdin.isatty():
        return True
    return input(question).strip().lower() in ("j", "ja", "y", "yes")


def cmd_user_password(a):
    """Set a new password, in Entra or - for a synced account - in AD.

    Which half of a hybrid tenant owns the password decides everything here, so
    the command says which one it is talking to before it writes anything.
    """
    try:
        u = users.resolve(a.user)
    except users.UserError as e:
        print(str(e), file=sys.stderr)
        return 1

    password = _read_password(a, u.get("userPrincipalName"))
    if password is None:
        print("Lösenorden skiljer sig, avbryter.", file=sys.stderr)
        return 2
    if not password:
        print("Tomt lösenord, avbryter.", file=sys.stderr)
        return 2

    synced = u.get("onPremisesSyncEnabled")
    print(f"Konto:  {u.get('displayName')} <{u.get('userPrincipalName')}>", file=sys.stderr)
    print(f"        {'AKTIVERAT' if u.get('accountEnabled') else 'BLOCKERAT'}, "
          f"{'synkat från lokalt AD' if synced else 'molnkonto'}, "
          f"lösenord ändrat {_ts(u.get('lastPasswordChangeDateTime'))}"
          f"{_age(u.get('lastPasswordChangeDateTime'))}", file=sys.stderr)

    # Where the password lives decides the route, and the account says which that
    # is — asking the operator to know would only be asking them to get it wrong.
    # A synced account's password belongs to AD, so that is where the write goes;
    # --entra forces the other way for the rare case that wants it.
    if synced and not a.entra:
        return _password_on_prem(a, u, password)
    if a.on_prem and not synced:
        print("        Molnkonto: det finns inte i AD, så --on-prem har inget att skriva "
              "till. Kör utan flaggan.", file=sys.stderr)
        return 2

    if (u.get("userType") or "").casefold() == "guest":
        # A B2B guest has no password in this tenant at all: it authenticates in
        # its home tenant, and the passwordProfile here is a stub Graph refuses to
        # write. Worth catching by name, because the 403 does not explain it.
        print("        Gästkonto: lösenordet ligger i gästens egen tenant och kan inte "
              "sättas härifrån.", file=sys.stderr)
        if not a.force:
            print("Avbryter.", file=sys.stderr)
            return 2

    if synced:
        # In a hybrid tenant this is where the command usually stops. Asking the
        # tenant beats guessing, and beats sending a PATCH that Graph refuses with
        # an error about "objects originated from an external service" that says
        # nothing about writeback being the reason.
        writeback = users.password_writeback()
        if writeback is False:
            print("        Synkat konto och password writeback är AVSLAGET i Entra Connect: "
                  "lösenordet ägs av lokalt AD.\n"
                  "        Entra tar inte emot bytet, och hash-synken skulle skriva över det "
                  "om den gjorde det.", file=sys.stderr)
            if not a.force:
                print("Avbryter. Utan --entra går kommandot till AD i stället, vilket är "
                      "det du vill (--force skickar till Entra ändå).", file=sys.stderr)
                return 2
        elif writeback is None:
            print("        Synkat konto: lösenordet ägs av lokalt AD. Om password writeback "
                  "är avslaget i Entra Connect tar Entra inte emot bytet.\n"
                  "        (Kunde inte läsa sync-inställningarna — kräver "
                  "OnPremDirectorySynchronization.Read.All.)", file=sys.stderr)
        else:
            print("        Synkat konto: lösenordet ägs av lokalt AD men writeback är på, "
                  "så bytet skrivs tillbaka dit.", file=sys.stderr)

    after = ("måste bytas vid nästa inloggning" if a.force_change else "gäller som det är")
    print(f"Efteråt: {after}"
          + (", med MFA innan bytet" if a.mfa else "")
          + (", alla sessioner återkallas" if a.revoke_sessions else "")
          + ".", file=sys.stderr)

    if a.dry_run:
        print(f"[torrkörning] {len(password)} tecken, inget skickat.", file=sys.stderr)
        if a.generate:
            print(password)
        return 0

    if not _confirm(a, "Sätt lösenordet i Entra? [j/N] "):
        print("Avbryter.", file=sys.stderr)
        return 1

    try:
        users.set_password(u["id"], password, force_change=a.force_change,
                           force_change_with_mfa=a.mfa)
    except users.UserError as e:
        print(str(e), file=sys.stderr)
        return 1

    # The password is the only thing on stdout, so `wsg user password X --generate`
    # can be piped into a password manager without the report coming with it.
    if a.generate:
        print(password)
    print(f"Lösenordet är satt på {u.get('userPrincipalName')}.", file=sys.stderr)
    return _revoke(a, u)


def _password_on_prem(a, u, password):
    """Reset a synced account where its password lives: in Active Directory.

    Entra keeps the account's on-prem DN, so the DC is told exactly which object
    to write rather than being asked to match a name.
    """
    identity = u.get("onPremisesDistinguishedName") or u.get("onPremisesSamAccountName")
    if not identity:
        print("        Kontot är märkt som synkat men saknar både DN och samAccountName "
              "i Entra, så det går inte att peka ut i AD.", file=sys.stderr)
        return 2
    try:
        dc = onprem.host(a.host)
    except onprem.OnPremError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"Skriver: {dc} (AD), {identity}", file=sys.stderr)
    after = ("måste bytas vid nästa inloggning" if a.force_change else "gäller som det är")
    print(f"Efteråt: {after}"
          + (", kontot låses upp om det är låst" if a.unlock else "")
          + (", delta-sync startas" if a.sync else "")
          + (", alla sessioner återkallas i Entra" if a.revoke_sessions else "")
          + ".", file=sys.stderr)
    if a.mfa:
        print("        --mfa gäller bara Entra-vägen och ignoreras här: AD har ingen "
              "motsvarighet.", file=sys.stderr)

    try:
        cred = onprem.credential(a.ad_user)
    except onprem.OnPremError as e:
        print(str(e), file=sys.stderr)
        return 2

    if a.dry_run:
        try:
            found = onprem.find_user(dc, cred, identity)
        except onprem.OnPremError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"[torrkörning] AD svarar: {found.get('samAccountName')}, "
              f"{'aktivt' if found.get('enabled') else 'BLOCKERAT'}"
              + (", LÅST" if found.get("lockedOut") else "")
              + f", lösenord satt {found.get('pwdLastSet') or 'aldrig'}", file=sys.stderr)
        print(f"[torrkörning] {len(password)} tecken, inget skrivet.", file=sys.stderr)
        if a.generate:
            print(password)
        return 0

    if not _confirm(a, f"Sätt lösenordet i AD på {dc}? [j/N] "):
        print("Avbryter.", file=sys.stderr)
        return 1

    before = u.get("lastPasswordChangeDateTime")
    try:
        r = onprem.set_password(dc, cred, identity, password,
                                force_change=a.force_change, unlock=a.unlock)
    except onprem.OnPremError as e:
        print(str(e), file=sys.stderr)
        return 1

    if a.generate:
        print(password)
    print(f"Lösenordet är satt i AD på {r.get('samAccountName') or identity}.", file=sys.stderr)
    if isinstance(r.get("forceChange"), str):
        print(f"  Men flaggan 'byt vid nästa inloggning' gick inte att sätta: "
              f"{r['forceChange']}", file=sys.stderr)
    if r.get("unlocked") is True:
        print("  Kontot var låst och har låsts upp.", file=sys.stderr)
    elif isinstance(r.get("unlocked"), str):
        print(f"  Kontot är låst och gick inte att låsa upp: {r['unlocked']}", file=sys.stderr)

    if a.sync:
        try:
            sync = onprem.delta_sync(dc, cred)
        except onprem.OnPremError as e:
            sync = {"ran": False, "reason": str(e)}
        if sync.get("ran"):
            print(f"  Delta-sync startad: {sync.get('result')}. Den flyttar attribut, "
                  f"inte lösenordet — det rider på hash-synken.", file=sys.stderr)
        else:
            print(f"  Ingen delta-sync: {sync.get('reason')}", file=sys.stderr)

    rc = _wait_for_sync(u, before, a.wait)
    return _revoke(a, u) or rc


def _wait_for_sync(u, before, seconds):
    """Poll Entra until the account's password date moves past what it was.

    This is the only honest proof that the reset reached the cloud: password hash
    sync runs on a 2-minute cycle of its own, and nothing on the AD side knows
    whether it has been through. Not finding it inside the window is reported as
    "not yet", never as a failure - the reset in AD already happened.
    """
    if not seconds:
        return 0
    print(f"Väntar på hash-synken (upp till {seconds}s)", end="", file=sys.stderr, flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(15)
        print(".", end="", file=sys.stderr, flush=True)
        try:
            now = users.resolve(u["id"]).get("lastPasswordChangeDateTime")
        except users.UserError:
            continue
        if now and now != before:
            print(f"\nEntra har det nya lösenordet: {_ts(now)}.", file=sys.stderr)
            return 0
    print(f"\nEntra visar fortfarande {_ts(before)}. Bytet i AD är gjort — hash-synken "
          f"går var annan minut, så kolla igen med: wsg user show "
          f"{u.get('userPrincipalName')}", file=sys.stderr)
    return 0


def _revoke(a, u):
    if not a.revoke_sessions:
        return 0
    try:
        users.revoke_sessions(u["id"])
    except users.UserError as e:
        print(f"Lösenordet är bytt men sessionerna lever kvar: {e}", file=sys.stderr)
        return 1
    print("Sessioner återkallade: utloggad från alla klienter inom en timme "
          "(redan utfärdade access-tokens lever tills de går ut).", file=sys.stderr)
    return 0


def cmd_user_onprem_check(a):
    """Whether the AD path is usable at all, before anyone needs it in a hurry."""
    try:
        dc = onprem.host(a.host)
    except onprem.OnPremError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"Domänkontrollant: {dc}", file=sys.stderr)
    saved = onprem.cred_path().exists()
    print("Inloggning:       "
          + (f"sparad i {onprem.cred_path()}" if saved
             else f"frågas vid körning ({onprem.ad_user(a.ad_user) or 'inget konto angivet'})"),
          file=sys.stderr)
    try:
        r = onprem.check(dc, onprem.credential(a.ad_user))
    except onprem.OnPremError as e:
        print(str(e), file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    print(f"Ansluten som:     {r.get('whoami')} på {r.get('computer')} ({r.get('domain')})")
    print(f"ActiveDirectory:  {'finns' if r.get('ad') else 'SAKNAS — RSAT-modulen behövs för lösenordsbyte'}")
    print(f"ADSync:           {'finns — Entra Connect kör här' if r.get('adsync') else 'saknas — Entra Connect kör på en annan server'}")
    if not r.get("adsync"):
        print("                  (ingen delta-sync härifrån; hash-synken går ändå av sig själv)")
    return 0


def _add_user_commands(sub):
    def user_arg(s):
        s.add_argument("user", help="UPN, object id, or part of a display name")

    s = sub.add_parser("find", help="search the directory by name, UPN or mail")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=25)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_user_find)

    s = sub.add_parser("show", help="account state: enabled, licences, last sign-in, "
                                    "password age, and the latest changes")
    user_arg(s)
    s.add_argument("--days", type=int, default=30, help="window for the change list, default 30")
    s.add_argument("--no-audit", action="store_true", help="skip the change list")
    s.add_argument("--auth-methods", action="store_true",
                   help="also list registered MFA methods (extra consent)")
    s.add_argument("-v", "--verbose", action="store_true", help="also proxy addresses")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_user_show)

    s = sub.add_parser("audit", help="who changed this account, when, and what")
    user_arg(s)
    s.add_argument("--days", type=int, default=30, help="default 30, which is the retention")
    s.add_argument("--top", type=int, default=100)
    s.add_argument("--by-user", action="store_true",
                   help="changes this account made, instead of changes made to it")
    s.add_argument("-v", "--verbose", action="store_true", help="every modified property")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_user_audit)

    s = sub.add_parser("signins", help="sign-in log for one account")
    user_arg(s)
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--top", type=int, default=50)
    s.add_argument("--failed", action="store_true", help="only failed sign-ins")
    s.add_argument("--kinds", choices=("all", "interactive", "noninteractive"),
                   default="all",
                   help="Graph returns interactive sign-ins only unless asked; "
                        "default here is all of them")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_user_signins)

    s = sub.add_parser("groups", help="group and directory role memberships")
    user_arg(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_user_groups)

    s = sub.add_parser("password", help="set a new password on an account; the value is "
                                        "read from stdin or generated, never an argument")
    user_arg(s)
    s.add_argument("--generate", action="store_true",
                   help="generate a password and print it on stdout, instead of reading one")
    s.add_argument("--length", type=int, default=20, metavar="N",
                   help="length of the generated password, default 20")
    s.add_argument("--no-force-change", dest="force_change", action="store_false",
                   help="let the password stand; default is that the user must change it "
                        "at next sign-in")
    s.add_argument("--mfa", action="store_true",
                   help="require MFA before that change (fails if the account has no method)")
    s.add_argument("--revoke-sessions", action="store_true",
                   help="also sign the account out everywhere; a reset alone leaves old "
                        "refresh tokens working")
    s.add_argument("--on-prem", dest="on_prem", action="store_true",
                   help="reset in Active Directory over WinRM; this is already the default "
                        "for a synced account, so the flag only makes it explicit")
    s.add_argument("--entra", action="store_true",
                   help="reset in Entra even for a synced account, where it is expected to "
                        "fail while password writeback is off")
    s.add_argument("--host", metavar="DC",
                   help="domain controller to reset on, default WSG_AD_HOST or the "
                        "configured one")
    s.add_argument("--ad-user", dest="ad_user", metavar="USER",
                   help="AD account that signs in to the DC, e.g. you@ad.example.com; "
                        "default WSG_AD_USER or the configured one")
    s.add_argument("--sync", action="store_true",
                   help="with --on-prem, also start a delta sync cycle (note: the password "
                        "itself rides hash sync, not this)")
    s.add_argument("--wait", type=int, default=240, metavar="SEC",
                   help="with --on-prem, wait this long for Entra to show the new password "
                        "date; 0 skips the check (default 240)")
    s.add_argument("--no-unlock", dest="unlock", action="store_false",
                   help="with --on-prem, leave a locked-out account locked")
    s.add_argument("--dry-run", action="store_true", help="show the account and stop")
    s.add_argument("--force", action="store_true",
                   help="send it even where it is known to fail: a synced account with "
                        "writeback off, or a guest")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    s.set_defaults(fn=cmd_user_password)

    s = sub.add_parser("onprem-check", help="can we reach the domain controller, and does "
                                            "Entra Connect run there")
    s.add_argument("--host", metavar="DC", help="default WSG_AD_HOST or the configured one")
    s.add_argument("--ad-user", dest="ad_user", metavar="USER",
                   help="AD account that signs in to the DC; default WSG_AD_USER or the "
                        "configured one")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_user_onprem_check)


def _add_graph_commands(sub, visible=True):
    """Graph subcommands. Registered under `wsg graph` and, for compatibility with
    scripts written against 0.1 (`wsg token --header`), as hidden top-level aliases."""
    # argparse lists a subcommand in --help only when the `help` kwarg is present,
    # so the aliases are added without one.
    h = (lambda t: {"help": t}) if visible else (lambda t: {})

    s = sub.add_parser("login", **h("sign in interactively and cache the token"))
    s.add_argument("--scopes", help="comma separated, default is the full list in wsg/scopes.py")
    s.add_argument("--open", action="store_true", help="open the default browser instead of just printing the URL")
    s.add_argument("--browser", metavar="NAME", help="open in this browser: edge, chrome, firefox, or an executable")
    s.add_argument("--device", action="store_true", help="use device code flow (for machines without a browser)")
    s.add_argument("--user", help="login hint, e.g. name@domain")
    s.add_argument("--timeout", type=int, default=600, help="seconds to wait for the browser, default 600")
    s.set_defaults(fn=cmd_graph_login)

    s = sub.add_parser("token", **h("print a valid access token, refreshing silently"))
    s.add_argument("--scopes", help="require these scopes (comma separated)")
    s.add_argument("--header", action="store_true", help="print as 'Bearer <token>'")
    s.set_defaults(fn=cmd_graph_token)

    s = sub.add_parser("status", **h("show who is signed in and what was granted"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_graph_status)

    s = sub.add_parser("whoami", **h("call Graph /me"))
    s.set_defaults(fn=cmd_graph_whoami)

    s = sub.add_parser("sendmail", **h("send mail as yourself via /me/sendMail; body read from stdin"))
    s.add_argument("--to", required=True, help="recipient, or several comma-separated")
    s.add_argument("--cc", help="cc recipients, comma-separated")
    s.add_argument("--subject", required=True)
    s.add_argument("--attach", action="append", metavar="FILE",
                   help="attach a file inline (repeatable; ~3 MB total after base64)")
    s.add_argument("--html", action="store_true", help="send the body as HTML instead of plain text")
    s.add_argument("--no-save", action="store_true", help="do not keep a copy in Sent Items")
    s.add_argument("--dry-run", action="store_true", help="print the exact request and send nothing")
    s.set_defaults(fn=cmd_graph_sendmail)

    s = sub.add_parser("logout", **h("delete the cached token"))
    s.set_defaults(fn=cmd_graph_logout)


def _add_arena_commands(sub):
    s = sub.add_parser("login", help="sign in to Arena through Cloudflare Access (Entra + MFA)")
    s.add_argument("--timeout", type=int, default=600, help="seconds to wait for the browser, default 600")
    s.set_defaults(fn=cmd_arena_login)

    s = sub.add_parser("token", help="print the cached Access JWT")
    s.add_argument("--header", action="store_true", help="print as 'cf-access-token: <jwt>'")
    s.set_defaults(fn=cmd_arena_token)

    s = sub.add_parser("status", help="who is signed in to Arena and until when")
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


# ── Garp ERP ──────────────────────────────────────────────────────────────────
# The ERP half of the same question. `wsg ongoing last` says whether the warehouse
# got the order; this says whether Garp ever sent it. Read together they locate the
# stop, which neither can do alone.


def cmd_garp_last(a):
    try:
        rows = garp.last_per_channel(a.company, days=a.days)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    names = ongoing.channel_names()
    print(f"GARP   senaste order per kanal, senaste {a.days} dygnen\n")
    waiting_total = 0
    for comp in sorted({(r["company"], r["companyName"]) for r in rows}):
        mine = [r for r in rows if r["company"] == comp[0]]
        if not mine:
            continue
        # Letter codes are named B2B salespeople with their own rhythm; numeric ones
        # are the webshops and marketplaces this is really about.
        mine = [r for r in mine if a.all or r["channel"].isdigit() or r["waiting"]]
        print(f"{comp[1].upper()} ({comp[0]}) — {len(mine)} kanaler")
        w = max([len(_label(r["channel"], names, None)) for r in mine] + [22]) if mine else 22
        for r in sorted(mine, key=lambda r: r["createdAt"] or ""):
            waiting_total += r["waiting"]
            # A queue that is not draining is the finding; say how old its head is.
            q = ""
            if r["waiting"]:
                q = f"  väntar {r['waiting']:>3}"
                if r["oldestWaitingAt"]:
                    q += f", äldsta {_for(r['oldestWaitingAt'])}"
            print(f"  {_label(r['channel'], names, None):{w}}{_for(r['createdAt']):>13}"
                  f"  {str(r['order'] or ''):>10}{q}")
        print()

    if waiting_total:
        # Saying "the handover is broken" here was wrong: orders wait on stock, on
        # holds, on split deliveries, and the count sits in the dozens on a healthy
        # day. Only the warehouse can settle it, so point at it instead of guessing.
        print(f"{waiting_total} ordrar saknar plocklista. Det är inte ett fel i sig — "
              f"ordrar väntar också på lagersaldo och spärrar.")
        print("Jämför med `wsg ongoing last`: får lagret ordrar nu hålls de av något "
              "annat skäl, är lagret tyst samtidigt är det överlämningen som står.")
    else:
        print("Inga ordrar väntar på plocklista.")
    return 1 if waiting_total else 0


def _add_garp_commands(sub):
    s = sub.add_parser("last", help="latest order per channel in Garp, and what has no picking list")
    s.add_argument("--company", help="002 / 013, or a configured name; default is both")
    s.add_argument("--days", type=int, default=2, metavar="N", help="look back N days (default 2)")
    s.add_argument("--all", action="store_true", help="include B2B salespeople with nothing waiting")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_garp_last)


# ── Garp SQL ──────────────────────────────────────────────────────────────────
# The same box as `wsg garp`, the other door. The REST API on 8443 answers what
# Garp was built to answer; the SQL mirror on 1433 answers the rest, and carries
# the integration databases that never went near the ERP.


def _print_rows(rows, empty="Inga rader."):
    """Left-align text, right-align numbers, and let the widest value set the column."""
    if not rows:
        print(empty)
        return
    cols = list(rows[0].keys())
    numeric = {c: all(isinstance(r.get(c), (int, float)) or r.get(c) is None for r in rows)
               for c in cols}
    def cell(v):
        return "" if v is None else str(v)
    width = {c: max([len(c)] + [len(cell(r.get(c))) for r in rows]) for c in cols}
    print("  ".join(c.ljust(width[c]) if not numeric[c] else c.rjust(width[c]) for c in cols))
    print("  ".join("-" * width[c] for c in cols))
    for r in rows:
        print("  ".join(cell(r.get(c)).ljust(width[c]) if not numeric[c]
                        else cell(r.get(c)).rjust(width[c]) for c in cols))


def cmd_garpdb_check(a):
    try:
        row = garpdb.check()
    except garpdb.GarpDbError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"Server:    {row.get('server_name')}")
    print(f"Version:   {row.get('version')}")
    print(f"Login:     {row.get('login_name')}")
    print(f"Databas:   {row.get('database_name')}")
    reads = "ja" if row.get("datareader") else "nej"
    denied = "ja" if row.get("denydatawriter") else "nej"
    print(f"Lasratt:   {reads}    skrivsparr: {denied}")
    if row.get("sysadmin"):
        # Worth saying plainly: the read-only promise in the module docstring
        # rests on db_denydatawriter, and sysadmin ignores it.
        print("\nVARNING: det har loginet ar sysadmin. Skrivsparren galler inte for det.")
    return 0


def cmd_garpdb_databases(a):
    try:
        rows = garpdb.databases()
    except garpdb.GarpDbError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    open_ = [r for r in rows if r.get("has_access")]
    shut = [r for r in rows if not r.get("has_access")]
    for r in rows:
        r["has_access"] = "ja" if r["has_access"] else "nej"
    _print_rows(rows, "Inga databaser.")
    print(f"\n{len(open_)} oppna for loginet, {len(shut)} stangda.")
    return 0


def cmd_garpdb_tables(a):
    try:
        rows = garpdb.tables(a.db)
    except garpdb.GarpDbError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    _print_rows(rows, "Databasen har inga tabeller, eller inga som loginet ser.")
    return 0


def cmd_garpdb_query(a):
    sql = a.sql
    if not sql:
        if sys.stdin.isatty():
            print("Ge fragan som argument eller pipa in den pa stdin.", file=sys.stderr)
            return 2
        sql = _stdin_text()
    try:
        rows = garpdb.query(sql, a.db, timeout=a.timeout)
    except garpdb.GarpDbError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    _print_rows(rows)
    if rows:
        print(f"\n{len(rows)} rader.")
    return 0


def _add_garpdb_commands(sub):
    s = sub.add_parser("check", help="who the login is on the instance, and what it may do")
    s.set_defaults(fn=cmd_garpdb_check)

    s = sub.add_parser("databases", help="every database on the instance, and which ones the login may open")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_garpdb_databases)

    s = sub.add_parser("tables", help="tables in one database with row counts and size")
    s.add_argument("--db", help=f"database to read (default {garpdb.DEFAULT_DATABASE})")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_garpdb_tables)

    s = sub.add_parser("query", help="run one SELECT; the statement may also come from stdin")
    s.add_argument("sql", nargs="?", help="the SELECT to run; omit it to read from stdin")
    s.add_argument("--db", help=f"database to read (default {garpdb.DEFAULT_DATABASE})")
    s.add_argument("--timeout", type=int, default=180, metavar="S", help="seconds to wait (default 180)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_garpdb_query)


# ── Ongoing WMS ───────────────────────────────────────────────────────────────
# Our own client, straight at the warehouse. Ongoing can say whether it got an
# order; it cannot say why it did not, because it knows nothing about the webshop
# or the ERP. Read it as evidence, not as a verdict.


def cmd_ongoing_accounts(a):
    try:
        rows = ongoing.accounts()
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        # The key never leaves the config file, not even into a script's stdout.
        print(json.dumps([{k: v for k, v in r.items() if k != "key"} for r in rows],
                         indent=2, ensure_ascii=False))
        return 0
    print(f"konfiguration  {ongoing.config_path()}\n")
    for r in rows:
        print(f"  {r['name']:14} {r['url']:52} goodsOwnerId={r['goodsOwnerId']}  "
              f"api-användare {r['user']}")
    return 0


def _label(code, names, account=None):
    """A channel as `code name`, or the bare code when the map has never heard of it.

    The map is one goods owner's numbering, so it is only applied to that owner's
    rows. That matters most for the EMPTY code: on Vartex it means the vartex.se
    dealer portal, but on Rawbike or Living Sport an order with no marketPlace is
    simply their own — labelling it vartex.se would be a plain lie.
    """
    owner = names.get("__account__")
    if owner and account and owner.lower() != account.lower():
        return code or "(utan kanal)"
    name = (names.get(code or "") or {}).get("name")
    if not name:
        return code or "(utan kanal)"
    return f"{(code or '—'):5}{name}"


def cmd_ongoing_last(a):
    try:
        if a.sweep or not ongoing.channel_names():
            result = ongoing.last_per_channel(a.account, days=a.days, max_orders=a.top)
        else:
            result = ongoing.last_by_query(a.account)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    rows = result["channels"]
    # Names come from a local map built out of pulse; missing is normal, not an error.
    names = ongoing.channel_names()

    # Asking per channel reaches every code the register has ever had, including ones
    # retired years ago — MP Cdon.dk last sold 853 days ago. A dead code is not a quiet
    # channel, and listing them buries the ones that mean something. The cutoff is
    # generous enough that a genuinely seasonal channel survives it.
    dormant = []
    if result.get("mode") == "query" and not a.all:
        keep = []
        for r in rows:
            age = _dt(r["createdAt"])
            days_ago = (datetime.now(age.tzinfo) - age).days if age else 0
            (keep if days_ago <= a.dormant else dormant).append(r)
        rows = keep

    if result.get("mode") == "query":
        # One query per known channel: no window to disclose, because each answer is
        # that channel's latest order however old it is.
        print(f"LAGRET   senaste order per kanal, en fråga per kanal ur registret")
    else:
        window = f"senaste {a.days} dygnen" if a.days else f"de {a.top} senaste ordrarna"
        print(f"LAGRET   senaste order per kanal, ur {window}")
    if result.get("reach"):
        # The weakest account's reach — that is the coverage of the whole answer.
        who = f", svagast: {result['reachAccount']}" if result.get("reachAccount") else ""
        print(f"         täcker tillbaka till {_ts(result['reach'])} "
              f"({_for(result['reach'])} sedan{who})")
    print()
    if not rows:
        print("  Inga ordrar i fönstret.")
        return 0
    # Grouped per goods owner rather than one flat age-sorted list: these are separate
    # legal entities sharing a warehouse, and a row means nothing until you know whose
    # it is. Group order is config order, which puts the main goods owner first.
    for acc in result.get("accounts") or [{"name": r["account"]} for r in rows]:
        mine = [r for r in rows if r["account"] == acc["name"]]
        if not mine:
            continue
        owner = f" (goodsOwnerId {acc['goodsOwnerId']})" if acc.get("goodsOwnerId") else ""
        n = f"{len(mine)} {'kanal' if len(mine) == 1 else 'kanaler'}"
        print(f"{acc['name'].upper()}{owner} — {n}")
        w = max([len(_label(r["channel"], names, acc["name"])) for r in mine] + [22])

        def line(r):
            # referenceNumber where there is one, otherwise the order number — which
            # on some accounts is a customer name, so the column is "whatever the
            # warehouse calls this order" rather than an order number.
            ref = r.get("reference") or r.get("orderNumber") or ""
            print(f"  {_label(r['channel'], names, acc['name']):{w}}{_for(r['createdAt']):>13}"
                  f"  {ref:16} {r.get('status') or ''}")

        # Storefront groups come from pulse's migration and cover only the domains it
        # named, so most channels have none. Group order is fixed rather than derived:
        # a block that moves between runs is a block you have to re-find every time.
        owner_ok = not names.get("__account__") or \
            names["__account__"].lower() == acc["name"].lower()
        buckets = {}
        for r in mine:
            g = (names.get(r["channel"] or "") or {}).get("group") if owner_ok else None
            buckets.setdefault(g, []).append(r)
        order = [g for g in ("Vartex Outdoor", "Vartex Fitness", "Vartex MC") if g in buckets]
        order += sorted(g for g in buckets if g and g not in order)
        blocks = [(g, buckets[g]) for g in order]
        if buckets.get(None):
            blocks.append(("Ogrupperade i pulse" if order else None, buckets[None]))

        # Summary first: one line per group with its freshest order. A group that is
        # minutes old next to one that is hours old is the comparison worth making,
        # and it should not require reading to the end of every block to find. Same
        # order as the detail below, so a summary line maps onto a block by position
        # rather than by searching.
        if len(blocks) > 1 or a.summary:
            gw = max(len(g or "") for g, _ in blocks)
            # _label pads the code into a column; a summary line wants it tight, but
            # the "äldst" half still has to line up across rows to be scannable.
            tight = lambda r: " ".join(_label(r["channel"], names, acc["name"]).split())
            nw = max(len(tight(b[-1])) for _, b in blocks)
            for g, bucket in blocks:
                newest = bucket[-1]           # rows are oldest-first
                who = f"{tight(newest):{nw}}" if a.summary else tight(newest)
                # The oldest is the one worth acting on, so a summary that hides the
                # rows still has to say how far behind the group's slowest channel is.
                oldest = bucket[0]
                behind = ""
                if len(bucket) > 1 and a.summary:
                    behind = f"   äldst {_for(oldest['createdAt']):>11}: {tight(oldest)}"
                print(f"  {(g or ''):{gw}}  {_for(newest['createdAt']):>13}  {who}{behind}")
            print()

        if a.summary:
            continue
        for g, bucket in blocks:
            if g:
                print(f"  ── {g}")
            for r in bucket:
                line(r)
        print()
    # A channel absent here is not idle. Whether that absence carries any
    # information at all depends on whether the sweep was cut short.
    owner = names.get("__account__")
    unknown = sorted({r["channel"] for r in rows
                      if (not owner or r["account"].lower() == owner.lower())
                      and not (names.get(r["channel"] or "") or {}).get("name")})
    unknown = [u for u in unknown if u]      # the empty code is a channel, not a gap
    if unknown and names:
        # A code the map has not heard of is a channel nobody told pulse about.
        print(f"Utan namn i kanalkartan: {', '.join(unknown)} "
              f"— lägg till i pulse och kör scripts/channels-from-pulse.py.\n")
    print(f"{len(rows)} {'kanal' if len(rows) == 1 else 'kanaler'} totalt.", end=" ")
    if result.get("mode") == "query":
        if dormant:
            print(f"{len(dormant)} vilande (ingen order på {a.dormant} dygn, --all visar dem).", end=" ")
        # The register is the channel list here, so a channel outside it is invisible
        # — the opposite blind spot to the sweep's, and worth naming as such.
        print("Kanaler utanför Garps register syns inte — kör --sweep för att leta.")
    elif result.get("capped"):
        print(f"Hämtningen slog i taket på {a.top} ordrar, så en kanal som saknas "
              f"säger ingenting — höj --top.")
    else:
        print("Ingen kanal utöver dessa har en order i fönstret.")
    return 0


def _add_ongoing_commands(sub):
    s = sub.add_parser("accounts", help="the configured Ongoing accounts (never the key)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ongoing_accounts)

    s = sub.add_parser("last", help="how long since the warehouse last got an order, per channel")
    s.add_argument("--account", help="one configured account; default is all of them")
    s.add_argument("--days", type=int, help="look back N days instead of the newest --top orders")
    s.add_argument("--top", type=int, default=ongoing.DEFAULT_MAX_ORDERS, metavar="N",
                   help=f"how many recent orders to sweep (default {ongoing.DEFAULT_MAX_ORDERS})")
    s.add_argument("--all", action="store_true", help="include dormant channels")
    s.add_argument("--dormant", type=int, default=90, metavar="DAYS",
                   help="a channel with no order in this many days is dormant (default 90)")
    s.add_argument("--sweep", action="store_true",
                   help="fetch every order in the window instead of one per known channel")
    s.add_argument("-s", "--summary", action="store_true",
                   help="one line per group, newest and oldest; no channel rows")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ongoing_last)


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
                    "Services: graph (Microsoft Graph via Entra), arena (WeSports Arena via Cloudflare Access).",
    )
    # metavar hides the compatibility aliases from the usage line.
    sub = p.add_subparsers(dest="cmd", required=True,
                           metavar="{graph,user,arena,intune,ongoing,garp,garpdb,calendar,planner}")

    g = sub.add_parser("graph", help="Microsoft Graph: sign in as yourself with Entra, get a Graph token")
    _add_graph_commands(g.add_subparsers(dest="graph_cmd", required=True))

    us = sub.add_parser("user", help="M365/Entra accounts: state, licences, sign-ins, audit trail")
    _add_user_commands(us.add_subparsers(dest="user_cmd", required=True))

    ar = sub.add_parser("arena", help="WeSports Arena via Cloudflare Access (needs cloudflared)")
    _add_arena_commands(ar.add_subparsers(dest="arena_cmd", required=True))

    it = sub.add_parser("intune", help="Intune app management: list, status, publish Store and Win32 apps")
    _add_intune_commands(it.add_subparsers(dest="intune_cmd", required=True))

    og = sub.add_parser("ongoing", help="Ongoing WMS directly: what the warehouse has actually received")
    _add_ongoing_commands(og.add_subparsers(dest="ongoing_cmd", required=True))

    ga = sub.add_parser("garp", help="Garp ERP, read-only: what the ERP took in and has not handed over")
    _add_garp_commands(ga.add_subparsers(dest="garp_cmd", required=True))

    gd = sub.add_parser("garpdb", help="Garp's SQL Server directly, read-only: the ERP mirror and "
                                       "the integration databases beside it")
    _add_garpdb_commands(gd.add_subparsers(dest="garpdb_cmd", required=True))

    ca = sub.add_parser("calendar", help="mirror a private booking onto the work calendar as busy")
    _add_calendar_commands(ca.add_subparsers(dest="calendar_cmd", required=True))

    pl = sub.add_parser("planner", help="Planner boards, read-only: what is on them and what is mine")
    _add_planner_commands(pl.add_subparsers(dest="planner_cmd", required=True))

    _add_graph_commands(sub, visible=False)   # wsg login / token / status / whoami / logout

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
