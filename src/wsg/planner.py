"""
Planner over Graph, read-only: which boards exist, what is on them, and what is
mine.

Four things here that are easy to get wrong by hand:

1. **"My plans" is membership, not everything.** `/me/planner/plans` answers with
   the boards the account belongs to: the group ones from the groups it is a
   member of, its own personal ones, and the shared lists it was added to. A
   board someone only sent a link to is not in the list - and Graph says so with
   an empty array rather than an error, so a board that is missing looks exactly
   like a board with nothing on it. Such a board is still readable by id, which
   is why `resolve_plan` falls back to fetching one.
2. **Assignees are GUIDs.** A task carries `assignments` keyed by user object id
   and no name anywhere. One POST to `/directoryObjects/getByIds` turns a whole
   board into names; resolving them per task is one round trip per assignee, and
   a board with sixty tasks then takes a minute to print.
3. **Progress is a number, not a state.** `percentComplete` is 0, 1-99 or 100,
   and that is what Planner's own Not started / In progress / Completed columns
   read. There is no separate status field to filter on.
4. **Board order is the order hint.** Buckets and tasks have no index, only an
   `orderHint` string that sorts into the order the board shows when compared as
   an ordinary string. Sorting on title instead prints a board nobody recognises.

Reading needs the `Tasks.Read` scope. It was added to the defaults later than
most, so an account that signed in before then gets HTTP 403 from every call
here until `wsg graph login` runs once more - which is what the 403 says.
"""

import json
from urllib import parse, request
from urllib.error import HTTPError

from .auth import decode_jwt, get_token

GRAPH = "https://graph.microsoft.com/v1.0"

# Where a board actually opens. Planner's web app moved to planner.cloud.microsoft;
# tasks.office.com is the old host and still redirects, which is what --legacy is
# for if a link ever lands wrong.
WEB = "https://planner.cloud.microsoft/webui"
WEB_LEGACY = "https://tasks.office.com"

# getByIds takes at most 1000 ids per request. A single board never comes close,
# but `mine` across every plan in a big tenant can, so the lookup chunks.
ID_BATCH = 1000


class PlannerError(Exception):
    pass


def _call(path, method="GET", body=None, token=None):
    token = token or get_token()
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data:
        headers["Content-Type"] = "application/json"
    elif method == "POST":
        headers["Content-Length"] = "0"
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        detail = (e.read().decode("utf-8", "replace") or "").strip()
        if e.code == 403:
            raise PlannerError(
                f"{method} {url.split('?')[0]} gav HTTP 403.\n"
                "Planner kräver scopet Tasks.Read. Logga in en gång till så hämtas det:\n"
                "  wsg graph login"
            ) from None
        raise PlannerError(f"{method} {url.split('?')[0]} gav HTTP {e.code}\n{detail[:800]}") from None


def _paged(path):
    out, url = [], path
    while url:
        page = _call(url)
        out.extend(page.get("value", []))
        url = page.get("@odata.nextLink")
    return out


def _looks_like_guid(s):
    return len(s) == 36 and s.count("-") == 4


# ── names ─────────────────────────────────────────────────────────────────────

def names(ids, kind=None):
    """{object id: display name} for users or groups, in as few calls as possible.

    Ids that no longer resolve - a deleted account still assigned to a task - are
    left out, so callers fall back to the raw GUID rather than printing nothing.
    """
    ids = [i for i in dict.fromkeys(ids) if i]
    out = {}
    for i in range(0, len(ids), ID_BATCH):
        body = {"ids": ids[i:i + ID_BATCH]}
        if kind:
            body["types"] = [kind]
        for o in _call("/directoryObjects/getByIds", method="POST", body=body).get("value", []):
            if o.get("id"):
                out[o["id"]] = o.get("displayName") or o.get("userPrincipalName") or o["id"]
    return out


# ── plans ─────────────────────────────────────────────────────────────────────

def plans():
    """Every plan the signed-in account can see, each with its owner's name.

    The owner is `container.containerId` on current plans and the legacy `owner`
    on older ones; both are read, so a board made years ago still shows what it
    belongs to.

    A container is not always a group. A personal board is contained by the user
    who made it and a shared list by a roster, so the lookup asks for no
    particular type - pinning it to `group` turns every personal board's heading
    into a bare GUID. A roster is not a directory object at all and never
    resolves, which is why that one falls back to a word rather than an id.
    """
    rows = _paged("/me/planner/plans")
    for p in rows:
        c = p.get("container") or {}
        p["_owner"] = c.get("containerId") or p.get("owner")
        p["_owner_type"] = c.get("type") or "group"
    found = names([p["_owner"] for p in rows])
    for p in rows:
        p["_owner_name"] = _owner_name(p, found)
    return rows


def _owner_name(p, found):
    known = found.get(p.get("_owner") or "")
    if known:
        return known
    if p.get("_owner_type") == "roster":
        return "Delad lista"
    return p.get("_owner") or "-"


def resolve_plan(who, known=None):
    """One plan from an id or a fragment of its title.

    An ambiguous fragment raises rather than picking the first hit: printing the
    wrong board is worse than asking again, and two teams both naming a plan
    "Drift" is the normal case rather than the odd one.
    """
    rows = known if known is not None else plans()
    if _looks_like_guid(who):
        for p in rows:
            if p.get("id") == who:
                return p
        # A plan can be readable without being in /me/planner/plans: a board in a
        # group we are not a member of, opened from a link someone sent.
        p = _call(f"/planner/plans/{parse.quote(who)}")
        c = p.get("container") or {}
        p["_owner"] = c.get("containerId") or p.get("owner")
        p["_owner_type"] = c.get("type") or "group"
        p["_owner_name"] = _owner_name(p, names([p["_owner"]]))
        return p
    needle = who.casefold()
    hits = [p for p in rows if needle in (p.get("title") or "").casefold()]
    if not hits:
        raise PlannerError(f"Ingen plan matchade {who!r}. 'wsg planner plans' listar dem.")
    if len(hits) > 1:
        lines = "\n  ".join(f"{p.get('title')}  ({p.get('_owner_name')})  {p.get('id')}"
                            for p in hits[:10])
        raise PlannerError(f"{len(hits)} planer matchade {who!r}:\n  {lines}\nAnge plan-id.")
    return hits[0]


# ── tasks ─────────────────────────────────────────────────────────────────────

def buckets(plan_id):
    """The plan's columns, in board order."""
    return sorted(_paged(f"/planner/plans/{parse.quote(plan_id)}/buckets"),
                  key=lambda b: b.get("orderHint") or "")


def tasks(plan_id, include_done=False):
    """Every task on one plan, in board order, with assignee names filled in."""
    rows = _paged(f"/planner/plans/{parse.quote(plan_id)}/tasks")
    if not include_done:
        rows = [t for t in rows if (t.get("percentComplete") or 0) < 100]
    return _with_assignees(sorted(rows, key=lambda t: t.get("orderHint") or ""))


def my_tasks(include_done=False):
    """Tasks assigned to the signed-in account, across every plan it can see.

    Sorted by due date with the undated last, because the question this answers
    is what is closest, not what board it sits on.
    """
    rows = _paged("/me/planner/tasks")
    if not include_done:
        rows = [t for t in rows if (t.get("percentComplete") or 0) < 100]
    return _with_assignees(sorted(rows, key=lambda t: (t.get("dueDateTime") or "9999",
                                                       t.get("title") or "")))


def task(task_id):
    """One task, with the description and checklist that only /details carries."""
    t = _call(f"/planner/tasks/{parse.quote(task_id)}")
    try:
        t["_details"] = _call(f"/planner/tasks/{parse.quote(task_id)}/details")
    except PlannerError:
        # A task can be readable while its details are not. The task is still
        # worth printing without them.
        t["_details"] = {}
    return _with_assignees([t])[0]


def _with_assignees(rows):
    who = names([uid for t in rows for uid in (t.get("assignments") or {})], kind="user")
    for t in rows:
        t["_assignees"] = [who.get(uid, uid) for uid in (t.get("assignments") or {})]
    return rows


# ── links ─────────────────────────────────────────────────────────────────────

def tenant_id():
    """The tenant the cached token was issued for, read from its own `tid` claim.

    A Planner link carries the tenant so it opens the right one for someone
    signed into several; without it the web app picks whichever session it finds
    and answers "you don't have access" for a board that is perfectly readable.
    """
    return decode_jwt(get_token()).get("tid") or ""


def url(plan_id, task_id=None, legacy=False):
    """A clickable link to a board, or to one card on it.

    Graph hands out no `webUrl` for a plan or a task the way it does for a file
    or an event, so the address is assembled here rather than read. The legacy
    form is kept because it needs no plan id to open a task, which is the one
    thing the new one cannot do.
    """
    tid = tenant_id()
    if legacy:
        if task_id:
            return f"{WEB_LEGACY}/{tid}/Home/Task/{parse.quote(task_id)}"
        return f"{WEB_LEGACY}/{tid}/Home/PlanViews/{parse.quote(plan_id)}"
    base = f"{WEB}/plan/{parse.quote(plan_id)}/view/board"
    if task_id:
        base += f"/task/{parse.quote(task_id)}"
    return f"{base}?tid={tid}" if tid else base


def progress(task_row):
    """What Planner's own three columns call this percentComplete."""
    pct = task_row.get("percentComplete") or 0
    return "klar" if pct >= 100 else ("pågår" if pct > 0 else "ej påbörjad")


def checklist(task_row):
    """The checklist as [(done, title)], in the order the task keeps it."""
    items = ((task_row.get("_details") or {}).get("checklist") or {}).values()
    return [(bool(i.get("isChecked")), i.get("title") or "")
            for i in sorted(items, key=lambda i: i.get("orderHint") or "")]
