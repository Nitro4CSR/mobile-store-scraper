"""Turn every date format the stores use into timezone-aware UTC datetimes."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

__all__ = ["from_timestamp", "from_iso", "from_play_api_date", "from_js_date", "to_iso", "parse_any"]

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def from_timestamp(value: Any) -> datetime | None:
    """Unix seconds (or milliseconds) -> aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 1e11:  # milliseconds
        ts /= 1000.0
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def from_iso(value: Any) -> datetime | None:
    """ISO-8601 string (``2026-10-22T15:42:36Z`` or ``2026-10-22``) -> aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def from_play_api_date(value: Any) -> datetime | None:
    """Play Android API ``infoUpdatedOn`` (``"Oct 23, 2026"`` with an en_US session) -> UTC midnight."""
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"\s*([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", value)
    if m and m.group(1).lower() in _MONTHS:
        return datetime(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)), tzinfo=timezone.utc)
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})", value)  # "23 Oct 2026"
    if m and m.group(2).lower() in _MONTHS:
        return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)), tzinfo=timezone.utc)
    return from_iso(value)


def from_js_date(value: Any) -> datetime | None:
    """JavaScript ``Date.toString()`` such as ``Thu Oct 22 2026 15:42:36 GMT+0000 (...)``."""
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"\s*\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})\s+(\d{2}):(\d{2}):(\d{2})\s+GMT([+-]\d{4})", value)
    if not m or m.group(1).lower() not in _MONTHS:
        return from_iso(value)
    off = m.group(7)
    iso = (f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}T"
           f"{m.group(4)}:{m.group(5)}:{m.group(6)}{off[:3]}:{off[3:]}")
    return from_iso(iso)


def parse_any(value: Any) -> datetime | None:
    """Best effort for user input: datetime, timestamp, ISO string or store string."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return from_timestamp(value)
    return from_iso(value) or from_play_api_date(value) or from_js_date(value)


def to_iso(dt: datetime | None) -> str | None:
    """Aware datetime -> ``YYYY-MM-DDTHH:MM:SSZ`` (the unified string format)."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
