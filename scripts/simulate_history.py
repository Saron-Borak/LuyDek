"""Generate SIMULATED sales and delivery history so the weekly order lists have data.

The pilot needs 2-4 weeks of real history before the forecast means anything;
for a demo this fast-forwards. Every row written here has simulated = 1, and
the dashboard and Telegram order lists say so whenever such rows exist.

    python scripts/simulate_history.py --weeks 8
    python scripts/simulate_history.py --clear     # remove simulated rows only

The story it tells comes from the `sim:` block of each product in the seed
catalog: best-sellers, a dead green tea, a baby-formula stockout after a
forgotten order, fever-season paracetamol, all ordered "by feeling".
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luydek import clock, db  # noqa: E402
from luydek.config import Config, load_config  # noqa: E402
from luydek.services import catalog, stock  # noqa: E402

WEEKEND_BOOST = 1.25
WEEKDAY_FACTOR = 0.93


def simulate(cfg: Config, weeks: int = 8, seed: int = 7) -> int:
    """Replace all simulated movements with `weeks` of fresh history ending yesterday."""
    params = catalog.seed_sim_params(cfg.resolve(cfg.paths.seed_catalog))
    rng = np.random.default_rng(seed)
    with db.session(cfg) as conn:
        db.init_schema(conn)
        conn.execute("DELETE FROM stock_movements WHERE simulated = 1")
        conn.execute("DELETE FROM forecasts")
        products = conn.execute(
            """
            SELECT p.*, s.visit_weekday FROM products p JOIN suppliers s ON s.id = p.supplier_id
            WHERE p.active = 1
            """
        ).fetchall()
        if not products:
            sys.exit("No products. Run scripts/seed_demo.py first.")

        today = clock.today()
        start = today - timedelta(weeks=weeks)
        written = 0
        for p in products:
            sim = params.get(p["sku"], {})
            rate = float(sim.get("rate", 1.0))
            trend = float(sim.get("trend", 1.0))
            feeling = int(sim.get("feeling_cases", 1))
            forget = set(sim.get("forget_visits", []))
            level = int(sim.get("start_stock", p["case_size"]))

            stock.record(conn, p["id"], level, "opening", ref_type="sim", reason="simulated opening count",
                         simulated=True, at=datetime.combine(start, time(7, 0)))
            written += 1
            visits = [start + timedelta(days=i) for i in range((today - start).days)
                      if (start + timedelta(days=i)).weekday() == p["visit_weekday"]]

            for i in range((today - start).days):
                day = start + timedelta(days=i)
                if day in visits:
                    from_end = len(visits) - visits.index(day)  # 1 = the most recent visit
                    # "By feeling": glance at the shelf, order when it looks low.
                    if feeling and from_end not in forget and level < p["case_size"] * feeling:
                        qty = feeling * p["case_size"]
                        expiry = (day + timedelta(days=p["shelf_life_days"] or 365)).isoformat()
                        stock.record(conn, p["id"], qty, "delivery", ref_type="sim", reason="simulated delivery",
                                     unit_cost=p["unit_cost"], expiry_date=expiry, simulated=True,
                                     at=datetime.combine(day, time(8, 30)))
                        level += qty
                        written += 1
                weeks_back = (today - day).days // 7
                mean = rate * trend ** (-weeks_back) * (WEEKEND_BOOST if day.weekday() >= 5 else WEEKDAY_FACTOR)
                sold = min(int(rng.poisson(mean)), level)
                if sold:
                    stock.record(conn, p["id"], -sold, "sale", ref_type="sim", reason="simulated sales",
                                 simulated=True, at=datetime.combine(day, time(19, 0)))
                    level -= sold
                    written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--weeks", type=int, default=8, help="weeks of history ending yesterday")
    parser.add_argument("--seed", type=int, default=7, help="random seed")
    parser.add_argument("--clear", action="store_true", help="only delete existing simulated rows")
    args = parser.parse_args()

    cfg = load_config()
    if args.clear:
        with db.session(cfg) as conn:
            deleted = conn.execute("DELETE FROM stock_movements WHERE simulated = 1").rowcount
            conn.execute("DELETE FROM forecasts")
        print(f"Deleted {deleted} simulated movements.")
        return
    written = simulate(cfg, args.weeks, args.seed)
    today = clock.today()
    print(f"Wrote {written} SIMULATED movements covering {today - timedelta(weeks=args.weeks)} "
          f"to {today - timedelta(days=1)}.")


if __name__ == "__main__":
    main()
