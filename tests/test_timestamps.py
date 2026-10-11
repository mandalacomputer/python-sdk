"""The shared RFC 3339 reader: the same answers on every supported Python."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mandala_computer._timestamps import parse_rfc3339

UTC = timezone.utc


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-10-09T01:00:00.123456789Z", datetime(2026, 10, 9, 1, 0, 0, 123456, UTC)),
        ("2026-10-09T01:00:00.5Z", datetime(2026, 10, 9, 1, 0, 0, 500000, UTC)),
        ("2026-10-09T01:00:00Z", datetime(2026, 10, 9, 1, 0, 0, 0, UTC)),
        (
            "2026-10-09T03:00:00.000000001+02:00",
            datetime(2026, 10, 9, 3, 0, 0, 0, timezone(timedelta(hours=2))),
        ),
    ],
)
def test_reads_platform_fractions(stamp: str, expected: datetime) -> None:
    assert parse_rfc3339(stamp) == expected


@pytest.mark.parametrize(
    "stamp,options",
    [
        ("2026-10-09T01:00:00.1234567891Z", {}),
        ("2026-10-09T01:00:00.Z", {}),
        ("2026-10-09T01:00:00", {}),
        ("2026-10-09 01:00:00Z", {}),
        ("2026-10-09T01:00:00+0000", {}),
        ("2026-13-09T01:00:00Z", {}),
        ("2026-10-09T01:00:00+02:00", {"utc_only": True}),
        (None, {}),
    ],
)
def test_refuses_malformed_stamps(stamp: object, options: dict[str, bool]) -> None:
    with pytest.raises(ValueError):
        parse_rfc3339(stamp, **options)


def test_fraction_bound_is_the_callers() -> None:
    long = "2026-10-09T01:00:00.1234567891234Z"
    assert parse_rfc3339(long, fraction_digits=None) == datetime(2026, 10, 9, 1, 0, 0, 123456, UTC)
