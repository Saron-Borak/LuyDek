"""Finding items with a detector (YOLOE-26): which of its boxes are items, and how it works with the
mat difference. A fake detector stands in for the model, so these run without it."""

import cv2
import numpy as np
import pytest

from luydek.config import DetectorCfg, SegmentCfg
from luydek.recognition import detector, pipeline
from luydek.recognition.detector import Found
from tests.conftest import mat_frame, pack_of, paint
from tests.test_recognition import enroll_all

ROI = (0, 0, 1000, 1000)
SEG = SegmentCfg(min_area_frac=0.004, max_area_frac=0.6)
CFG = DetectorCfg()


def boxes(found):
    return sorted(f.box for f in detector.tidy(found, ROI, None, CFG, SEG))


def test_off_the_mat_on_the_tray_specks_and_the_whole_scene_are_not_items():
    found = [Found((100, 100, 200, 300), .9), Found((1100, 100, 1200, 300), .9),   # off the mat
             Found((800, 800, 900, 900), .9),                                       # on the tablet tray
             Found((10, 10, 30, 30), .9),                                           # a speck
             Found((0, 0, 1000, 990), .98)]                                         # the whole view
    assert [f.box for f in detector.tidy(found, ROI, (750, 750, 1000, 1000), CFG, SEG)] == [(100, 100, 200, 300)]


def test_parts_of_an_item_are_the_item():
    bottle = Found((100, 100, 200, 500), .92)
    label, cap, bottom = Found((110, 300, 190, 400), .81), Found((130, 100, 170, 140), .59), Found((101, 380, 199, 499), .27)
    assert boxes([label, bottle, cap, bottom]) == [bottle.box]


def test_a_box_around_items_side_by_side_is_not_an_item():
    left, right = Found((100, 100, 200, 400), .89), Found((210, 100, 310, 400), .82)
    group = Found((95, 95, 315, 405), .11)
    assert boxes([group, left, right]) == [left.box, right.box]
    # ...but a whole item surer than the pieces of its label stays one item
    assert boxes([Found((95, 95, 315, 405), .95), left, right]) == [(95, 95, 315, 405)]


def test_the_same_object_under_two_names_is_one_item():
    assert boxes([Found((100, 100, 200, 400), .7, "carton"), Found((105, 110, 210, 400), .9, "can")]) == [(105, 110, 210, 400)]


class FakeDetector:
    name = "fake"

    def __init__(self, found):
        self.found = found

    def detect(self, frame):
        return list(self.found)


@pytest.fixture
def calibrated(conn, cfg):
    from tests.conftest import jpeg
    pipeline.save_reference(conn, cfg, jpeg(mat_frame()))
    return conn


def test_touching_items_the_mat_merges_are_told_apart(calibrated, cfg, stub_encoder):
    conn = calibrated
    enroll_all(conn, cfg, skus=("coke-330", "sprite-1500"))
    frame = mat_frame()
    paint(frame, "coke-330", 100, 100)                     # touching: one blob to the mat difference
    paint(frame, "sprite-1500", 190, 100)
    rec = pipeline.get_recognizer(cfg)
    alone = rec.recognize(conn, frame, "m")
    rec.finder = FakeDetector([Found((100, 100, 190, 230), .9), Found((190, 100, 280, 230), .9)])
    try:
        found = rec.recognize(conn, frame, "y")
    finally:
        rec.finder = None
    assert len(found) == 2 and {d.record.pack_id for d in found} == {pack_of(conn, "coke-330"), pack_of(conn, "sprite-1500")}
    assert len(alone) <= 2


def test_what_the_detector_misses_is_still_an_item(calibrated, cfg, stub_encoder):
    conn = calibrated
    enroll_all(conn, cfg, skus=("coke-330", "dumex-400"))
    frame = mat_frame()
    paint(frame, "coke-330", 100, 100)
    paint(frame, "dumex-400", 400, 250)                    # the detector doesn't box this one
    rec = pipeline.get_recognizer(cfg)
    rec.finder = FakeDetector([Found((100, 100, 190, 230), .9)])
    try:
        found = rec.recognize(conn, frame, "y")
    finally:
        rec.finder = None
    assert sorted(d.record.pack_id for d in found) == sorted([pack_of(conn, "coke-330"), pack_of(conn, "dumex-400")])


def test_a_box_on_bare_mat_is_not_an_item(calibrated, cfg, stub_encoder):
    conn = calibrated
    enroll_all(conn, cfg, skus=("coke-330",))
    frame = mat_frame()
    paint(frame, "coke-330", 100, 100)
    rec = pipeline.get_recognizer(cfg)
    rec.finder = FakeDetector([Found((100, 100, 190, 230), .9), Found((400, 250, 500, 380), .8)])  # 2nd: mat texture
    try:
        found = rec.recognize(conn, frame, "y")
    finally:
        rec.finder = None
    assert [d.record.pack_id for d in found] == [pack_of(conn, "coke-330")]


def test_no_weights_means_the_mat_difference_alone(cfg, tmp_path):
    detector.reset()
    cfg.recognition.detector = DetectorCfg(enabled=True, weights=str(tmp_path / "missing.pt"))
    try:
        assert detector.get_detector(cfg) is None
    finally:
        detector.reset()
    cfg.recognition.detector = DetectorCfg(enabled=False)
    assert detector.get_detector(cfg) is None


def test_crops_paint_out_a_neighbour_with_the_mat():
    frame = mat_frame(400, 300)
    frame[50:250, 50:150] = (0, 0, 255)                    # item A, red
    frame[50:250, 150:250] = (255, 0, 0)                   # item B, blue, touching A
    found = [Found((50, 50, 150, 250), .9, polygon=np.array([[50, 50], [149, 50], [149, 249], [50, 249]], float)),
             Found((150, 50, 250, 250), .9, polygon=np.array([[150, 50], [249, 50], [249, 249], [150, 249]], float))]
    blue = lambda crop: int(((crop[..., 2] > 200) & (crop[..., 0] < 50)).sum())  # noqa: E731 - RGB
    plain = np.asarray(frame[30:270, 34:166, ::-1])                       # A's crop area as photographed (RGB)
    a, b = detector.blobs(frame, found, (0, 0, 400, 300), mat_frame(400, 300), SEG)
    crop_a = np.asarray(a.crop)
    assert blue(crop_a) < 0.4 * blue(plain)                              # B painted out, but for A's own margin
    assert ((crop_a[..., 0] > 200) & (crop_a[..., 2] < 50)).any()       # A itself is there
    assert b.bbox[0] < 150 and a.area_frac > 0


def rect(x0, y0, x1, y1):
    return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], float)


def diagonal(cx, cy, length, width, angle):
    """The outline of a long item (a bottle, a pen) lying at an angle."""
    return cv2.boxPoints(((cx, cy), (length, width), angle)).astype(float)


def test_a_small_item_in_the_corner_of_a_diagonal_items_box_is_its_own_item():
    """Their boxes overlap; their outlines don't."""
    bottle = diagonal(400, 400, 600, 120, 45)
    xs, ys = bottle[:, 0], bottle[:, 1]
    box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    sweet = Found((560, 200, 650, 290), .9, polygon=rect(560, 200, 650, 290))   # in the box's empty corner
    got = detector.tidy([Found(box, .8, polygon=bottle), sweet], ROI, None, CFG, SEG)
    assert len(got) == 2
    # with boxes only, the sweet would have been taken for a part of the bottle
    assert len(detector.tidy([Found(box, .8), Found(sweet.box, .9)], ROI, None, CFG, SEG)) == 1


def test_a_picture_printed_on_a_bag_is_part_of_the_bag_even_when_cut_out_of_its_outline():
    bag = np.array([[100, 100], [500, 100], [500, 600], [100, 600], [100, 380], [260, 380], [260, 260], [100, 260]], float)
    picture = Found((100, 260, 260, 380), .93, polygon=rect(100, 260, 260, 380))   # sits in the notch
    assert boxes([Found((100, 100, 500, 600), .96, polygon=bag), picture]) == [(100, 100, 500, 600)]

