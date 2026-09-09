import argparse
import json
import sys
import time
from datetime import datetime

from . import arena, auth, intune
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
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{graph,arena,intune}")

    g = sub.add_parser("graph", help="Microsoft Graph: sign in as yourself with Entra, get a Graph token")
    _add_graph_commands(g.add_subparsers(dest="graph_cmd", required=True))

    ar = sub.add_parser("arena", help="WeSports Arena via Cloudflare Access (needs cloudflared)")
    _add_arena_commands(ar.add_subparsers(dest="arena_cmd", required=True))

    it = sub.add_parser("intune", help="Intune app management: list, status, publish Store and Win32 apps")
    _add_intune_commands(it.add_subparsers(dest="intune_cmd", required=True))

    _add_graph_commands(sub, visible=False)   # wsg login / token / status / whoami / logout

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
