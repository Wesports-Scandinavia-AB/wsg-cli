"""
Mirror a private calendar booking onto the work calendar over Graph.

The point is to block the time, not to tell the office what it is: the mirrored
booking is called "Upptagen" and carries no description, no location and no
guests. A title from a private calendar never reaches a calendar colleagues can
open.

**Travel.** A booking can be flanked by half an hour of "Restid" either side, so
the trip there and back is not bookable either. Those blocks say only that word,
carry the same link as the booking, and are therefore cleared by the same
`unmirror`. All-day events do not get them.

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
from datetime import datetime, timedelta, timezone
from urllib import parse
from urllib.error import HTTPError

from .auth import graph

# Named properties live under a GUID namespace of the caller's choosing. This one
# is arbitrary but fixed: change it and every mirror already on the calendar stops
# being findable.
NAMESPACE = "66f5a359-4659-4830-9070-00047ec6ac6e"
LINK = "String {%s} Name pkj-google-id" % NAMESPACE

TITLE = "Upptagen"
TRAVEL_TITLE = "Restid"
TRAVEL_DEFAULT = 30


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


def _instant(block):
    """One side of a Google event as an aware UTC moment. None if all-day.

    Travel time is arithmetic on a real instant, so it needs the datetime that
    _moment throws away once it has formatted it.
    """
    if block.get("dateTime"):
        when = datetime.fromisoformat(block["dateTime"].replace("Z", "+00:00"))
        return when.astimezone(timezone.utc)
    return None


def _timed_block(gid, subject, start, end, suffix):
    """A fixed-time block on the work calendar, linked to a Google event."""
    # The suffix is what keeps the three transaction ids apart, so it has to
    # survive the length cap: trim the id, never the suffix.
    tx = ("pkj-" + gid)[:256 - len(suffix)] + suffix
    return {
        "subject": subject,
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
        "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"},
        "isAllDay": False,
        "showAs": "busy",
        "sensitivity": "private",
        "isReminderOn": False,
        "responseRequested": False,
        "transactionId": tx,
        # The same link as the booking itself, so find() and unmirror() treat
        # the travel as part of it rather than as strays nobody owns.
        "singleValueExtendedProperties": [{"id": LINK, "value": gid}],
    }


def travel_bodies_for(ev, minutes=TRAVEL_DEFAULT):
    """The Restid blocks either side of a booking, earliest first.

    Empty for an all-day event: there is no journey to midnight, and half an
    hour before one only takes a bite out of the evening before.
    """
    if minutes <= 0:
        return []
    gid = (ev.get("id") or "").strip()
    start = _instant(ev.get("start") or {})
    end = _instant(ev.get("end") or {})
    if start is None or end is None:
        return []
    gap = timedelta(minutes=minutes)
    return [
        _timed_block(gid, TRAVEL_TITLE, start - gap, start, "-pre"),
        _timed_block(gid, TRAVEL_TITLE, end, end + gap, "-post"),
    ]


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


def bodies_for(ev, zone=None, travel=0):
    """Every Graph body for one Google event, in time order.

    The booking itself always; a Restid block either side of it when asked for
    one. body_for is what validates, so it runs whatever the travel setting is.
    """
    main = body_for(ev, zone)
    either_side = travel_bodies_for(ev, travel)
    if not either_side:
        return [main]
    before, after = either_side
    return [before, main, after]


def mirror(ev, zone=None, travel=0):
    """Put the blocks on the work calendar. Returns the Graph events, in time order.

    A POST that fails partway leaves what it already booked: every block carries
    the link, so `unmirror` clears the lot, and the error says how many are out
    there rather than letting them sit unmentioned.
    """
    made = []
    for body in bodies_for(ev, zone, travel):
        try:
            made.append(graph("POST", "/me/events", body=body))
        except HTTPError as e:
            extra = ("" if not made else
                     " %d block ligger redan på kalendern - ta bort dem med "
                     "wsg calendar unmirror." % len(made))
            raise MirrorError("Graph nekade bokningen: %s.%s" % (_error(e), extra))
    return made


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
