"""Stock by batch: what is on the shelf, when each lot expires, and where it is.

Every movement that brings stock in (a delivery, a free bonus, a count that found more) starts a
batch with its own expiry date and place. Nothing stores what is left of a batch: it is worked
out by replaying the ledger in order. A sale takes from the batch that expires first ("first
expired, first out", which is how the shop is told to shelve), except a removal that names its
batch (expired stock taken off the shelf). Batches without a printed date are taken in the order
they came in, after the dated ones that expire sooner.

From that come the three things the shop uses:

* the **shelf check**: walking the shop place by place, what to remove (expired), what to put in
  front (expires soon), and what won't sell before it expires (discount, bundle or send back);
* **put-away** instructions when a delivery comes in: in front of or behind what is already there;
* the soonest expiry still on the shelf, for warnings and order lists.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import clock

NO_DATE = "9999-12-31"


@dataclass
class Batch:
    id: int                    # the movement that brought it in
    product_id: int
    received_at: str
    qty_in: int
    left: int
    kind: str                  # delivery / freebie / opening / spot_check / adjustment
    expiry: str | None         # printed on the pack (from the invoice, a photo of the date, or typed)
    est_expiry: str | None     # came in + the product's shelf life, when no date was given
    location_id: int | None
    lot_no: str | None = None

    @property
    def order(self) -> tuple:
        """Which goes first: the soonest expiry, then the oldest."""
        return (self.expiry or self.est_expiry or NO_DATE, self.received_at, self.id)


@dataclass
class Stock:
    batches: list[Batch] = field(default_factory=list)   # every batch, in the order they came in
    deficit: int = 0           # sold or taken beyond everything known to have come in

    @property
    def open(self) -> list[Batch]:
        return sorted((b for b in self.batches if b.left > 0), key=lambda b: b.order)


def replay(conn: sqlite3.Connection, product_ids: list[int] | None = None) -> dict[int, Stock]:
    """Each product's batches with what is left of them, from the whole ledger."""
    where, params = "", ()
    if product_ids is not None:
        if not product_ids:
            return {}
        where = f"WHERE m.product_id IN ({','.join('?' * len(product_ids))})"
        params = tuple(product_ids)
    life = {r["id"]: r["shelf_life_days"] for r in conn.execute("SELECT id, shelf_life_days FROM products")}
    out: dict[int, Stock] = {}
    for m in conn.execute(
        f"""
        SELECT m.id, m.product_id, m.qty, m.type, m.expiry_date, m.created_at, m.batch_id, m.location_id, m.lot_no
        FROM stock_movements m {where} ORDER BY m.created_at, m.id
        """, params):
        s = out.setdefault(m["product_id"], Stock())
        qty = m["qty"]
        target = next((b for b in s.batches if b.id == m["batch_id"]), None) if m["batch_id"] else None
        if qty > 0:
            if target is not None:
                target.left += qty
                continue
            repaid = min(s.deficit, qty)        # stock that was sold before anyone knew it was there
            s.deficit -= repaid
            if repaid == qty:
                continue
            if m["type"] == "sale" and s.open:  # put back on the shelf: to the batch it most likely left
                s.open[0].left += qty - repaid
                continue
            shelf_life = life.get(m["product_id"])
            est = None
            if not m["expiry_date"] and shelf_life:
                est = (date.fromisoformat(m["created_at"][:10]) + timedelta(days=shelf_life)).isoformat()
            s.batches.append(Batch(m["id"], m["product_id"], m["created_at"], qty, qty - repaid, m["type"],
                                   m["expiry_date"], est, m["location_id"], m["lot_no"]))
        elif qty < 0:
            need = -qty
            if target is not None:
                take = min(target.left, need)
                target.left -= take
                need -= take
            for b in s.open:
                if not need:
                    break
                take = min(b.left, need)
                b.left -= take
                need -= take
            s.deficit += need
    return out


def open_batches(conn: sqlite3.Connection, product_id: int) -> list[Batch]:
    return replay(conn, [product_id]).get(product_id, Stock()).open


def earliest_expiry(conn: sqlite3.Connection) -> dict[int, str]:
    """The soonest printed expiry still on the shelf, per product."""
    out = {}
    for pid, s in replay(conn).items():
        dated = [b.expiry for b in s.open if b.expiry]
        if dated:
            out[pid] = min(dated)
    return out


def places(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {r["id"]: r for r in conn.execute("SELECT * FROM locations")}


# --- the shelf check ----------------------------------------------------------------------------

@dataclass
class Task:
    batch: Batch
    product: dict
    action: str                # remove (expired) / front (expires soon) / clear (won't sell in time)
    days_left: int             # until it expires; negative once expired
    place: str | None
    place_sort: tuple


def shelf_check(conn: sqlite3.Connection, within_days: int = 30, today: date | None = None) -> list[Task]:
    """What to do on a walk round the shop, in walking order (place by place, soonest first).

    Only batches with a printed date: a guessed shelf life is good enough to decide which goes
    first, not to tell someone to throw stock away.
    """
    from . import stock  # stock imports this module for the soonest expiry

    today = today or clock.today()
    soon = today + timedelta(days=within_days)
    rows = {r["id"]: r for r in stock.stock_table(conn, within_days)}
    where = places(conn)
    tasks = []
    for pid, s in replay(conn).items():
        p = rows.get(pid)
        if p is None:
            continue
        daily = p["sold_28d"] / 28
        ahead = 0                               # units that go first, so must sell before this batch
        for b in s.open:
            if not b.expiry:
                ahead += b.left
                continue
            exp = date.fromisoformat(b.expiry)
            if exp > soon:
                ahead += b.left
                continue
            days = (exp - today).days
            if days < 0:
                action = "remove"
            elif daily <= 0 or (ahead + b.left) / daily > days:
                action = "clear"
            else:
                action = "front"
            ahead += b.left
            loc = where.get(b.location_id) if b.location_id else None
            tasks.append(Task(b, p, action, days, loc["name"] if loc else None,
                              (loc["sort"], loc["name"].lower()) if loc else (10**6, "")))
    return sorted(tasks, key=lambda t: (t.place_sort, t.days_left, t.product["name_en"]))


# --- putting a delivery away -----------------------------------------------------------------------

def put_away(conn: sqlite3.Connection, batch_ids: list[int]) -> list[dict]:
    """Where each new batch goes: in front of what is already there if it expires sooner, behind
    it if later (the usual case), or simply on its shelf when there is nothing left of the old."""
    if not batch_ids:
        return []
    moves = conn.execute(f"SELECT id, product_id FROM stock_movements WHERE id IN ({','.join('?' * len(batch_ids))})",
                         batch_ids).fetchall()
    stocks = replay(conn, sorted({m["product_id"] for m in moves}))
    where = places(conn)
    names = {r["id"]: r for r in conn.execute("SELECT id, name_en, name_km FROM products")}
    out = []
    for m in moves:
        s = stocks.get(m["product_id"], Stock())
        new = next((b for b in s.batches if b.id == m["id"]), None)
        if new is None or new.left <= 0:
            continue
        older = [b for b in s.open if b.id != new.id and b.received_at <= new.received_at]
        how, old_left, old_expiry = "on", 0, None
        if older:
            old_left = sum(b.left for b in older)
            dated = [b.expiry for b in older if b.expiry]
            old_expiry = min(dated) if dated else None
            how = "front" if (new.expiry and old_expiry and new.expiry < old_expiry) else "behind"
        loc = where.get(new.location_id) if new.location_id else None
        p = names[m["product_id"]]
        out.append({"batch_id": new.id, "product_id": m["product_id"], "name_en": p["name_en"], "name_km": p["name_km"],
                    "qty": new.left, "expiry": new.expiry, "place": loc["name"] if loc else None, "how": how,
                    "old_left": old_left, "old_expiry": old_expiry})
    return out


def for_invoice(conn: sqlite3.Connection, invoice_id: int) -> list[int]:
    """The batches an invoice brought in."""
    return [r["id"] for r in conn.execute(
        "SELECT m.id FROM stock_movements m JOIN invoice_lines l ON l.id = m.ref_id AND m.ref_type = 'invoice_line' "
        "WHERE l.invoice_id = ? AND m.qty > 0 ORDER BY m.id", (invoice_id,))]


# --- changing a batch ---------------------------------------------------------------------------

def get(conn: sqlite3.Connection, batch_id: int) -> Batch | None:
    row = conn.execute("SELECT product_id FROM stock_movements WHERE id = ? AND qty > 0", (batch_id,)).fetchone()
    if row is None:
        return None
    return next((b for b in replay(conn, [row["product_id"]]).get(row["product_id"], Stock()).batches
                 if b.id == batch_id), None)


def remove(conn: sqlite3.Connection, batch_id: int, qty: int | None = None, reason: str = "expired",
           by: str | None = None) -> int:
    """Take a batch (or part of it) off the shelf. Returns how many were taken."""
    from . import stock

    b = get(conn, batch_id)
    if b is None:
        raise ValueError("no such batch")
    n = b.left if qty is None else max(0, min(int(qty), b.left))
    if n:
        stock.record(conn, b.product_id, -n, "adjustment", ref_type="batch", ref_id=batch_id, batch_id=batch_id,
                     reason=f"{reason} by {by}" if by else reason)
    return n


def update(conn: sqlite3.Connection, batch_id: int, *, expiry: str | None = ..., location_id: int | None = ...,
           lot_no: str | None = ...) -> None:
    """Correct a batch's printed date, the place it was put, or its lot number."""
    sets, params = [], []
    for column, value in (("expiry_date", expiry), ("location_id", location_id), ("lot_no", lot_no)):
        if value is not ...:
            sets.append(f"{column} = ?")
            params.append(value or None)
    if not sets:
        return
    if conn.execute(f"UPDATE stock_movements SET {', '.join(sets)} WHERE id = ? AND qty > 0",
                    (*params, batch_id)).rowcount != 1:
        raise ValueError("no such batch")
