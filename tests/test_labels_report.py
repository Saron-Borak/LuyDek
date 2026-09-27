from datetime import date, datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx
from PIL import Image

from luydek import db
from luydek.recognition import labels
from luydek.services import report, stock, transactions
from tests.conftest import NOW, pack_of, pid


# --- label reader --------------------------------------------------------------------------

class FakeClient:
    def __init__(self, reading=None, error=None):
        self.calls, self.reading, self.error = [], reading, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.reading)


def pending_lookalike(conn, cfg, second_score=0.94):
    (cfg.media_dir / "crops").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 90), "white").save(cfg.media_dir / "crops" / "p.jpg")
    p250, p500 = pack_of(conn, "para-250"), pack_of(conn, "para-500")
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [transactions.DetectionRecord(
        p250, 0.95, "medium", "image", (0, 0, 60, 90), "crops/p.jpg", [(p250, 0.95), (p500, second_score)])])
    transactions.close_transaction(conn, txn)
    conn.commit()
    return transactions.pending_items(conn)[0]["id"]


def test_only_close_scores_count_as_lookalikes():
    assert labels.lookalikes([(1, 0.95), (2, 0.93), (3, 0.80)], 0.05, 4) == [1, 2]
    assert labels.lookalikes([(1, 0.95), (2, 0.85)], 0.05, 4) == []
    assert labels.lookalikes([(1, 0.95)], 0.05, 4) == []


def test_a_readable_label_settles_the_item_without_the_owner(conn, cfg):
    item_id = pending_lookalike(conn, cfg)
    client = FakeClient(labels.LabelReading(text_seen="Paracetamol 500mg", choice=2, sure=True))
    assert labels.settle(cfg, client=client) == 1

    call = client.calls[0]
    image, text = call["messages"][0]["content"]
    assert image["type"] == "image"
    assert "1. Paracetamol 250mg children" in text["text"] and "2. Paracetamol 500mg" in text["text"]
    assert call["output_config"] == {"effort": "low"} and call["fallbacks"] == "default"

    check = db.connect(cfg.db_path)
    row = transactions.item(check, item_id)
    assert (row["status"], row["method"], row["pack_id"], row["label_text"]) == \
        ("auto", "label", pack_of(check, "para-500"), "Paracetamol 500mg")
    assert stock.on_hand(check)[pid(check, "para-500")] == -1


def test_an_unclear_label_leaves_it_to_the_owner_and_is_not_asked_twice(conn, cfg):
    item_id = pending_lookalike(conn, cfg)
    client = FakeClient(labels.LabelReading(text_seen="PARA 5?0mg", choice=2, sure=False))
    assert labels.settle(cfg, client=client) == 0
    row = transactions.item(db.connect(cfg.db_path), item_id)
    assert row["status"] == "pending" and row["label_text"] == "PARA 5?0mg" and row["label_checked_at"]
    labels.settle(cfg, client=client)
    assert len(client.calls) == 1


def test_clear_winners_and_offline_moments_cost_nothing(conn, cfg):
    item_id = pending_lookalike(conn, cfg, second_score=0.70)       # not a look-alike
    client = FakeClient(labels.LabelReading(text_seen="", choice=1, sure=True))
    labels.settle(cfg, client=client)
    assert client.calls == [] and transactions.item(db.connect(cfg.db_path), item_id)["label_checked_at"]

    conn.execute("DELETE FROM transaction_items"); conn.commit()
    item_id = pending_lookalike(conn, cfg)
    offline = FakeClient(error=anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")))
    assert labels.settle(cfg, client=offline) == 0
    assert transactions.item(db.connect(cfg.db_path), item_id)["label_checked_at"] is None  # retried later


def test_without_an_api_key_nothing_is_sent(conn, cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    pending_lookalike(conn, cfg)
    assert labels.settle(cfg) == 0


# --- pilot report -------------------------------------------------------------------------------

def at(d: date, hour: int = 12) -> datetime:
    return datetime.combine(d, datetime.min.time()).replace(hour=hour)


def test_report_compares_the_watching_weeks_with_the_latest_full_weeks(conn, cfg):
    start = (NOW - timedelta(days=35)).date()          # Sunday 23 Aug: weeks 1-2 watch, 3-5 pilot, 6 in progress
    cfg.pilot.start = start.isoformat()
    dumex, vitc = pid(conn, "dumex-400"), pid(conn, "vitc-1000")

    # A must-have item runs out in the watching weeks, then stays stocked once advice starts.
    stock.record(conn, dumex, 10, "opening", at=at(start, 7))
    for d in range(2):
        stock.record(conn, dumex, -5, "sale", at=at(start + timedelta(days=d)))
    stock.record(conn, dumex, 50, "delivery", at=at(start + timedelta(days=14), 8))
    for d in range(14, 35):
        stock.record(conn, dumex, -2, "sale", at=at(start + timedelta(days=d)))
    # Dead stock from before the pilot finally sells in week 3.
    stock.record(conn, vitc, 14, "opening", at=datetime(2026, 7, 1, 8))
    stock.record(conn, vitc, -1, "sale", at=at(start + timedelta(days=18)))

    rep = report.build(conn, cfg)
    assert [w.phase for w in rep.weeks] == ["baseline"] * 2 + ["pilot"] * 4
    assert rep.weeks[-1].partial and rep.comparable
    assert [w.stockout_critical for w in rep.weeks[:3]] == [5, 7, 0]
    assert rep.weeks[0].dead_value == round(14 * 2.10, 2) and rep.weeks[3].dead_value == 0

    head = {h.key: h for h in rep.headlines}
    assert (head["stockout_critical"].before, head["stockout_critical"].now) == (6.0, 0.0)
    assert head["stockout_critical"].good is True
    assert head["dead_value"].good is True
    assert head["accuracy"].now is None                       # no camera sales yet: not invented


def test_baseline_weeks_stay_on_the_report_however_long_the_pilot_runs(conn, cfg):
    cfg.pilot.start = (NOW - timedelta(days=7 * 20)).date().isoformat()
    cfg.pilot.report_weeks = 8
    stock.record(conn, pid(conn, "coke-330"), 10, "opening", at=NOW - timedelta(days=7 * 20))
    rep = report.build(conn, cfg)
    assert len(rep.weeks) == 8
    assert [w.phase for w in rep.weeks[:3]] == ["baseline", "baseline", "pilot"]


def test_report_page_renders_charts_and_the_numbers_table(conn, cfg, stub_encoder, monkeypatch):
    from fastapi.testclient import TestClient
    from luydek.web.server import create_app

    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    stock.record(conn, pid(conn, "coke-330"), 10, "opening", at=NOW - timedelta(days=30))
    conn.commit()
    cfg.telegram.enabled = False
    with TestClient(create_app(cfg)) as client:
        html = client.get("/report").text
    assert "Pilot report" in html and html.count("<svg") == len(report.SERIES)
    assert "Empty-shelf days on must-have items" in html and "Show the numbers" in html
