"""
Mirror a private calendar booking onto the work calendar over Graph.

The point is to block the time, not to tell the office what it is: every mirrored
event is called "Upptagen" and carries no description, no location and no guests.
A title from a private calendar never reaches a calendar colleagues can open.

Two problems this has to get right, both of which bite silently.

**Duplicates.** Running the same mirror twice must not put two blocks on the
calendar. Graph has `transactionId` for exactly this: POST the same id again and
it hands back the event it already made instead of a second one. That is checked,
not assumed - the second POST answers 201 with the same event id as the first.

**Time.** A booking is mirrored as the instant it actually is, in UTC, rather than
as wall-clock plus a zone guessed here. Wall-clock and a zone is how a booking
ends up an hour out across a clock change, and UTC has no such edge. All-day is
the exception, because a day starts at midnight where you live and not in UTC, so
those carry the mailbox's own timezone - the string Graph itself reports, which
is therefore a string Graph accepts.

Recurring series are refused. Google's RRULE and Graph's patternedRecurrence are
different models, and a half-right translation of an every-other-week rule is
worse than an honest no.
"""

import json
from datetime import datetime, timezone
from urllib import parse
from urllib.error import HTTPError

from .auth import graph

# Named properties live under a GUID namespace of the caller's choosing. This one
# is arbitrary but fixed: change it and every mirror already on the calendar stops
# being findable.
NAMESPACE = "66f5a359-4659-4830-9070-00047ec6ac6e"
LINK = "String {%s} Name pkj-google-id" % NAMESPACE

TITLE = "Upptagen"


class MirrorError(Exception):
    pass


def _error(e):
    """Graph's own reason, which is in the body rather than the status line."""
    try:
        return json.loads(e.read() or b"{}").get("error", {}).get("message") or str(e)
    except Exception:
        return str(e)


def mailbox_timezone():
    """The zone the mailbox keeps its clock in, as Graph names it."""
    try:
        return (graph("GET", "/me/mailboxSettings") or {}).get("timeZone") or "UTC"
    except HTTPError as e:
        raise MirrorError("Kunde inte läsa postlådans tidszon: %s" % _error(e))


def _moment(block):
    """(text, zone, all_day) for one side of a Google event."""
    if block.get("dateTime"):
        when = datetime.fromisoformat(block["dateTime"].replace("Z", "+00:00"))
        return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "UTC", False
    if block.get("date"):
        return block["date"] + "T00:00:00", None, True
    raise MirrorError("Eventet har varken dateTime eller date.")


def body_for(ev, zone=None):
    """The Graph event body for a Google event. Raises on anything not mirrorable."""
    gid = (ev.get("id") or "").strip()
    if not gid:
        raise MirrorError("Eventet saknar id, så det går inte att länka.")
    if ev.get("recurrence") or ev.get("recurringEventId"):
        raise MirrorError(
            "Det här är en serie, och serier speglas inte: Googles RRULE och Graphs "
            "patternedRecurrence är olika modeller. Spegla de enskilda tillfällen "
            "du vill blockera, eller lägg serien för hand i jobbkalendern.")
    start, szone, all_day = _moment(ev.get("start") or {})
    end, ezone, _ = _moment(ev.get("end") or {})
    if all_day:
        szone = ezone = zone or mailbox_timezone()
    return {
        "subject": TITLE,
        "start": {"dateTime": start, "timeZone": szone},
        "end": {"dateTime": end, "timeZone": ezone},
        "isAllDay": all_day,
        "showAs": "busy",
        "sensitivity": "private",
        "isReminderOn": False,
        "responseRequested": False,
        # Same id, same event: this is what keeps a re-run from doubling the block.
        "transactionId": ("pkj-" + gid)[:256],
        "singleValueExtendedProperties": [{"id": LINK, "value": gid}],
    }


def find(google_id):
    """Every mirror of one Google event. A list, because a search can only say so."""
    flt = "singleValueExtendedProperties/any(ep:ep/id eq '%s' and ep/value eq '%s')" % (
        LINK, google_id.replace("'", "''"))
    try:
        got = graph("GET", "/me/events?$filter=" + parse.quote(flt, safe=""))
    except HTTPError as e:
        raise MirrorError("Sökningen misslyckades: %s" % _error(e))
    return (got or {}).get("value") or []


def mirror(ev, zone=None):
    """Put the block on the work calendar. Returns the Graph event."""
    try:
        return graph("POST", "/me/events", body=body_for(ev, zone))
    except HTTPError as e:
        raise MirrorError("Graph nekade bokningen: %s" % _error(e))


def unmirror(google_id):
    """Remove every block that belongs to one Google event. Returns how many."""
    gone = 0
    for ev in find(google_id):
        try:
            graph("DELETE", "/me/events/" + ev["id"])
        except HTTPError as e:
            raise MirrorError("Kunde inte ta bort %s: %s" % (ev["id"][:20], _error(e)))
        gone += 1
    return gone
