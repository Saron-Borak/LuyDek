"""Counter transactions: capture -> close -> verify -> stock.

A transaction is everything that lands on the counter between two moments the
counter is empty. Each still frame is recognized on its own; closing the
transaction keeps, for every product, the frame where the most of it were
visible. Items keep piling up while earlier ones are already bagged, so the
maximum is right where a sum would double-count.

High-confidence items become sales immediately. Everything else waits for the
owner (Telegram digest or the dashboard queue, both through `resolve`) or for
the end-of-day job that accepts the best guess.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .. import clock
from . import stock

# (conn, crop_path, pack_id or None, ignore_label or None) -> None
Learner = Callable[[sqlite3.Connection, str, int | None, str | None], None]

RESOLVED = ("confirmed", "corrected", "best_guess", "removed", "unknown")


@dataclass
class DetectionRecord:
    pack_id: int | None
    confidence: float
    tier: str                      # high / medium / low
    method: str                    # barcode / image / multipack
    bbox: tuple[int, int, int, int]
    crop_path: str | None = None   # relative to the media dir
    candidates: list[tuple[int, float]] = field(default_factory=list)


# --- capture -----------------------------------------------------------------

def open_transaction(conn: sqlite3.Connection, source: str = "camera") -> int:
    cur = conn.execute(
        "INSERT INTO transactions (started_at, source, status) VALUES (?, ?, 'open')", (clock.stamp(), source)
    )
    return cur.lastrowid


def add_frame(conn: sqlite3.Connection, txn_id: int, photo_path: str | None, detections: list[DetectionRecord],
              media_dir: Path | None = None) -> int:
    frame_no = conn.execute(
        "SELECT COALESCE(MAX(frame_no), 0) + 1 FROM frame_detections WHERE txn_id = ?", (txn_id,)
    ).fetchone()[0]
    for d in detections:
        conn.execute(
            """
            INSERT INTO frame_detections (txn_id, frame_no, pack_id, confidence, tier, method, bbox,
                                          crop_path, candidates_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, frame_no, d.pack_id, d.confidence, d.tier, d.method, json.dumps(list(d.bbox)),
             d.crop_path, json.dumps(d.candidates)),
        )
    if photo_path:
        # Only the latest frame is kept; it is the most complete view of the sale.
        old = conn.execute("SELECT photo_path FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if old and old["photo_path"] and media_dir and old["photo_path"] != photo_path:
            _unlink(media_dir, old["photo_path"])
        conn.execute("UPDATE transactions SET photo_path = ? WHERE id = ?", (photo_path, txn_id))
    return frame_no


def close_transaction(conn: sqlite3.Connection, txn_id: int, media_dir: Path | None = None,
                      delete_photo: bool = False) -> dict:
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if txn is None or txn["status"] != "open":
        return {"txn_id": txn_id, "items": 0, "auto": 0, "pending": 0, "closed": False}

    detections = conn.execute(
        "SELECT * FROM frame_detections WHERE txn_id = ? ORDER BY frame_no, id", (txn_id,)
    ).fetchall()
    chosen = _merge_frames(detections)
    if not chosen:
        # Nothing but a hand or a phone ever sat on the counter.
        if media_dir:
            for d in detections:
                _unlink(media_dir, d["crop_path"])
            _unlink(media_dir, txn["photo_path"])
        conn.execute("DELETE FROM frame_detections WHERE txn_id = ?", (txn_id,))
        conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
        return {"txn_id": txn_id, "items": 0, "auto": 0, "pending": 0, "closed": True, "empty": True}

    kept = {d["id"] for d in chosen}
    auto = pending = 0
    for d in chosen:
        status = "auto" if d["tier"] == "high" else "pending"
        cur = conn.execute(
            """
            INSERT INTO transaction_items (txn_id, pack_id, guess_pack_id, count, confidence, tier, method,
                                           bbox, crop_path, candidates_json, status)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, d["pack_id"], d["pack_id"], d["confidence"], d["tier"], d["method"], d["bbox"],
             d["crop_path"], d["candidates_json"], status),
        )
        if status == "auto":
            _write_sale(conn, cur.lastrowid, d["pack_id"], 1, txn["started_at"])
            auto += 1
        else:
            pending += 1
    if media_dir:
        for d in detections:
            if d["id"] not in kept:
                _unlink(media_dir, d["crop_path"])
    conn.execute("DELETE FROM frame_detections WHERE txn_id = ?", (txn_id,))
    conn.execute(
        "UPDATE transactions SET closed_at = ?, status = ? WHERE id = ?",
        (clock.stamp(), "pending" if pending else "auto", txn_id),
    )
    _maybe_finish(conn, txn_id, media_dir, delete_photo)
    return {"txn_id": txn_id, "items": auto + pending, "auto": auto, "pending": pending, "closed": True}


def _merge_frames(detections: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """For each pack, the detections from the frame where it was seen most often.

    Ties go to the later frame: the scene is more settled and the crops better.
    """
    by_frame: dict[int, dict[int | None, list[sqlite3.Row]]] = {}
    for d in detections:
        by_frame.setdefault(d["frame_no"], {}).setdefault(d["pack_id"], []).append(d)
    best: dict[int | None, list[sqlite3.Row]] = {}
    for frame_no in sorted(by_frame):
        for pack_id, items in by_frame[frame_no].items():
            if len(items) >= len(best.get(pack_id, [])):
                best[pack_id] = items
    return [d for items in best.values() for d in items]


def close_stale(conn: sqlite3.Connection, minutes: int, media_dir: Path | None = None,
                delete_photo: bool = False) -> int:
    """Close transactions whose counter page vanished before the counter cleared."""
    cutoff = clock.stamp(clock.now() - timedelta(minutes=minutes))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE status = 'open' AND started_at < ?", (cutoff,)
    )]
    for txn_id in ids:
        close_transaction(conn, txn_id, media_dir, delete_photo)
    return len(ids)


# --- verification --------------------------------------------------------------

def item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(_ITEM_SQL + " WHERE ti.id = ?", (item_id,)).fetchone()


def pending_items(conn: sqlite3.Connection, unsent_only: bool = False) -> list[sqlite3.Row]:
    sql = _ITEM_SQL + " WHERE ti.status = 'pending'"
    if unsent_only:
        sql += " AND ti.digest_id IS NULL"
    return conn.execute(sql + " ORDER BY t.started_at, ti.id").fetchall()


def candidates(row: sqlite3.Row) -> list[tuple[int, float]]:
    return [(int(p), float(s)) for p, s in json.loads(row["candidates_json"] or "[]")]


_ITEM_SQL = """
    SELECT ti.*, t.started_at, t.photo_path,
           pk.units, pk.label, pk.product_id,
           p.name_en, p.name_km, p.sku
    FROM transaction_items ti
    JOIN transactions t ON t.id = ti.txn_id
    LEFT JOIN product_packs pk ON pk.id = ti.pack_id
    LEFT JOIN products p ON p.id = pk.product_id
"""


def resolve(
    conn: sqlite3.Connection,
    item_id: int,
    action: str,
    *,
    pack_id: int | None = None,
    delta: int = 0,
    by: str = "dashboard",
    learn: Learner | None = None,
    media_dir: Path | None = None,
    delete_photo: bool = False,
) -> dict:
    """Apply one owner decision to a pending item.

    Actions: yes (the guess is right), pick (it is `pack_id`), qty (count +/- `delta`,
    stays pending), remove (not a product sold), unknown (a product not in the catalog).
    Returns {"status": ..., "already": bool}.
    """
    row = item(conn, item_id)
    if row is None:
        raise KeyError(item_id)
    if row["status"] != "pending":
        return {"status": row["status"], "already": True}

    if action == "qty":
        count = max(1, row["count"] + delta)
        conn.execute("UPDATE transaction_items SET count = ? WHERE id = ?", (count, item_id))
        return {"status": "pending", "already": False, "count": count}

    now = clock.stamp()
    if action == "yes":
        if row["pack_id"] is None:
            raise ValueError("nothing was guessed for this item; pick a product instead")
        status, final_pack = "confirmed", row["pack_id"]
    elif action == "pick":
        if pack_id is None:
            raise ValueError("pick needs a pack_id")
        status = "confirmed" if pack_id == row["pack_id"] else "corrected"
        final_pack = pack_id
    elif action == "remove":
        status, final_pack = "removed", None
    elif action == "unknown":
        status, final_pack = "unknown", None
    else:
        raise ValueError(f"unknown action {action!r}")

    conn.execute(
        "UPDATE transaction_items SET status = ?, pack_id = COALESCE(?, pack_id), verified_by = ?, verified_at = ? "
        "WHERE id = ?",
        (status, final_pack, by, now, item_id),
    )
    if final_pack is not None:
        _write_sale(conn, item_id, final_pack, row["count"], row["started_at"])

    # Every correction becomes a new example in this store's gallery.
    if learn and row["crop_path"]:
        if status == "corrected":
            learn(conn, row["crop_path"], final_pack, None)
        elif status == "removed":
            learn(conn, row["crop_path"], None, "other")

    _maybe_finish(conn, row["txn_id"], media_dir, delete_photo)
    return {"status": status, "already": False, "count": row["count"]}


def settle_by_label(conn: sqlite3.Connection, item_id: int, pack_id: int, label_text: str,
                    media_dir: Path | None = None, delete_photo: bool = False) -> bool:
    """The label reader decided between look-alikes: record the sale without asking the owner.

    It counts as automatic (method "label"), not as an owner check, so accuracy
    figures keep meaning "the owner looked at it".
    """
    row = item(conn, item_id)
    if row is None or row["status"] != "pending":
        return False
    conn.execute(
        "UPDATE transaction_items SET status = 'auto', method = 'label', pack_id = ?, label_text = ?, "
        "label_checked_at = ?, verified_by = 'label reader', verified_at = ? WHERE id = ?",
        (pack_id, label_text, clock.stamp(), clock.stamp(), item_id),
    )
    _write_sale(conn, item_id, pack_id, row["count"], row["started_at"])
    _maybe_finish(conn, row["txn_id"], media_dir, delete_photo)
    return True


def confirm_many(conn: sqlite3.Connection, item_ids: list[int], by: str, **kwargs) -> int:
    done = 0
    for item_id in item_ids:
        row = item(conn, item_id)
        if row is not None and row["status"] == "pending" and row["pack_id"] is not None:
            resolve(conn, item_id, "yes", by=by, **kwargs)
            done += 1
    return done


def best_guess_all(conn: sqlite3.Connection, media_dir: Path | None = None, delete_photo: bool = False) -> list[int]:
    """End of day: accept every unchecked guess and flag it for the weekly review."""
    ids = []
    for row in pending_items(conn):
        if row["pack_id"] is None:
            status = "unknown"
        else:
            status = "best_guess"
            _write_sale(conn, row["id"], row["pack_id"], row["count"], row["started_at"])
        conn.execute(
            "UPDATE transaction_items SET status = ?, verified_by = 'end_of_day', verified_at = ? WHERE id = ?",
            (status, clock.stamp(), row["id"]),
        )
        _maybe_finish(conn, row["txn_id"], media_dir, delete_photo)
        ids.append(row["id"])
    return ids


def mark_sent(conn: sqlite3.Connection, item_ids: list[int], digest_id: int) -> None:
    conn.executemany("UPDATE transaction_items SET digest_id = ? WHERE id = ?", [(digest_id, i) for i in item_ids])


def recent(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    txns = conn.execute(
        "SELECT * FROM transactions WHERE status != 'open' ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    if not txns:
        return []
    ids = [t["id"] for t in txns]
    items = conn.execute(
        _ITEM_SQL + f" WHERE ti.txn_id IN ({','.join('?' * len(ids))}) ORDER BY ti.id", ids
    ).fetchall()
    grouped: dict[int, list] = {}
    for it in items:
        grouped.setdefault(it["txn_id"], []).append(it)
    return [{"txn": t, "items": grouped.get(t["id"], [])} for t in txns]


# --- internals -------------------------------------------------------------------

def _write_sale(conn: sqlite3.Connection, item_id: int, pack_id: int, count: int, sold_at: str) -> None:
    pk = conn.execute("SELECT product_id, units FROM product_packs WHERE id = ?", (pack_id,)).fetchone()
    # Dated at the sale, not at the check: a sale confirmed next morning still
    # belongs to the day the customer bought it.
    stock.record(conn, pk["product_id"], -count * pk["units"], "sale", ref_type="transaction_item",
                 ref_id=item_id, at=datetime.strptime(sold_at, "%Y-%m-%d %H:%M:%S"))


def _maybe_finish(conn: sqlite3.Connection, txn_id: int, media_dir: Path | None, delete_photo: bool) -> None:
    counts = conn.execute(
        """
        SELECT SUM(status = 'pending') AS pending, SUM(status = 'best_guess') AS guessed,
               SUM(status IN ('confirmed', 'corrected', 'removed', 'unknown')) AS checked
        FROM transaction_items WHERE txn_id = ?
        """,
        (txn_id,),
    ).fetchone()
    if counts["pending"]:
        return
    status = "best_guess" if counts["guessed"] else "verified" if counts["checked"] else "auto"
    conn.execute("UPDATE transactions SET status = ? WHERE id = ? AND status != 'open'", (status, txn_id))
    if delete_photo and media_dir:
        # Privacy: once settled, keep only the product crops, never the counter photo.
        photo = conn.execute("SELECT photo_path FROM transactions WHERE id = ?", (txn_id,)).fetchone()["photo_path"]
        if photo:
            _unlink(media_dir, photo)
            conn.execute("UPDATE transactions SET photo_path = NULL WHERE id = ?", (txn_id,))


def _unlink(media_dir: Path, relative: str | None) -> None:
    if relative:
        (media_dir / relative).unlink(missing_ok=True)
