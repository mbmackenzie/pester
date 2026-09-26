import re
from datetime import timedelta

_PART = re.compile(r"(\d+)\s*([mhd]?)")
_UNITS = {"": 60, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta | None:
    """Parse durations like ``30m``, ``2h``, ``1h30m``, ``1d``, or ``45`` (minutes). None if invalid."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    seconds = 0
    position = 0
    for match in _PART.finditer(text):
        if match.start() != position:
            return None
        seconds += int(match.group(1)) * _UNITS[match.group(2)]
        position = match.end()
    if position != len(text) or seconds <= 0:
        return None
    return timedelta(seconds=seconds)
