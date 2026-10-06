"""Shadows and changes in the light are not items; dark items with sharp outlines still are."""

import cv2
import numpy as np
import pytest

from luydek.config import SegmentCfg
from luydek.recognition import pipeline, segment
from luydek.recognition.detector import Found
from tests.conftest import MAT, jpeg, pack_of, paint
from tests.test_detector import FakeDetector
from tests.test_recognition import enroll_all

SEG = SegmentCfg(work_width=640, min_area_frac=0.004)


def mat(w=640, h=480, seed=1):
    """The counter mat with a little grain, like real fabric under a webcam."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 3, (h, w, 1))
    return np.clip(np.full((h, w, 3), MAT, np.float32) + noise, 0, 255).astype(np.uint8)


def shadow(img, centre=(320, 240), axes=(150, 70), light=0.6):
    """A soft shadow, as cast by an arm or a bag: darker by `light`, fading out at its edge, a little bluer."""
    m = np.zeros(img.shape[:2], np.float32)
    cv2.ellipse(m, centre, axes, 20, 0, 360, 1.0, -1)
    m = cv2.GaussianBlur(m, (0, 0), 14)[..., None]
    shade = light * np.array([1.04, 1.0, 0.97], np.float32)   # BGR
    return np.clip(img * (1 - m * (1 - shade)), 0, 255).astype(np.uint8)


def found(frame, reference, cfg=SEG):
    return segment.segment(frame, reference, cfg, check_scene=True)


@pytest.mark.parametrize("light", [0.75, 0.6, 0.45])
def test_a_shadow_on_the_mat_is_not_an_item(light):
    empty = mat()
    assert found(shadow(empty, light=light), empty) == []
    assert found(shadow(empty, light=light), empty, SEG.model_copy(update={"shadow_filter": False}))  # it was one


def test_a_shadow_that_has_gone_is_not_an_item():
    empty = mat()
    assert found(empty, shadow(empty)) == []          # the empty-mat photo had the shadow; the mat is lit now


def test_a_dark_grey_box_on_the_mat_is_still_an_item():
    """Plain, the mat's own colour, only darker, like a shadow: but with a sharp outline."""
    empty, frame = mat(), mat(seed=2)
    frame[150:330, 250:380] = (np.array(MAT) * 0.55).astype(np.uint8)
    [blob] = found(frame, empty)
    x, y, w, h = blob.bbox
    assert abs(x - 250) < 25 and abs(w - 130) < 40


def test_an_item_next_to_a_shadow_is_found_and_the_shadow_is_not():
    empty = mat()
    frame = shadow(mat(seed=3), centre=(470, 330), axes=(120, 60))
    paint(frame, "coke-330", 80, 80)
    [blob] = found(frame, empty)
    assert blob.bbox[0] < 120 and blob.bbox[1] < 120


def test_the_shadow_an_item_casts_beside_it_is_not_another_item(conn, cfg, stub_encoder):
    """With the detector: the item is boxed, and the shadow beside it (cut off by the box) stays a shadow."""
    empty = mat()
    pipeline.save_reference(conn, cfg, jpeg(empty))
    enroll_all(conn, cfg, skus=("coke-330",))
    frame = shadow(mat(seed=4), centre=(260, 260), axes=(90, 110), light=0.55)  # cast down-right of the can
    paint(frame, "coke-330", 120, 90)
    rec = pipeline.get_recognizer(cfg)
    rec.finder = FakeDetector([Found((120, 90, 210, 220), .9)])
    try:
        got = rec.recognize(conn, frame, "s")
    finally:
        rec.finder = None
    assert [d.record.pack_id for d in got] == [pack_of(conn, "coke-330")]


def test_the_whole_mat_darker_is_still_a_changed_scene():
    """Lights turned down, or the camera moved: still asks for a recalibration, as before."""
    empty = mat()
    with pytest.raises(segment.SceneChanged):
        found((empty * 0.5).astype(np.uint8), empty)
