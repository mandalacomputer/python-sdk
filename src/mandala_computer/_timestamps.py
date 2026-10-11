"""RFC 3339 timestamps as the platform writes them, read alike on every Python."""

from __future__ import annotations

import re
from datetime import datetime

# A whole second, an optional fraction of any length (each caller bounds it),
# and `Z` or a numeric offset. Go writes up to nine fractional digits with
# trailing zeros trimmed, so one to nine is what the platform sends.
_RFC3339 = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})"
)


def parse_rfc3339(
    value: object, *, fraction_digits: int | None = 9, utc_only: bool = False
) -> datetime:
    """An aware datetime from an RFC 3339 timestamp; ``ValueError`` otherwise.

    Python 3.10's ``fromisoformat`` reads exactly three or six fractional
    digits and no ``Z``; 3.11 reads any. So the fraction is padded or cut to
    microseconds and the zone spelled as an offset before parsing, and every
    supported version reads the same stamps and refuses the same malformed
    ones. ``fraction_digits`` bounds the fraction (``None``: any length), and
    ``utc_only`` accepts only ``Z``.
    """
    match = _RFC3339.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("not an RFC 3339 timestamp")
    whole, fraction, zone = match.groups()
    if fraction is not None and fraction_digits is not None and len(fraction) > fraction_digits:
        raise ValueError("too many fractional digits")
    if utc_only and zone != "Z":
        raise ValueError("not a UTC timestamp")
    micros = "." + (fraction + "00000")[:6] if fraction else ""
    return datetime.fromisoformat(whole + micros + ("+00:00" if zone == "Z" else zone))
