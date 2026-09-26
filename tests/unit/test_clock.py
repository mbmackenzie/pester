from datetime import UTC, datetime, timedelta, timezone

import pytest

from pester.core.clock import FakeClock, SystemClock


def test_system_clock_is_utc_aware() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_fake_clock_advances() -> None:
    clock = FakeClock(datetime(2026, 9, 25, 12, tzinfo=UTC))
    clock.advance(timedelta(hours=2))
    assert clock.now() == datetime(2026, 9, 25, 14, tzinfo=UTC)


def test_fake_clock_normalizes_to_utc() -> None:
    est = timezone(timedelta(hours=-5))
    clock = FakeClock(datetime(2026, 1, 1, 7, tzinfo=est))
    assert clock.now() == datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert clock.now().tzinfo is UTC


def test_fake_clock_rejects_naive_and_backwards() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 1, 1))
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        clock.set(clock.now() - timedelta(seconds=1))
