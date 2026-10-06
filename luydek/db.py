"""SQLite access: one short-lived connection per unit of work."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config, get_config

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # The web server's threadpool, the bot's event loop and the job queue all
    # open their own connections; WAL lets readers run beside the one writer.
    conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS never
# alters an existing table, so databases from earlier versions get them here.
MIGRATIONS = {
    "transaction_items": {"label_text": "TEXT", "label_checked_at": "TEXT", "tray_json": "TEXT",
                          "name_guess": "TEXT", "name_guess_km": "TEXT", "name_checked_at": "TEXT", "hint": "TEXT"},
    "frame_detections": {"hint": "TEXT"},
    "products": {"sold_loose": "INTEGER NOT NULL DEFAULT 0", "origin": "TEXT NOT NULL DEFAULT 'catalog'",
                 "added_at": "TEXT", "location_id": "INTEGER"},
    "invoice_lines": {"name_suggested": "TEXT", "name_suggested_km": "TEXT", "units_per_case": "INTEGER",
                      "unit_name": "TEXT", "lot_no": "TEXT"},
    "stock_movements": {"batch_id": "INTEGER", "location_id": "INTEGER", "lot_no": "TEXT"},
    "gallery_images": {"area_frac": "REAL"},
    "locations": {"kind": "TEXT", "levels": "INTEGER", "floor_x": "REAL", "floor_z": "REAL", "floor_turn": "INTEGER"},
}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    for table, columns in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


@contextmanager
def session(cfg: Config | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success and rolls back on error."""
    conn = connect((cfg or get_config()).db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
