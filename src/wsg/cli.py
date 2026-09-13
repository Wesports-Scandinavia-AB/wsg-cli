import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import arena, auth, intune, mscal, ongoing, users
from .scopes import DEFAULT_SCOPES


def _scopes_arg(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else list(DEFAULT_SCOPES)


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
    body_text = sys.stdin.read()
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
        value = sys.stdin.read()
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
        ev = json.loads(sys.stdin.read() or "{}")
    except ValueError as e:
        print(f"Kunde inte tolka JSON från stdin: {e}", file=sys.stderr)
        return 2
    if not isinstance(ev, dict) or not ev.get("id"):
        print("Ingen Google-event i indatan (saknar 'id').", file=sys.stderr)
        return 2

    try:
        body = mscal.body_for(ev)
        if a.dry_run:
            print("[torrkörning] POST /me/events — inget bokat")
            print(json.dumps(body, indent=2, ensure_ascii=False))
            return 0
        made = mscal.mirror(ev)
    except mscal.MirrorError as e:
        print(str(e), file=sys.stderr)
        return 1
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2

    when = (made.get("start") or {}).get("dateTime") or "?"
    print(f"{mscal.TITLE} {_ts(when)} — {(ev.get('summary') or 'namnlös bokning')} "
          f"speglad. id={made.get('id', '')[:24]}")
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
    for ev in rows:
        start = (ev.get("start") or {}).get("dateTime")
        end = (ev.get("end") or {}).get("dateTime")
        print(f"  {ev.get('subject')}  {_ts(start)} – {_ts(end)}  "
              f"{ev.get('showAs')}/{ev.get('sensitivity')}")
        print(f"  {'':4}id={ev.get('id')}")
    # More than one means an earlier run got past transactionId somehow; say so
    # rather than let the count pass unnoticed.
    if len(rows) > 1:
        print(f"\n{len(rows)} speglingar av samma event — en av dem är övertalig.")
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
            print(f"  {ev.get('subject')}  {_ts((ev.get('start') or {}).get('dateTime'))}")
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
    s.add_argument("--dry-run", action="store_true", help="print the Graph body and book nothing")
    s.set_defaults(fn=cmd_calendar_mirror)

    s = sub.add_parser("show", help="whether a Google event is mirrored, and where")
    s.add_argument("google_id", metavar="GOOGLE-ID")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_calendar_show)

    s = sub.add_parser("unmirror", help="remove the block; the Google event is left alone")
    s.add_argument("google_id", metavar="GOOGLE-ID")
    s.add_argument("--yes", action="store_true", help="actually delete; without it, only lists")
    s.set_defaults(fn=cmd_calendar_unmirror)


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


def cmd_ongoing_last(a):
    try:
        result = ongoing.last_per_channel(a.account, days=a.days, max_orders=a.top)
    except auth.AuthError as e:
        print(str(e), file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    rows = result["channels"]

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
    multi = len({r["account"] for r in rows}) > 1
    for r in rows:
        acc = f"{r['account']:10} " if multi else ""
        ref = r.get("reference") or r.get("orderNumber") or ""
        print(f"  {acc}{(r['channel'] or '(utan kanal)'):18}{_for(r['createdAt']):>13}"
              f"  {ref:16} {r.get('status') or ''}")
    # A channel absent here is not idle. Whether that absence carries any
    # information at all depends on whether the sweep was cut short.
    print(f"\n{len(rows)} {'kanal' if len(rows) == 1 else 'kanaler'}.", end=" ")
    if result.get("capped"):
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
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_ongoing_last)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
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
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{graph,user,arena,intune,ongoing,calendar}")

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

    ca = sub.add_parser("calendar", help="mirror a private booking onto the work calendar as busy")
    _add_calendar_commands(ca.add_subparsers(dest="calendar_cmd", required=True))

    _add_graph_commands(sub, visible=False)   # wsg login / token / status / whoami / logout

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
