"""Local user input converted to explicit UTC instants for Graph."""

from datetime import datetime, timezone


class InputError(ValueError):
    """A user-correctable argument error, safe to print."""


def moment(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise InputError("Datum måste vara ISO-format, t.ex. 2026-10-22 eller 2026-10-22T08:00+02:00.") from None


def bounds(since=None, before=None):
    start, end = moment(since), moment(before)
    if start and end and start >= end:
        raise InputError("--since måste ligga före --before.")
    return start, end


def utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
