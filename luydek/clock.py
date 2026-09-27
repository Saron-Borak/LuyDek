"""Shop-local wall clock.

Every timestamp in the database is naive local time ("2026-09-27 07:42:10"),
because the shop lives in one timezone and SQLite's date() then buckets sales
into the owner's days without conversion. Tests and the history simulator
freeze the clock with `freeze()`.
"""

from __future__ import annotations

from datetime import date, datetime

from .config import get_config

_frozen: datetime | None = None


def now() -> datetime:
    if _frozen is not None:
        return _frozen
    return datetime.now(get_config().tz).replace(tzinfo=None, microsecond=0)


def today() -> date:
    return now().date()


def freeze(moment: datetime | None) -> None:
    """Pin now() to `moment`; pass None to release."""
    global _frozen
    _frozen = moment


def stamp(moment: datetime | None = None) -> str:
    return (moment or now()).strftime("%Y-%m-%d %H:%M:%S")
