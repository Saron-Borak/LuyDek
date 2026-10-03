import cv2
import numpy as np
import pytest

from luydek import db
from luydek.recognition import pipeline, segment
from luydek.recognition import encoder as encoder_mod
from luydek.recognition.matcher import Gallery, Match, colour_signature, colour_tiebreak, tier
from luydek.services import stock, transactions
from tests.conftest import jpeg, mat_frame, pack_of, paint, pid


def enroll_all(conn, cfg, skus=("coke-330", "sprite-1500", "ichitan-420", "dumex-400"), shots=4):
    rec = pipeline.get_recognizer(cfg)
    for sku in skus:
        for i in range(shots):
            frame = mat_frame()
            paint(frame, sku, 100 + 60 * i, 80 + 30 * i, 90 + 5 * i, 130 - 4 * i)
            rec.enroll(conn, frame, pack_of(conn, sku))
    return rec


def test_segment_finds_each_item_on_the_mat(cfg):
    frame = mat_frame()
    paint(frame, "coke-330", 40, 60)
    paint(frame, "sprite-1500", 260, 200)
    paint(frame, "dumex-400", 470, 90)
    blobs = segment.segment(frame, mat_frame(), cfg.recognition.segment)
    assert len(blobs) == 3
    # Crops include a margin but not the neighbours.
    assert all(b.crop.size[0] < 200 for b in blobs)


def test_segment_without_calibration_guesses_the_mat_from_the_border(cfg):
    frame = mat_frame()
    paint(frame, "coke-330", 200, 150)
    assert len(segment.segment(frame, None, cfg.recognition.segment)) == 1


def test_empty_mat_has_no_items(cfg):
    assert segment.segment(mat_frame(), mat_frame(), cfg.recognition.segment) == []


def test_tiers_need_both_score_and_margin(cfg):
    rc = cfg.recognition
    assert tier(0.99, 0.10, rc) == "high"
    assert tier(0.99, 0.001, rc) == "medium"   # a look-alike right behind: ask the owner
    assert tier(0.40, 0.30, rc) == "low"


def test_gallery_scores_packs_by_their_best_matches():
    v = np.eye(4, dtype=np.float32)
    g = Gallery(np.stack([v[0], v[0], v[1], v[2]]), [("pack", 1), ("pack", 1), ("pack", 2), ("ignore", "phone")])
    m = g.match(v[0], k=2)
    assert m.pack_id == 1 and m.score == pytest.approx(1.0)
    assert g.match(v[2]).ignore_label == "phone"


def test_recognize_matches_enrolled_products(conn, cfg, stub_encoder):
    rec = enroll_all(conn, cfg)
    frame = mat_frame()
    paint(frame, "coke-330", 60, 80)
    paint(frame, "dumex-400", 380, 220)
    found = {d.record.pack_id for d in rec.recognize(conn, frame, "t1")}
    assert found == {pack_of(conn, "coke-330"), pack_of(conn, "dumex-400")}


def test_ignore_classes_are_dropped(conn, cfg, stub_encoder):
    rec = enroll_all(conn, cfg, skus=("coke-330",))
    for i in range(3):
        phone = mat_frame()
        phone[100 + i * 10:260, 200:290] = (20, 20, 20)
        rec.enroll(conn, phone, None, "phone")
    frame = mat_frame()
    paint(frame, "coke-330", 60, 80)
    frame[200:360, 400:490] = (20, 20, 20)
    dets = rec.recognize(conn, frame, "t2")
    assert [d.ignored for d in dets if d.ignored] == ["phone"]
    assert [d.record.pack_id for d in dets if not d.ignored] == [pack_of(conn, "coke-330")]


def test_multipack_counts_its_units(conn, cfg, stub_encoder):
    rec = pipeline.get_recognizer(cfg)
    six = pack_of(conn, "coke-330", 6)
    for i in range(3):
        f = mat_frame()
        paint(f, "coke-330", 150 + 20 * i, 120, 260, 150)  # a wide shrink-wrapped pack
        rec.enroll(conn, f, six)
    frame = mat_frame()
    paint(frame, "coke-330", 180, 140, 260, 150)
    [d] = rec.recognize(conn, frame, "t3")
    assert d.record.pack_id == six and d.record.method == "multipack"


# --- transactions ------------------------------------------------------------------------

def det(pack_id, tier_="high", conf=0.99, crop=None):
    return transactions.DetectionRecord(pack_id, conf, tier_, "image", (0, 0, 10, 10), crop, [(pack_id, conf)])


def test_closing_keeps_the_frame_with_the_most_of_each_product(conn):
    coke, sprite = pack_of(conn, "coke-330"), pack_of(conn, "sprite-1500")
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [det(coke)])                        # first can lands
    transactions.add_frame(conn, txn, None, [det(coke), det(coke), det(sprite)])  # two cans + sprite
    transactions.add_frame(conn, txn, None, [det(sprite)])                      # cans already bagged
    summary = transactions.close_transaction(conn, txn)
    assert summary["items"] == 3
    assert stock.on_hand(conn)[pid(conn, "coke-330")] == -2
    assert stock.on_hand(conn)[pid(conn, "sprite-1500")] == -1


def test_only_confident_items_become_sales_before_a_check(conn):
    coke, tea = pack_of(conn, "coke-330"), pack_of(conn, "ichitan-420")
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [det(coke), det(tea, "medium", 0.8)])
    summary = transactions.close_transaction(conn, txn)
    assert (summary["auto"], summary["pending"]) == (1, 1)
    assert pid(conn, "ichitan-420") not in stock.on_hand(conn)


def test_owner_correction_moves_stock_and_teaches_the_gallery(conn):
    tea, oishi = pack_of(conn, "ichitan-420"), pack_of(conn, "oishi-380")
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [det(tea, "medium", 0.8, crop="crops/x.jpg")])
    transactions.close_transaction(conn, txn)
    [item] = transactions.pending_items(conn)
    learned = []
    transactions.resolve(conn, item["id"], "qty", delta=1)
    result = transactions.resolve(conn, item["id"], "pick", pack_id=oishi, by="Dara",
                                  learn=lambda c, crop, pack, ignore, source: learned.append((crop, pack, ignore, source)))
    assert result["status"] == "corrected"
    assert stock.on_hand(conn)[pid(conn, "oishi-380")] == -2
    assert pid(conn, "ichitan-420") not in stock.on_hand(conn)
    assert learned == [("crops/x.jpg", oishi, None, "correction")]
    # A second tap from another family member's phone changes nothing.
    assert transactions.resolve(conn, item["id"], "yes")["already"]


def test_not_an_item_is_no_sale_and_learns_to_ignore(conn):
    tea = pack_of(conn, "ichitan-420")
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [det(tea, "low", 0.5, crop="crops/y.jpg")])
    transactions.close_transaction(conn, txn)
    [item] = transactions.pending_items(conn)
    learned = []
    transactions.resolve(conn, item["id"], "remove", learn=lambda *a: learned.append(a[1:]))
    assert stock.on_hand(conn) == {}
    assert learned == [("crops/y.jpg", None, "other", "correction")]


def test_end_of_day_accepts_best_guesses(conn):
    tea = pack_of(conn, "ichitan-420")
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [det(tea, "medium", 0.8)])
    transactions.close_transaction(conn, txn)
    transactions.best_guess_all(conn)
    assert stock.on_hand(conn)[pid(conn, "ichitan-420")] == -1
    assert conn.execute("SELECT status FROM transactions WHERE id = ?", (txn,)).fetchone()[0] == "best_guess"


def test_counter_photo_is_deleted_once_the_sale_is_settled(conn, cfg):
    media = cfg.media_dir
    (media / "frames").mkdir(parents=True)
    (media / "frames" / "f.jpg").write_bytes(b"x")
    tea = pack_of(conn, "ichitan-420")
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, "frames/f.jpg", [det(tea, "medium", 0.8)], media)
    transactions.close_transaction(conn, txn, media, delete_photo=True)
    assert (media / "frames" / "f.jpg").exists()          # still needed for the check
    [item] = transactions.pending_items(conn)
    transactions.resolve(conn, item["id"], "yes", media_dir=media, delete_photo=True)
    assert not (media / "frames" / "f.jpg").exists()


def test_a_counter_that_only_ever_saw_a_phone_leaves_no_sale(conn):
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [])
    assert transactions.close_transaction(conn, txn)["empty"]
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_stale_open_transactions_are_closed(conn, cfg):
    from luydek import clock
    from tests.conftest import NOW
    from datetime import timedelta

    clock.freeze(NOW - timedelta(minutes=30))
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [det(pack_of(conn, "coke-330"))])
    clock.freeze(NOW)
    assert transactions.close_stale(conn, 10) == 1
    assert conn.execute("SELECT status FROM transactions WHERE id = ?", (txn,)).fetchone()[0] == "auto"


def test_reference_photo_round_trip(conn, cfg):
    rel = pipeline.save_reference(conn, cfg, jpeg(mat_frame()))
    assert db.get_setting(conn, pipeline.REFERENCE_KEY) == rel
    assert pipeline.load_reference(conn, cfg).shape == (480, 640, 3)


# --- touching items ----------------------------------------------------------------------------

def test_two_touching_cans_are_split_and_each_crop_shows_one(cfg):
    frame = mat_frame()
    cv2.circle(frame, (250, 240), 60, (30, 30, 200), -1)    # red can from above
    cv2.circle(frame, (365, 240), 60, (200, 120, 30), -1)   # blue can touching it
    blobs = segment.segment(frame, mat_frame(), cfg.recognition.segment)
    assert len(blobs) == 2
    left = np.asarray(min(blobs, key=lambda b: b.bbox[0]).crop)
    blue_px = ((left[..., 2] > 150) & (left[..., 0] < 80)).sum()   # RGB crop: blue can = high B, low R
    red_px = ((left[..., 0] > 150) & (left[..., 2] < 80)).sum()
    assert red_px > 20 * max(blue_px, 1)                           # the neighbour was painted out


def test_boxes_overlapping_at_a_corner_are_split(cfg):
    frame = mat_frame()
    paint(frame, "coke-330", 150, 120)
    paint(frame, "dumex-400", 225, 220)                            # overlaps the first box's corner
    assert len(segment.segment(frame, mat_frame(), cfg.recognition.segment)) == 2


def test_one_long_or_necked_item_stays_whole(cfg):
    frame = mat_frame()
    cv2.rectangle(frame, (120, 200), (440, 270), (40, 140, 60), -1)   # a bottle lying down
    cv2.rectangle(frame, (440, 222), (500, 248), (40, 140, 60), -1)   # its neck
    assert len(segment.segment(frame, mat_frame(), cfg.recognition.segment)) == 1


def test_boxes_pushed_edge_to_edge_stay_merged(cfg):
    # Known limit: no neck, no split. A trained detector is the fix, not more thresholds.
    frame = mat_frame()
    cv2.rectangle(frame, (150, 150), (240, 300), (30, 30, 200), -1)
    cv2.rectangle(frame, (240, 150), (330, 300), (200, 120, 30), -1)
    assert len(segment.segment(frame, mat_frame(), cfg.recognition.segment)) == 1


def test_splitting_can_be_switched_off(cfg):
    cfg.recognition.segment.split_touching = False
    frame = mat_frame()
    cv2.circle(frame, (250, 240), 60, (30, 30, 200), -1)
    cv2.circle(frame, (365, 240), 60, (200, 120, 30), -1)
    assert len(segment.segment(frame, mat_frame(), cfg.recognition.segment)) == 1


def test_touching_products_are_recognised_separately(conn, cfg, stub_encoder):
    rec = enroll_all(conn, cfg)
    frame = mat_frame()
    paint(frame, "coke-330", 150, 120)
    paint(frame, "dumex-400", 225, 220)
    found = sorted(d.record.pack_id for d in rec.recognize(conn, frame, "t9"))
    assert found == sorted([pack_of(conn, "coke-330"), pack_of(conn, "dumex-400")])


def test_removed_products_are_never_matched(conn, cfg, stub_encoder):
    rec = enroll_all(conn, cfg, skus=("coke-330", "dumex-400"))
    frame = mat_frame()
    paint(frame, "coke-330", 60, 80)
    assert [d.record.pack_id for d in rec.recognize(conn, frame, "a")] == [pack_of(conn, "coke-330")]

    from luydek.services import catalog
    catalog.archive_product(conn, pid(conn, "coke-330"))
    # The cached gallery notices the removal without a restart.
    found = [d.record.pack_id for d in rec.recognize(conn, frame, "b")]
    assert pack_of(conn, "coke-330") not in found
    assert all(p != pack_of(conn, "coke-330") for d in rec.recognize(conn, frame, "c") for p, _ in d.record.candidates)


def test_a_yes_teaches_the_gallery_but_an_unchecked_guess_does_not(conn):
    tea = pack_of(conn, "ichitan-420")
    for _ in range(2):
        txn = transactions.open_transaction(conn)
        transactions.add_frame(conn, txn, None, [det(tea, "medium", 0.8, crop="crops/z.jpg")])
        transactions.close_transaction(conn, txn)
    first, second = transactions.pending_items(conn)
    learned = []
    learn = lambda *a: learned.append(a[1:])  # noqa: E731
    transactions.resolve(conn, first["id"], "yes", learn=learn)
    assert learned == [("crops/z.jpg", tea, None, "confirmed")]
    transactions.best_guess_all(conn)        # end of day: nobody looked, nothing is learned
    assert len(learned) == 1


def test_learned_photos_are_capped_per_product_and_enrollment_is_kept(conn, cfg, stub_encoder):
    cfg.recognition.learn_cap = 3
    rec = enroll_all(conn, cfg, skus=("coke-330",), shots=2)
    coke = pack_of(conn, "coke-330")
    (cfg.media_dir / "crops").mkdir(parents=True, exist_ok=True)
    for i in range(5):
        frame = mat_frame()
        paint(frame, "coke-330", 0, 0)
        cv2.imwrite(str(cfg.media_dir / "crops" / f"c{i}.jpg"), frame[:140, :100])
        rec.learn(conn, f"crops/c{i}.jpg", coke, None, "confirmed")
    rows = conn.execute("SELECT source, COUNT(*) FROM gallery_images WHERE pack_id = ? GROUP BY source", (coke,)).fetchall()
    assert dict((r[0], r[1]) for r in rows) == {"enrollment": 2, "confirmed": 3}
    kept = {r[0] for r in conn.execute("SELECT crop_path FROM gallery_images WHERE source = 'confirmed'")}
    on_disk = {f"gallery/{f.name}" for f in (cfg.media_dir / "gallery").iterdir()}
    assert kept <= on_disk and len(on_disk) == 5                # the 2 dropped photos left the disk too


# --- colour tie-break -------------------------------------------------------------

BLUE, BLACK, NAVY = (200, 60, 20), (25, 25, 25), (120, 40, 20)  # BGR


def pen(colour, x=200, y=200, w=220, h=26, frame=None):
    """A pen-like bar on the mat: a clear body with a coloured grip, like the real ones."""
    frame = mat_frame() if frame is None else frame
    cv2.rectangle(frame, (x, y), (x + w, y + h), (225, 225, 225), -1)
    cv2.rectangle(frame, (x + w // 3, y), (x + w, y + h), colour, -1)
    return frame


class BlindEncoder:
    """A CLIP stand-in that can't see colour at all: every crop looks the same."""

    def encode_images(self, images):
        return np.tile(np.array([1, 0, 0, 0], np.float32), (len(images), 1))


@pytest.fixture
def blind_encoder():
    encoder_mod.set_encoder(BlindEncoder())
    pipeline.reset()
    yield
    encoder_mod.set_encoder(None)
    pipeline.reset()


def crop_of(frame, cfg):
    return segment.largest(frame, mat_frame(), cfg.recognition.segment).crop


def test_colour_signature_sees_the_item_not_the_mat(cfg):
    blue, blue2, black = (colour_signature(crop_of(f, cfg)) for f in (
        pen(BLUE), pen(BLUE, x=60, y=380, w=300, h=30), pen(BLACK)))
    assert float(blue @ blue2) > 0.9      # same pen, other place and size
    assert float(blue @ black) < 0.7      # blue grip vs black grip


def lookalikes(colours, ignore_score=0.0):
    v = np.array([1, 0, 0, 0], np.float32)
    g = Gallery(np.stack([v] * 4), [("pack", 1), ("pack", 1), ("pack", 2), ("pack", 2)], np.stack(colours))
    m = g.match(v)
    return g, Match(m.pack_id, None, m.score, m.margin, m.candidates, ignore_score)


def test_colour_settles_a_look_alike_tie(cfg):
    blue, black = colour_signature(crop_of(pen(BLUE), cfg)), colour_signature(crop_of(pen(BLACK), cfg))
    g, m = lookalikes([blue, blue, black, black])
    assert m.margin == 0                                     # CLIP alone: a coin toss
    settled = colour_tiebreak(g, m, black, cfg.recognition)
    assert settled.pack_id == 2 and settled.method == "colour"
    assert settled.margin == pytest.approx(1.0)              # nothing else comes close
    # A phone that scores nearly as well still keeps the item from being auto-accepted.
    g, m = lookalikes([blue, blue, black, black], ignore_score=0.99)
    settled = colour_tiebreak(g, m, black, cfg.recognition)
    assert settled.pack_id == 2 and tier(settled.score, settled.margin, cfg.recognition) == "medium"


def test_colour_stays_out_when_it_cannot_tell(cfg):
    blue, navy = colour_signature(crop_of(pen(BLUE), cfg)), colour_signature(crop_of(pen(NAVY), cfg))
    g, m = lookalikes([blue, blue, navy, navy])
    assert colour_tiebreak(g, m, colour_signature(crop_of(pen((170, 50, 20)), cfg)), cfg.recognition) is None
    # No tie, no tie-break: a clear CLIP winner is left alone.
    v = np.eye(4, dtype=np.float32)
    g = Gallery(np.stack([v[0], v[1]]), [("pack", 1), ("pack", 2)], np.stack([blue, navy]))
    assert colour_tiebreak(g, g.match(v[0]), navy, cfg.recognition) is None


def test_pens_that_differ_only_in_colour_are_told_apart(conn, cfg, blind_encoder):
    blue_pen, black_pen = pack_of(conn, "coke-330"), pack_of(conn, "dumex-400")  # stand-ins
    rec = pipeline.get_recognizer(cfg)
    for i in range(3):
        rec.enroll(conn, pen(BLUE, x=120 + 40 * i, y=150 + 60 * i), blue_pen)
        rec.enroll(conn, pen(BLACK, x=100 + 50 * i, y=120 + 70 * i), black_pen)
    for colour, want in ((BLUE, blue_pen), (BLACK, black_pen)):
        (d,) = rec.recognize(conn, pen(colour, x=260, y=300), f"p{want}")
        assert (d.record.pack_id, d.record.method, d.record.tier) == (want, "colour", "high")
        assert d.record.candidates[0][0] == want

    cfg.recognition.colour_tiebreak = False
    pipeline.reset()
    (d,) = pipeline.get_recognizer(cfg).recognize(conn, pen(BLACK, x=260, y=300), "off")
    assert d.record.method == "image" and d.record.tier == "medium"   # a tie goes to the owner


# --- the marked mat and a changed scene ---------------------------------------------

def test_only_items_inside_the_marked_mat_count(cfg):
    frame = mat_frame()
    paint(frame, "coke-330", 40, 60)          # left of the mat: a phone, a hand, the till...
    paint(frame, "dumex-400", 420, 200)
    right_half = (0.5, 0.0, 1.0, 1.0)
    blobs = segment.segment(frame, mat_frame(), cfg.recognition.segment, roi=right_half)
    assert len(blobs) == 1 and blobs[0].bbox[0] >= 320 - 20


def test_a_camera_that_moved_or_new_light_is_not_a_sale(cfg):
    sc = cfg.recognition.segment
    lamp_on = np.clip(mat_frame().astype(np.int16) - 70, 0, 255).astype(np.uint8)
    with pytest.raises(segment.SceneChanged) as err:
        segment.segment(lamp_on, mat_frame(), sc, check_scene=True)
    assert err.value.changed > 0.95
    # Pointing at the wall instead of the mat, with only a sliver of mat left.
    moved = np.full_like(mat_frame(), (40, 90, 140))
    moved[:, :60] = mat_frame()[:, :60]
    with pytest.raises(segment.SceneChanged):
        segment.segment(moved, mat_frame(), sc, check_scene=True)
    # Within the marked mat, only the mat counts: light on the desk around it doesn't.
    beside = mat_frame()
    beside[:, :200] = lamp_on[:, :200]
    assert segment.segment(beside, mat_frame(), sc, roi=(0.4, 0.0, 1.0, 1.0), check_scene=True) == []


def test_a_busy_mat_is_still_a_sale(cfg):
    frame = mat_frame()
    for i, sku in enumerate(("coke-330", "sprite-1500", "ichitan-420", "dumex-400")):
        paint(frame, sku, 20 + i * 160, 40, 110, 400)   # four big items: over half the mat
    blobs = segment.segment(frame, mat_frame(), cfg.recognition.segment, check_scene=True)
    assert len(blobs) == 4


def test_mat_area_round_trip_and_validation(conn):
    assert pipeline.load_roi(conn) is None
    assert pipeline.save_roi(conn, (0.9, 0.8, 0.1, -0.2)) == (0.1, 0.0, 0.9, 0.8)   # sorted and clamped
    assert pipeline.load_roi(conn) == (0.1, 0.0, 0.9, 0.8)
    with pytest.raises(ValueError):
        pipeline.save_roi(conn, (0.1, 0.1, 0.15, 0.9))
    pipeline.save_roi(conn, None)
    assert pipeline.load_roi(conn) is None
