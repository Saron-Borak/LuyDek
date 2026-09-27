from datetime import datetime, timedelta

import pytest

from luydek.services import forecast, stock
from tests.conftest import NOW, pid


def day(n_days_ago: int, hour: int = 12) -> datetime:
    return (NOW - timedelta(days=n_days_ago)).replace(hour=hour, minute=0, second=0)


def sell_daily(conn, product_id: int, per_day: int, first_ago: int, last_ago: int) -> None:
    for ago in range(first_ago, last_ago - 1, -1):
        stock.record(conn, product_id, -per_day, "sale", at=day(ago, 19))


# --- ledger -----------------------------------------------------------------------

def test_on_hand_is_the_sum_of_movements(conn):
    p = pid(conn, "coke-330")
    stock.record(conn, p, 48, "delivery")
    stock.record(conn, p, -5, "sale")
    stock.record(conn, p, -1, "adjustment")
    assert stock.on_hand(conn)[p] == 42


def test_adjust_reasons_split_demand_from_losses(conn):
    p = pid(conn, "coke-330")
    stock.adjust(conn, p, "credit_sale", 2)
    stock.adjust(conn, p, "family_use", 1)
    stock.adjust(conn, p, "put_back", 1)
    rows = conn.execute("SELECT type, qty FROM stock_movements WHERE product_id = ? ORDER BY id", (p,)).fetchall()
    # Credit sales are demand the forecast must see; family use is not; a put-back undoes a sale.
    assert [(r["type"], r["qty"]) for r in rows] == [("sale", -2), ("adjustment", -1), ("sale", 1)]


def test_opening_count_sets_levels(conn):
    a, b = pid(conn, "coke-330"), pid(conn, "sprite-1500")
    stock.record(conn, a, 10, "delivery")
    stock.opening_count(conn, {a: 7, b: 12})
    assert stock.on_hand(conn)[a] == 7
    assert stock.on_hand(conn)[b] == 12


def test_negative_stock_is_flagged_not_shown_as_real(conn):
    p = pid(conn, "coke-330")
    stock.record(conn, p, -3, "sale")
    row = next(r for r in stock.stock_table(conn) if r["id"] == p)
    assert row["negative"] and row["out"]


def test_fifo_expiry_tracks_the_oldest_batch_still_on_the_shelf(conn):
    p = pid(conn, "para-250")
    stock.record(conn, p, 24, "delivery", expiry_date="2026-10-01", at=day(20))
    stock.record(conn, p, 24, "delivery", expiry_date="2027-01-01", at=day(5))
    stock.record(conn, p, -18, "sale", at=day(2))            # 30 left: both batches
    assert stock.earliest_live_expiry(conn, stock.on_hand(conn))[p] == "2026-10-01"
    stock.record(conn, p, -10, "sale", at=day(1))            # 20 left: newest batch only
    assert stock.earliest_live_expiry(conn, stock.on_hand(conn))[p] == "2027-01-01"


# --- forecast ------------------------------------------------------------------------

def plan_line(conn, cfg, sku):
    for plan in forecast.build_plans(conn, cfg.forecast):
        for line in plan.lines:
            if line.sku == sku:
                return plan, line
    raise AssertionError(sku)


def test_order_rounds_up_to_whole_cases(conn, cfg):
    p = pid(conn, "para-500")                      # Pharma visits Monday = tomorrow; case of 10
    stock.record(conn, p, 130, "opening", at=day(29, 8))
    sell_daily(conn, p, 4, 28, 1)                  # 112 sold, 18 left
    plan, line = plan_line(conn, cfg, "para-500")
    assert plan.days_to_visit == 1 and plan.cycle_days == 7
    assert line.avg_daily == pytest.approx(4)
    # target = 4/day x (1 + 7) days + 2 safety days x 4 = 40; need 22 -> 3 cases of 10
    assert (line.action, line.cases, line.units) == ("order", 3, 30)
    assert line.sold_last_week == 28


def test_stockout_days_do_not_count_as_zero_demand(conn, cfg):
    p = pid(conn, "para-500")
    stock.record(conn, p, 70, "opening", at=day(29, 8))
    sell_daily(conn, p, 5, 28, 15)                 # sells out after two weeks
    _, line = plan_line(conn, cfg, "para-500")
    assert line.stockout_days == 14
    assert line.avg_daily == pytest.approx(5)      # not 2.5
    assert line.action == "order" and line.cases == 5


def test_recent_weeks_weigh_more(conn, cfg):
    p = pid(conn, "para-500")
    stock.record(conn, p, 500, "opening", at=day(29, 8))
    sell_daily(conn, p, 2, 28, 8)
    sell_daily(conn, p, 10, 7, 1)
    _, line = plan_line(conn, cfg, "para-500")
    assert line.avg_daily == pytest.approx((4 * 70 + 3 * 14 + 2 * 14 + 1 * 14) / 70, abs=1e-3)


def test_expiry_cap_turns_a_case_order_into_a_market_run(conn, cfg):
    p = pid(conn, "para-500")
    conn.execute("UPDATE products SET shelf_life_days = 5 WHERE id = ?", (p,))
    stock.record(conn, p, 130, "opening", at=day(29, 8))
    sell_daily(conn, p, 4, 28, 1)                  # 18 left, needs 22 more
    _, line = plan_line(conn, cfg, "para-500")
    # A case of 10 cannot sell before it expires once 18 are already on the shelf.
    assert line.action == "market" and line.units == 22 and line.capped_by_expiry


def test_unsold_stock_is_sleeping_money(conn, cfg):
    p = pid(conn, "vitc-1000")
    stock.record(conn, p, 14, "opening", at=day(60, 8))
    _, line = plan_line(conn, cfg, "vitc-1000")
    assert line.action == "sleeping"
    assert line.value == pytest.approx(14 * 2.10)


def test_no_sale_for_a_month_is_skip(conn, cfg):
    p = pid(conn, "para-500")
    stock.record(conn, p, 20, "opening", at=day(40, 8))
    stock.record(conn, p, -1, "sale", at=day(35))
    _, line = plan_line(conn, cfg, "para-500")
    assert (line.action, line.reason) == ("skip", "no_sales")


def test_overstock_is_skip(conn, cfg):
    p = pid(conn, "para-500")
    stock.record(conn, p, 300, "opening", at=day(29, 8))
    sell_daily(conn, p, 3, 28, 1)
    _, line = plan_line(conn, cfg, "para-500")
    assert (line.action, line.reason) == ("skip", "overstock")


def test_observation_window_holds_back_orders(conn, cfg):
    cfg.forecast.observe_until = "2026-10-12"
    p = pid(conn, "para-500")
    stock.record(conn, p, 130, "opening", at=day(29, 8))
    sell_daily(conn, p, 4, 28, 1)
    _, line = plan_line(conn, cfg, "para-500")
    assert (line.action, line.reason, line.cases) == ("check", "observing", 0)


def test_follow_rate_is_settled_from_the_next_delivery(conn, cfg):
    p, q = pid(conn, "para-500"), pid(conn, "ors-sachet")
    sup = conn.execute("SELECT supplier_id FROM products WHERE id = ?", (p,)).fetchone()[0]
    visit = (NOW - timedelta(days=6)).date().isoformat()
    for product, action, cases in ((p, "order", 3), (q, "skip", 0)):
        conn.execute(
            "INSERT INTO forecasts (visit_date, supplier_id, product_id, avg_daily_sales, current_stock, "
            "recommended_units, recommended_cases, action, reason, created_at) VALUES (?, ?, ?, 1, 1, ?, ?, ?, '', ?)",
            (visit, sup, product, cases * 10, cases, action, visit),
        )
    stock.record(conn, p, 30, "delivery", at=day(6, 9))    # followed: 3 cases as recommended
    stock.record(conn, q, 50, "delivery", at=day(6, 9))    # not followed: bought despite SKIP
    forecast.settle_follow_rate(conn)
    got = dict(conn.execute("SELECT product_id, followed FROM forecasts").fetchall())
    assert got == {p: 1, q: 0}
