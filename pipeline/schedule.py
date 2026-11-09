"""When a company's roundup runs again."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

TIMEZONES = (
    ("America/Chicago", "Central"),
    ("America/New_York", "Eastern"),
    ("America/Denver", "Mountain"),
    ("America/Los_Angeles", "Pacific"),
    ("UTC", "UTC"),
)

DEFAULT_TIMEZONE = "America/Chicago"


def timezone_label(name: str) -> str:
    for key, label in TIMEZONES:
        if key == name:
            return label
    return name


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def stamp(when: datetime) -> str:
    """UTC timestamp that sorts correctly as text."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_local(when: datetime, tz_name: str) -> str:
    local = when.astimezone(zone(tz_name))
    hour = local.strftime("%I").lstrip("0") or "12"
    return local.strftime(f"%a, %b {local.day}, %Y · {hour}:%M %p ") + timezone_label(tz_name)


def next_run_at(
    *,
    cadence: str,
    hour: int,
    minute: int,
    weekday: int | None,
    interval_days: int,
    tz_name: str,
    after: datetime,
) -> datetime:
    """Next automatic run, strictly after `after`.

    Daily and weekly use the next clock time. Custom means every N days at
    that clock time, counting from the day the schedule is saved. The button
    that saves the schedule also sends a roundup immediately; this is the
    run after that one.
    """
    if cadence not in {"daily", "weekly", "custom"}:
        raise ValueError(f"Unknown cadence {cadence!r}")
    clock = zone(tz_name)
    local = after.astimezone(clock)
    if cadence == "daily":
        upcoming = _at(local.date(), hour, minute, clock)
        if upcoming <= local:
            upcoming = _at(local.date() + timedelta(days=1), hour, minute, clock)
    elif cadence == "weekly":
        want = 0 if weekday is None else int(weekday) % 7
        days_ahead = (want - local.weekday()) % 7
        day = local.date() + timedelta(days=days_ahead)
        upcoming = _at(day, hour, minute, clock)
        if upcoming <= local:
            upcoming = _at(day + timedelta(days=7), hour, minute, clock)
    else:
        span = max(1, int(interval_days))
        upcoming = _at(local.date() + timedelta(days=span), hour, minute, clock)
        if upcoming <= local:
            upcoming = upcoming + timedelta(days=span)
    return upcoming.astimezone(timezone.utc)


def describe_cadence(
    *,
    cadence: str,
    hour: int,
    minute: int,
    weekday: int,
    interval_days: int,
    tz_name: str,
) -> str:
    clock = f"{hour}:{minute:02d} {timezone_label(tz_name)}"
    if cadence == "daily":
        return f"Every day at {clock}"
    if cadence == "weekly":
        day = WEEKDAYS[weekday % 7]
        return f"Every {day} at {clock}"
    if cadence == "custom":
        span = max(1, interval_days)
        unit = "day" if span == 1 else "days"
        return f"Every {span} {unit} at {clock}"
    return "Only when someone presses Run now"


def _at(day, hour: int, minute: int, clock: ZoneInfo) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=clock)
