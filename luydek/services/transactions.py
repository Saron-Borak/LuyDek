"""Counter transactions: capture -> close -> verify -> stock.

A transaction is everything that lands on the counter between two moments the
counter is empty. Each still frame is recognized on its own; closing the
transaction keeps, for every product, the frame where the most of it were
visible. Items keep piling up while earlier ones are already bagged, so the
maximum is right where a sum would double-count.

High-confidence items become sales immediately. Everything else waits for the
owner (Telegram digest or the dashboard queue, both through `resolve`) or for
the end-of-day job that accepts the best guess.

Pharmacies: tablets sold loose lie on the dispensing tray, and the box they
came from lies beside it. At close, the tray becomes one item counted in
tablets of that medicine, and the box is taken as the source, not a sale.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .. import clock
from ..config import TrayCfg
from . import catalog, loose, stock

# (conn, crop_path, pack_id or None, ignore_label or None, source) -> None
Learner = Callable[[sqlite3.Connection, str, int | None, str | None, str], None]

RESOLVED = ("confirmed", "corrected", "best_guess", "removed", "unknown")
TIER_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class DetectionRecord:
    pack_id: int | None
    confidence: float
    tier: str                      # high / medium / low
    method: str                    # barcode / image / multipack / colour (look-alike settled by colour)
    bbox: tuple[int, int, int, int]
    crop_path: str | None = None   # relative to the media dir
    candidates: list[tuple[int, float]] = field(default_factory=list)
    hint: str | None = None        # why it is asked: "junk" (looked like clutter) / "background" (like the mat)


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
                                          crop_path, candidates_json, hint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, frame_no, d.pack_id, d.confidence, d.tier, d.method, json.dumps(list(d.bbox)),
             d.crop_path, json.dumps(d.candidates), d.hint),
        )
    if photo_path:
        # Only the latest frame is kept; it is the most complete view of the sale.
        old = conn.execute("SELECT photo_path FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if old and old["photo_path"] and media_dir and old["photo_path"] != photo_path:
            _unlink(media_dir, old["photo_path"])
        conn.execute("UPDATE transactions SET photo_path = ? WHERE id = ?", (photo_path, txn_id))
    return frame_no


def add_tray(conn: sqlite3.Connection, txn_id: int, frame_no: int, areas: list[float], oversized: bool,
             crop_path: str | None) -> None:
    """What lay on the tablet tray in one still frame."""
    conn.execute(
        "INSERT INTO tray_readings (txn_id, frame_no, pieces_json, crop_path) VALUES (?, ?, ?, ?)",
        (txn_id, frame_no, json.dumps({"areas": areas, "oversized": oversized}), crop_path),
    )


def close_transaction(conn: sqlite3.Connection, txn_id: int, media_dir: Path | None = None,
                      delete_photo: bool = False, tray: TrayCfg | None = None) -> dict:
    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if txn is None or txn["status"] != "open":
        return {"txn_id": txn_id, "items": 0, "auto": 0, "pending": 0, "closed": False}

    detections = conn.execute(
        "SELECT * FROM frame_detections WHERE txn_id = ? ORDER BY frame_no, id", (txn_id,)
    ).fetchall()
    trays = conn.execute("SELECT * FROM tray_readings WHERE txn_id = ? ORDER BY frame_no, id", (txn_id,)).fetchall()
    chosen = _merge_frames(detections)
    best_tray = _best_tray(trays)
    if not chosen and best_tray is None:
        # Nothing but a hand or a phone ever sat on the counter.
        if media_dir:
            for d in detections:
                _unlink(media_dir, d["crop_path"])
            for t in trays:
                _unlink(media_dir, t["crop_path"])
            _unlink(media_dir, txn["photo_path"])
        conn.execute("DELETE FROM frame_detections WHERE txn_id = ?", (txn_id,))
        conn.execute("DELETE FROM tray_readings WHERE txn_id = ?", (txn_id,))
        conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
        return {"txn_id": txn_id, "items": 0, "auto": 0, "pending": 0, "closed": True, "empty": True}

    rows = [dict(d) for d in chosen]
    if best_tray is not None:
        rows = _with_tray(conn, txn_id, rows, best_tray, media_dir, tray or TrayCfg())
    kept = {d["id"] for d in rows}
    auto = pending = 0
    for d in rows:
        status = "auto" if d["tier"] == "high" else "pending"
        count = d.get("count", 1)
        cur = conn.execute(
            """
            INSERT INTO transaction_items (txn_id, pack_id, guess_pack_id, count, confidence, tier, method,
                                           bbox, crop_path, candidates_json, status, tray_json, hint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, d["pack_id"], d["pack_id"], count, d["confidence"], d["tier"], d["method"], d["bbox"],
             d["crop_path"], d["candidates_json"], status, d.get("tray_json"), d.get("hint")),
        )
        if status == "auto":
            _write_sale(conn, cur.lastrowid, d["pack_id"], count, txn["started_at"])
            auto += 1
        else:
            pending += 1
    if media_dir:
        for d in detections:
            if d["id"] not in kept:
                _unlink(media_dir, d["crop_path"])
        for t in trays:
            if best_tray is None or t["id"] != best_tray["id"]:
                _unlink(media_dir, t["crop_path"])
    conn.execute("DELETE FROM frame_detections WHERE txn_id = ?", (txn_id,))
    conn.execute("DELETE FROM tray_readings WHERE txn_id = ?", (txn_id,))
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


def _best_tray(trays: list[sqlite3.Row]) -> sqlite3.Row | None:
    """The frame with the most on the tray (a hand over it disqualifies a frame); ties go to the later one."""
    best, best_key = None, None
    for t in trays:
        info = json.loads(t["pieces_json"])
        if not info["areas"]:
            continue
        key = (not info.get("oversized"), len(info["areas"]), sum(info["areas"]))
        if best_key is None or key >= best_key:
            best, best_key = t, key
    return best


def _rank(d: dict) -> tuple[int, float]:
    return TIER_RANK.get(d["tier"], 0), d["confidence"]


def _with_tray(conn: sqlite3.Connection, txn_id: int, rows: list[dict], tray_row: sqlite3.Row,
               media_dir: Path | None, cfg: TrayCfg) -> list[dict]:
    """Turn the tray into one item counted in tablets; the loose-sold box beside it is its source.

    One loose-sold medicine on the mat: that is what the tablets are, as sure as
    the box was recognised. Several: the most likely one, for the owner to check.
    None (the box was forgotten): the owner picks from the loose-sold medicines.
    """
    info = json.loads(tray_row["pieces_json"])
    areas = info["areas"]
    sources: dict[int, dict] = {}
    rest = []
    for d in rows:
        pk = catalog.pack(conn, d["pack_id"]) if d["pack_id"] is not None else None
        if pk is not None and loose.is_loose(conn, pk["product_id"]):
            best = sources.get(pk["product_id"])
            if best is None or _rank(d) > _rank(best):
                sources[pk["product_id"]] = d  # every box of a medicine being dispensed is a source
        else:
            rest.append(d)

    if len(sources) == 1:
        product_id, source = next(iter(sources.items()))
        product_sure = source["tier"] == "high"
        others = []
        for p, _ in json.loads(source["candidates_json"] or "[]"):
            pk = catalog.pack(conn, p)
            if pk is not None and pk["product_id"] != product_id and loose.is_loose(conn, pk["product_id"]):
                others.append(pk["product_id"])
    elif sources:
        product_id, source = max(sources.items(), key=lambda kv: _rank(kv[1]))
        product_sure = False  # two medicines beside the tray: which tablets are which?
        others = [p for p in sources if p != product_id]
    else:
        product_id, source, product_sure = None, None, False
        others = [p["id"] for p in loose.loose_products(conn)][:6]

    unit = loose.unit_area(conn, product_id, cfg.min_unit_samples)
    count, count_sure = loose.count_tablets(areas, unit, cfg.unit_tolerance)
    count_sure = count_sure and not info.get("oversized")
    crop = tray_row["crop_path"]
    if media_dir is not None and crop:
        crop = loose.side_by_side(media_dir, source["crop_path"] if source else None, crop,
                                  f"crops/t{txn_id}_tray.jpg")
    detail = {"areas": areas, "oversized": bool(info.get("oversized")), "tray_crop": tray_row["crop_path"],
              "with_source": source is not None, "product_sure": product_sure, "count_sure": count_sure}
    return rest + [{
        "id": None,
        "pack_id": loose.base_pack(conn, product_id) if product_id else None,
        "count": max(1, count),
        "confidence": source["confidence"] if source else 0.0,
        "tier": "high" if product_sure and count_sure else ("medium" if product_id else "low"),
        "method": "tray",
        "bbox": None,
        "crop_path": crop,
        "candidates_json": json.dumps([(loose.base_pack(conn, p), 0.0) for p in dict.fromkeys(others)]),
        "tray_json": json.dumps(detail),
    }]


def close_stale(conn: sqlite3.Connection, minutes: int, media_dir: Path | None = None,
                delete_photo: bool = False, tray: TrayCfg | None = None) -> int:
    """Close transactions whose counter page vanished before the counter cleared."""
    cutoff = clock.stamp(clock.now() - timedelta(minutes=minutes))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE status = 'open' AND started_at < ?", (cutoff,)
    )]
    for txn_id in ids:
        close_transaction(conn, txn_id, media_dir, delete_photo, tray)
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

    tray = row["method"] == "tray"
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
        if tray:
            pk = catalog.pack(conn, pack_id)
            if pk is not None and loose.is_loose(conn, pk["product_id"]):
                pack_id = loose.base_pack(conn, pk["product_id"])  # tablets, whichever of its packs was picked
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

    # Every owner answer becomes an example in this store's gallery: a "Yes" is a
    # real counter photo of a known product, a correction teaches the look-alike,
    # "Not a sale" teaches what to ignore. Unchecked guesses never teach anything.
    # A tray photo is a box and some tablets, not a product photo: it teaches the
    # size of one tablet instead.
    if tray:
        if status in ("confirmed", "corrected") and final_pack is not None:
            _learn_tablet(conn, row, final_pack, row["count"])
    elif learn and row["crop_path"]:
        if status == "confirmed":
            learn(conn, row["crop_path"], final_pack, None, "confirmed")
        elif status == "corrected":
            learn(conn, row["crop_path"], final_pack, None, "correction")
        elif status == "removed":
            learn(conn, row["crop_path"], None, "other", "correction")

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


def settle_tray(conn: sqlite3.Connection, item_id: int, pack_id: int, count: int, text: str | None, *,
                learn: bool = False, media_dir: Path | None = None, delete_photo: bool = False) -> bool:
    """The tray reader decided what the owner would have: record the sale without asking.

    `learn` when the count came from the reader (not from the tray's own tablet
    size), so the tray learns from it as it would from the owner.
    """
    row = item(conn, item_id)
    if row is None or row["status"] != "pending":
        return False
    now = clock.stamp()
    conn.execute(
        "UPDATE transaction_items SET status = 'auto', pack_id = ?, count = ?, label_text = ?, label_checked_at = ?, "
        "verified_by = 'tray reader', verified_at = ? WHERE id = ?",
        (pack_id, count, text or None, now, now, item_id),
    )
    _write_sale(conn, item_id, pack_id, count, row["started_at"])
    if learn:
        _learn_tablet(conn, row, pack_id, count)
    _maybe_finish(conn, row["txn_id"], media_dir, delete_photo)
    return True


def _learn_tablet(conn: sqlite3.Connection, row: sqlite3.Row, pack_id: int, count: int) -> None:
    pk = catalog.pack(conn, pack_id)
    if pk is not None and loose.is_loose(conn, pk["product_id"]):
        detail = json.loads(row["tray_json"] or "{}")
        loose.learn_unit(conn, pk["product_id"], detail.get("areas", []), count)


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


def unknown_items(conn: sqlite3.Connection, days: int = 14) -> list[sqlite3.Row]:
    """Sales of something not in the product list, waiting for a name ("Not now", or no guess at the
    end of the day). Newest first; kept nameable for `days`."""
    since = clock.stamp(clock.now() - timedelta(days=days))
    return conn.execute(
        _ITEM_SQL + " WHERE ti.status = 'unknown' AND ti.crop_path IS NOT NULL AND ti.method != 'tray' "
        "AND t.started_at >= ? ORDER BY t.started_at DESC, ti.id DESC", (since,),
    ).fetchall()


def name_item(conn: sqlite3.Connection, item_id: int, pack_id: int, by: str, learn: Learner | None = None,
              media_dir: Path | None = None, delete_photo: bool = False) -> dict:
    """The owner said what a pending or "not in the list" item is: record the sale and learn the photo."""
    row = item(conn, item_id)
    if row is None:
        raise KeyError(item_id)
    if row["status"] not in ("pending", "unknown"):
        return {"status": row["status"], "already": True}
    conn.execute(
        "UPDATE transaction_items SET status = 'corrected', pack_id = ?, verified_by = ?, verified_at = ? WHERE id = ?",
        (pack_id, by, clock.stamp(), item_id),
    )
    _write_sale(conn, item_id, pack_id, row["count"], row["started_at"])
    if learn and row["crop_path"]:
        learn(conn, row["crop_path"], pack_id, None, "correction")
    _maybe_finish(conn, row["txn_id"], media_dir, delete_photo)
    return {"status": "corrected", "already": False, "count": row["count"]}


def reguess(conn: sqlite3.Connection, item_id: int, pack_id: int, score: float,
            candidates: list[tuple[int, float]]) -> bool:
    """Make `pack_id` an unanswered item's guess (a product the owner has just named).

    Always a question, never an automatic sale: one photo is too little to record
    sales alone. An item set aside as "not in the list" comes back to the check
    list, and goes out again in the next Telegram digest.
    """
    row = item(conn, item_id)
    if row is None or row["status"] not in ("pending", "unknown"):
        return False
    cands = [(pack_id, score)] + [(p, s) for p, s in candidates if p != pack_id]
    conn.execute(
        "UPDATE transaction_items SET status = 'pending', pack_id = ?, confidence = ?, tier = 'medium', "
        "candidates_json = ?, verified_by = NULL, verified_at = NULL, digest_id = NULL WHERE id = ?",
        (pack_id, round(score, 4), json.dumps([[p, round(s, 4)] for p, s in cands[:5]]), item_id),
    )
    conn.execute("UPDATE transactions SET status = 'pending' WHERE id = ? AND status != 'open'", (row["txn_id"],))
    return True

# --- what the camera set aside, and fixing what it recorded --------------------------------------

def _iou(a: list | tuple, b: list | tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def add_ignored(conn: sqlite3.Connection, txn_id: int, found: list[tuple[str, DetectionRecord]],
                media_dir: Path | None = None) -> int:
    """Remember what the camera set aside in one frame. The same thing sits there for every frame
    of a sale, so each is kept once per sale (the first photo of it). Returns how many are new."""
    added = 0
    for reason, d in found:
        seen = conn.execute("SELECT bbox FROM ignored_detections WHERE txn_id = ? AND reason = ?",
                            (txn_id, reason)).fetchall()
        if any(_iou(json.loads(s["bbox"]), d.bbox) > 0.5 for s in seen):
            if media_dir:
                _unlink(media_dir, d.crop_path)
            continue
        best = d.candidates[0] if d.candidates else (None, None)
        conn.execute(
            "INSERT INTO ignored_detections (txn_id, reason, score, best_pack_id, best_score, bbox, crop_path, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (txn_id, reason, d.confidence, best[0], best[1], json.dumps(list(d.bbox)), d.crop_path, clock.stamp()),
        )
        added += 1
    return added


def ignored(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """What the camera set aside between two stamps, newest first, with the product it looked most like."""
    return conn.execute(
        """
        SELECT i.*, p.name_en AS best_name, pk.units, pk.label
        FROM ignored_detections i
        LEFT JOIN product_packs pk ON pk.id = i.best_pack_id LEFT JOIN products p ON p.id = pk.product_id
        WHERE i.created_at >= ? AND i.created_at < ? ORDER BY i.created_at DESC, i.id DESC
        """, (start, end)).fetchall()


def item_from_ignored(conn: sqlite3.Connection, ignored_id: int) -> int:
    """The owner says something the camera set aside was a sale: make it an item ("not in the list"
    for now), dated when it happened, so the usual answers (a product, a new product) apply."""
    row = conn.execute("SELECT * FROM ignored_detections WHERE id = ?", (ignored_id,)).fetchone()
    if row is None:
        raise KeyError(ignored_id)
    if row["fixed_item_id"]:
        return row["fixed_item_id"]
    txn = conn.execute("SELECT id, status FROM transactions WHERE id = ?", (row["txn_id"],)).fetchone()
    if txn is None or txn["status"] == "open":
        # Nothing else was sold then, so that sale was never kept: record one for it now.
        txn_id = conn.execute(
            "INSERT INTO transactions (started_at, closed_at, source, status) VALUES (?, ?, 'camera', 'verified')",
            (row["created_at"], row["created_at"])).lastrowid
    else:
        txn_id = txn["id"]
    cands = [[row["best_pack_id"], row["best_score"]]] if row["best_pack_id"] else []
    item_id = conn.execute(
        """
        INSERT INTO transaction_items (txn_id, pack_id, guess_pack_id, count, confidence, tier, method, bbox,
                                       crop_path, candidates_json, status, hint)
        VALUES (?, NULL, NULL, 1, ?, 'low', 'image', ?, ?, ?, 'unknown', ?)
        """,
        (txn_id, row["best_score"] or 0.0, row["bbox"], row["crop_path"], json.dumps(cands),
         "background" if row["reason"] == "background" else "junk"),
    ).lastrowid
    conn.execute("UPDATE ignored_detections SET fixed_item_id = ? WHERE id = ?", (item_id, ignored_id))
    return item_id


def forget_ignored(conn: sqlite3.Connection, media_dir: Path | None, days: int) -> int:
    """Set-aside detections older than `days`, and their photos, are let go."""
    cutoff = clock.stamp(clock.now() - timedelta(days=days))
    old = conn.execute("SELECT id, crop_path FROM ignored_detections WHERE created_at < ? AND fixed_item_id IS NULL",
                       (cutoff,)).fetchall()
    for r in old:
        if media_dir:
            _unlink(media_dir, r["crop_path"])
    conn.execute("DELETE FROM ignored_detections WHERE created_at < ?", (cutoff,))
    return len(old)


FIXABLE = ("auto", "confirmed", "corrected", "best_guess", "removed")


def fix(conn: sqlite3.Connection, item_id: int, action: str, *, pack_id: int | None = None,
        count: int | None = None, by: str = "dashboard", learn: Learner | None = None) -> dict:
    """Correct a sale that was already recorded: another product, another count, or not a sale.

    The wrong sale leaves the stock ledger (it never happened) and the right one is written in its
    place, dated when it happened. The photo teaches the camera, like any answer.
    """
    row = item(conn, item_id)
    if row is None:
        raise KeyError(item_id)
    if row["status"] not in FIXABLE:
        raise ValueError("answer this one in the check list instead")
    new_pack = row["pack_id"] if action == "qty" else pack_id
    if action == "pick" and new_pack is None:
        raise ValueError("pick a product")
    new_count = max(1, count if count is not None else row["count"])
    conn.execute("DELETE FROM stock_movements WHERE ref_type = 'transaction_item' AND ref_id = ?", (item_id,))
    if action == "remove":
        conn.execute("UPDATE transaction_items SET status = 'removed', verified_by = ?, verified_at = ? WHERE id = ?",
                     (by, clock.stamp(), item_id))
        if learn and row["crop_path"] and row["method"] != "tray":
            learn(conn, row["crop_path"], None, "other", "correction")
        return {"status": "removed"}
    if action not in ("pick", "qty"):
        raise ValueError(f"unknown fix {action!r}")
    status = "confirmed" if new_pack == row["guess_pack_id"] and row["status"] != "removed" else "corrected"
    conn.execute(
        "UPDATE transaction_items SET status = ?, pack_id = ?, count = ?, verified_by = ?, verified_at = ? WHERE id = ?",
        (status, new_pack, new_count, by, clock.stamp(), item_id),
    )
    _write_sale(conn, item_id, new_pack, new_count, row["started_at"])
    if learn and row["crop_path"] and row["method"] != "tray" and new_pack != row["pack_id"]:
        learn(conn, row["crop_path"], new_pack, None, "correction")
    return {"status": status, "count": new_count}


def several(conn: sqlite3.Connection, item_id: int, picks: list[tuple[int, int]], by: str = "dashboard",
            media_dir: Path | None = None, delete_photo: bool = False) -> list[int]:
    """One photo held several items the camera saw as one: record each as its own sale.

    `picks` is [(pack_id, count)]. The photo shows them all together, so it teaches the camera
    nothing (it would blur every product it's filed under); the original item is marked "split".
    """
    row = item(conn, item_id)
    if row is None:
        raise KeyError(item_id)
    if row["status"] not in ("pending", "unknown"):
        raise ValueError("this one is already answered")
    picks = [(p, max(1, n)) for p, n in picks if p]
    if not picks:
        raise ValueError("pick at least one product")
    made = []
    for pack_id, n in picks:
        new = conn.execute(
            """
            INSERT INTO transaction_items (txn_id, pack_id, guess_pack_id, count, confidence, tier, method, bbox,
                                           crop_path, candidates_json, status, verified_by, verified_at)
            VALUES (?, ?, ?, ?, ?, 'low', 'split', ?, ?, '[]', 'corrected', ?, ?)
            """,
            (row["txn_id"], pack_id, row["guess_pack_id"], n, row["confidence"], row["bbox"], row["crop_path"],
             by, clock.stamp()),
        ).lastrowid
        _write_sale(conn, new, pack_id, n, row["started_at"])
        made.append(new)
    conn.execute("UPDATE transaction_items SET status = 'split', verified_by = ?, verified_at = ? WHERE id = ?",
                 (by, clock.stamp(), item_id))
    _maybe_finish(conn, row["txn_id"], media_dir, delete_photo)
    return made


def product_history(conn: sqlite3.Connection, product_id: int, limit: int = 300) -> list[sqlite3.Row]:
    """Every camera sighting that ended up as this product, or that the camera first took for it.

    Newest first, with what the camera guessed, what was recorded, and who settled it.
    """
    return conn.execute(
        """
        SELECT ti.*, t.started_at, t.source,
               p.name_en AS final_name, pk.units, pk.label, pk.product_id AS final_product,
               gp.name_en AS guess_name, gpk.product_id AS guess_product
        FROM transaction_items ti JOIN transactions t ON t.id = ti.txn_id
        LEFT JOIN product_packs pk ON pk.id = ti.pack_id LEFT JOIN products p ON p.id = pk.product_id
        LEFT JOIN product_packs gpk ON gpk.id = ti.guess_pack_id LEFT JOIN products gp ON gp.id = gpk.product_id
        WHERE (pk.product_id = :p OR gpk.product_id = :p) AND ti.status != 'split'
        ORDER BY t.started_at DESC, ti.id DESC LIMIT :n
        """, {"p": product_id, "n": limit}).fetchall()


def day_log(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """The camera log for a stretch of time: every sale with all its items, newest first."""
    txns = conn.execute(
        "SELECT * FROM transactions WHERE started_at >= ? AND started_at < ? AND status != 'open' "
        "ORDER BY started_at DESC, id DESC", (start, end)).fetchall()
    out = []
    for t in txns:
        rows = conn.execute(
            """
            SELECT ti.*, t.started_at, p.name_en, p.name_km, pk.units, pk.label, pk.product_id,
                   gp.name_en AS guess_name
            FROM transaction_items ti JOIN transactions t ON t.id = ti.txn_id
            LEFT JOIN product_packs pk ON pk.id = ti.pack_id LEFT JOIN products p ON p.id = pk.product_id
            LEFT JOIN product_packs gpk ON gpk.id = ti.guess_pack_id LEFT JOIN products gp ON gp.id = gpk.product_id
            WHERE ti.txn_id = ? AND ti.status != 'split' ORDER BY ti.id
            """, (t["id"],)).fetchall()
        out.append({"txn": t, "items": rows})
    return out

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
