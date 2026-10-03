"""Out of stock, the moment it happens, and the daily quick count that keeps the numbers true.

An alert goes out when a product runs out or runs low, once, until it is back in stock. Every day
a few products get a quick count ("is there really 4 left?"): products about to be reordered or
already out first, then the best sellers, never the same one twice in a week. A count that matches
is logged too; one that doesn't corrects the stock.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import clock
from . import stock


def new_stock_alerts(conn: sqlite3.Connection, expiry_warning_days: int = 30) -> list[dict]:
    """Products that ran out or ran low since the last alert (and forget the ones that recovered)."""
    sent = {r["product_id"]: r["state"] for r in conn.execute("SELECT * FROM stock_alerts")}
    fresh = []
    for r in stock.stock_table(conn, expiry_warning_days):
        state = None
        if r["tracked"] and r["on_hand"] <= 0:
            state = "out"
        elif r["tracked"] and r["low"]:
            state = "low"
        was = sent.get(r["id"])
        if state is None:
            if was is not None:
                conn.execute("DELETE FROM stock_alerts WHERE product_id = ?", (r["id"],))
            continue
        if was == state or (was == "out" and state == "low"):
            continue                    # already told; a little stock back after "out" isn't news
        conn.execute("INSERT INTO stock_alerts (product_id, state, sent_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(product_id) DO UPDATE SET state = excluded.state, sent_at = excluded.sent_at",
                     (r["id"], state, clock.stamp()))
        fresh.append({**r, "alert": state})
    order = {"out": 0, "low": 1}
    return sorted(fresh, key=lambda r: (order[r["alert"]], not r["is_critical"], r["name_en"]))


def spot_checks(conn: sqlite3.Connection, n: int = 3, rest_days: int = 7, expiry_warning_days: int = 30) -> list[dict]:
    """The products worth a quick count today.

    Counted stock only (a product never counted is set up first), nothing counted in the last
    `rest_days`, and nothing with sales still waiting to be checked (the shelf and the ledger
    disagree until they are). Below zero, out and low first, since those decide orders; then
    the best sellers, whose small errors add up fastest.
    """
    since = clock.stamp(clock.now() - timedelta(days=rest_days))
    recent = {pid for pid, at in stock.last_counted(conn).items() if at >= since}
    rows = [r for r in stock.stock_table(conn, expiry_warning_days)
            if r["tracked"] and r["id"] not in recent and not r["pending_units"]]
    rows.sort(key=lambda r: (not r["negative"], not r["out"], not r["low"], -r["sold_28d"], r["name_en"]))
    return rows[:n]
