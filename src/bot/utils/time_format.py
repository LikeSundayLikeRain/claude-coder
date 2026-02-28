"""Relative time formatting for session picker display."""

from datetime import UTC, datetime, timedelta
from typing import Union


def relative_time(dt: Union[datetime, int]) -> str:
    """Format a datetime or millisecond timestamp as relative time.

    Args:
        dt: A timezone-aware datetime or millisecond epoch timestamp.

    Returns:
        Human-readable relative time string like '2 hours ago'.
    """
    if isinstance(dt, int):
        dt = datetime.fromtimestamp(dt / 1000.0, tz=UTC)

    now = datetime.now(UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return "just now"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"

    hours = minutes // 60
    if hours < 24:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit} ago"

    days = hours // 24
    if days < 7:
        unit = "day" if days == 1 else "days"
        return f"{days} {unit} ago"

    weeks = days // 7
    if days < 30:
        unit = "week" if weeks == 1 else "weeks"
        return f"{weeks} {unit} ago"

    months = days // 30
    unit = "month" if months == 1 else "months"
    return f"{months} {unit} ago"
