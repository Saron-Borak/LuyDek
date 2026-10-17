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


def day_words(d: str | date | None, short: bool = False) -> str:
    """A date the way people say it: "5 Oct 2026"; with `short`, "5 Oct" when it's this year."""
    if not d:
        return ""
    d = date.fromisoformat(d[:10]) if isinstance(d, str) else d
    if short and d.year == today().year:
        return f"{d.day} {d:%b}"
    return f"{d.day} {d:%b %Y}"


def when_words(moment: str | datetime | None, cap: bool = False) -> str:
    """When something happened: "today 09:06", "yesterday 18:40", "5 Oct, 09:06"."""
    if not moment:
        return ""
    m = datetime.fromisoformat(moment) if isinstance(moment, str) else moment
    days = (today() - m.date()).days
    word = "today" if days == 0 else "yesterday" if days == 1 else None
    out = f"{word} {m:%H:%M}" if word else f"{day_words(m.date(), short=True)}, {m:%H:%M}"
    return out[0].upper() + out[1:] if cap else out
