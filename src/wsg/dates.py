"""Local user input converted to explicit UTC instants for Graph."""

import re
from datetime import datetime, timedelta, timezone

# "30d" is thirty days ago, so --before 30d means older than thirty days and
# --since 7d means the last week. The absolute form still works unchanged.
AGO = re.compile(r"^(\d{1,5})\s*([hdw])$", re.IGNORECASE)
UNITS = {"h": "hours", "d": "days", "w": "weeks"}


class InputError(ValueError):
    """A user-correctable argument error, safe to print."""


def moment(value, now=None):
    if not value:
        return None
    text = value.strip()
    ago = AGO.match(text)
    if ago:
        amount, unit = int(ago.group(1)), ago.group(2).lower()
        return (now or datetime.now(timezone.utc)) - timedelta(**{UNITS[unit]: amount})
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise InputError("Datum måste vara ISO-format, t.ex. 2026-10-22 eller 2026-10-22T08:00+02:00, "
                         "eller en tid tillbaka: 36h, 30d, 2w.") from None


def bounds(since=None, before=None):
    start, end = moment(since), moment(before)
    if start and end and start >= end:
        raise InputError("--since måste ligga före --before.")
    return start, end


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
