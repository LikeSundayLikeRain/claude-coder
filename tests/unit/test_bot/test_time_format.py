"""Tests for relative_time utility."""

from datetime import UTC, datetime, timedelta

import pytest

from src.bot.utils.time_format import relative_time


class TestRelativeTime:
    def test_just_now(self) -> None:
        dt = datetime.now(UTC) - timedelta(seconds=30)
        assert relative_time(dt) == "just now"

    def test_minutes_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(minutes=5)
        assert relative_time(dt) == "5 min ago"

    def test_one_minute_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(minutes=1, seconds=30)
        assert relative_time(dt) == "1 min ago"

    def test_hours_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=3)
        assert relative_time(dt) == "3 hours ago"

    def test_one_hour_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=1, minutes=10)
        assert relative_time(dt) == "1 hour ago"

    def test_days_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=3)
        assert relative_time(dt) == "3 days ago"

    def test_one_day_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=1)
        assert relative_time(dt) == "1 day ago"

    def test_weeks_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(weeks=2)
        assert relative_time(dt) == "2 weeks ago"

    def test_one_week_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(weeks=1)
        assert relative_time(dt) == "1 week ago"

    def test_months_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=90)
        assert relative_time(dt) == "3 months ago"

    def test_one_month_ago(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=35)
        assert relative_time(dt) == "1 month ago"

    def test_millisecond_timestamp(self) -> None:
        ms = int((datetime.now(UTC) - timedelta(hours=2)).timestamp() * 1000)
        assert relative_time(ms) == "2 hours ago"
