"""Pilot health numbers, each one grouped SQL.

- Taps per day: verification fatigue. More than ~10 means something needs fixing.
- Recognition accuracy: of the items that were settled, how many did the camera
  get right on its own (auto-accepted, or confirmed as guessed).
- Follow-rate: whether deliveries matched the recommendation (proves causation).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .. import clock


def taps_per_day(conn: sqlite3.Connection, days: int = 7) -> float:
    since = clock.stamp(clock.now() - timedelta(days=days))
    n = conn.execute("SELECT COUNT(*) FROM taps WHERE created_at >= ?", (since,)).fetchone()[0]
    return round(n / days, 1)


def log_tap(conn: sqlite3.Connection, chat_id: int | None, action: str) -> None:
    conn.execute("INSERT INTO taps (chat_id, action, created_at) VALUES (?, ?, ?)", (chat_id, action, clock.stamp()))


def accuracy(conn: sqlite3.Connection, start: date, end: date) -> dict:
    row = conn.execute(
        """
        SELECT SUM(ti.status = 'auto')      AS auto,
               SUM(ti.status = 'confirmed') AS confirmed,
               SUM(ti.status = 'corrected') AS corrected,
               SUM(ti.status = 'removed')   AS removed,
               SUM(ti.status = 'best_guess') AS best_guess,
               SUM(ti.status = 'pending')   AS pending,
               COUNT(ti.id)                 AS items,
               COUNT(DISTINCT t.id)         AS txns
        FROM transactions t LEFT JOIN transaction_items ti ON ti.txn_id = t.id
        WHERE t.started_at >= ? AND t.started_at < ? AND t.status != 'open'
        """,
        (f"{start} 00:00:00", f"{end} 00:00:00"),
    ).fetchone()
    stats = {k: row[k] or 0 for k in row.keys()}
    right = stats["auto"] + stats["confirmed"]
    settled = right + stats["corrected"] + stats["removed"]
    stats["accuracy"] = round(100 * right / settled) if settled else None
    stats["auto_rate"] = round(100 * stats["auto"] / stats["items"]) if stats["items"] else None
    return stats


def weekly_trend(conn: sqlite3.Connection, weeks: int = 4) -> list[dict]:
    today = clock.today()
    out = []
    for k in range(weeks - 1, -1, -1):
        end = today + timedelta(days=1) - timedelta(days=7 * k)
        start = end - timedelta(days=7)
        stats = accuracy(conn, start, end)
        out.append({"week_end": (end - timedelta(days=1)).isoformat(), **stats})
    return out


def window_stats(conn: sqlite3.Connection, since: str) -> dict:
    """Header numbers for a digest: sales since `since` and how they were settled."""
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT t.id) AS txns,
               SUM(ti.status = 'auto') AS auto,
               SUM(ti.status = 'pending') AS pending
        FROM transactions t LEFT JOIN transaction_items ti ON ti.txn_id = t.id
        WHERE t.started_at >= ? AND t.status != 'open'
        """,
        (since,),
    ).fetchone()
    return {"txns": row["txns"] or 0, "auto": row["auto"] or 0, "pending": row["pending"] or 0}


def follow_rate(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(followed) AS settled, SUM(followed) AS followed FROM forecasts WHERE followed IS NOT NULL"
    ).fetchone()
    settled = row["settled"] or 0
    return {"settled": settled, "rate": round(100 * (row["followed"] or 0) / settled) if settled else None}


def today_counts(conn: sqlite3.Connection) -> dict:
    today = clock.today()
    return accuracy(conn, today, today + timedelta(days=1))


def busy_hours(conn: sqlite3.Connection, days: int = 28) -> dict:
    """Sales by hour of day over the last `days` days.

    Averaged over the days the camera actually ran, so a week with the laptop
    off doesn't make every hour look quiet.
    """
    since = clock.stamp(clock.now() - timedelta(days=days))
    where = "WHERE started_at >= ? AND status != 'open'"
    counts = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT CAST(substr(started_at, 12, 2) AS INTEGER) AS hr, COUNT(*) FROM transactions {where} GROUP BY hr",
            (since,),
        )
    }
    ran = conn.execute(f"SELECT COUNT(DISTINCT date(started_at)) FROM transactions {where}", (since,)).fetchone()[0]
    return {"counts": counts, "days": ran, "total": sum(counts.values()),
            "peak": max(counts, key=counts.get) if counts else None}
