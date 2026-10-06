"""Find each item on the mat with YOLOE-26 (Ultralytics YOLO26, open vocabulary, prompt-free).

The mat difference (segment.py) sees *that* something is on the mat, but two cans standing side by
side are one blob to it, and off the counter camera (a phone, no empty-mat photo) it can only guess
the background. A detector sees each object. Its own names for them are no use here (a Sting can is
a "zombie" to it): CLIP and the shop's photos still say which product each one is.

What the model returns is tidied before use, by each object's outline (the model draws one around
everything it finds), not its box. Boxes of items close together overlap even when the items don't:
two cans side by side at an angle, a sweet in the corner of a pen's box. Outlines don't:

* only objects inside the marked mat, and not on the tablet tray (it is counted on its own);
* nothing the size of the whole view (it outlines the scene too) and no specks of mat texture;
* an object within a bigger one's outline (with the outline's dents filled in: the model may cut a
  picture printed on a bag out of the bag's outline) is a part of it (a label, a cap, that picture),
  unless the bigger one holds two or more items side by side that the detector is surer of than of
  the bigger one: then it is an outline around a group, and the items stay. Something in the box
  but not within the outline (a sweet in the corner of a pen's box) is an item of its own;
* on the counter camera, only where the empty-mat photo shows something is really there, under the
  outline (not anywhere in its box).

Whatever the mat difference finds that no box covers is still an item (see pipeline.py), so a
product the detector doesn't see as an object is never lost.

Ultralytics YOLO is AGPL-3.0: offering LuyDek to shops as a service means publishing its source,
or an Ultralytics Enterprise licence.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from PIL import Image

from ..config import Config, DetectorCfg, SegmentCfg
from . import segment

log = logging.getLogger("luydek.detector")

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in full-resolution pixels


@dataclass
class Found:
    box: Box
    conf: float
    name: str = ""                      # the detector's own word for it: for logs, never for the product
    polygon: np.ndarray | None = None   # its outline (N, 2) in full-resolution pixels, when the model has masks


class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> list[Found]: ...


class YoloDetector:
    def __init__(self, weights: Path, cfg: DetectorCfg) -> None:
        from ultralytics import YOLO, YOLOE, settings
        settings.update({"sync": False})  # no usage analytics from the shop's computer
        self.cfg = cfg
        self.name = weights.name
        self.model = (YOLOE if "yoloe" in weights.name.lower() else YOLO)(str(weights))
        self._lock = threading.Lock()  # one prediction at a time: the predictor keeps state

    def detect(self, frame: np.ndarray) -> list[Found]:
        with self._lock:
            res = self.model.predict(frame, imgsz=self.cfg.imgsz, conf=self.cfg.conf, max_det=self.cfg.max_det,
                                     verbose=False)[0]
        if res.boxes is None or not len(res.boxes):
            return []
        boxes = res.boxes.xyxy.cpu().numpy()
        confs = res.boxes.conf.cpu().numpy()
        classes = res.boxes.cls.cpu().numpy().astype(int)
        polygons = [None] * len(boxes)
        if res.masks is not None:
            # Each object's outline is its biggest piece: a stray fleck of its mask on a neighbour (joined
            # to it by a thin line, the way Ultralytics' own outlines join pieces) would swallow that
            # neighbour into the object.
            from ultralytics.utils import ops
            polygons = [ops.scale_coords(res.masks.data.shape[1:], seg, res.masks.orig_shape, normalize=False)
                        for seg in ops.masks2segments(res.masks.data, strategy="largest")]
        return [Found(tuple(int(round(v)) for v in b), float(c), str(res.names.get(int(k), k)),
                      p if p is not None and len(p) >= 3 else None)
                for b, c, k, p in zip(boxes, confs, classes, polygons, strict=True)]


def _area(b: Box) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _overlap(a: Box, b: Box) -> int:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


OUTLINE_WIDTH = 640  # outlines are compared at this width: plenty to see where one item ends and the next begins


class Shapes:
    """Each object's outline as a small mask over its own box, at a working scale (`OUTLINE_WIDTH`).
    An object without an outline (a model that only draws boxes) is its box."""

    def __init__(self, found: list[Found], scale: float) -> None:
        self.scale = scale
        self.boxes: list[Box] = []
        self.masks: list[np.ndarray] = []
        for f in found:
            x0, y0 = int(f.box[0] * self.scale), int(f.box[1] * self.scale)
            x1, y1 = max(x0 + 1, int(np.ceil(f.box[2] * self.scale))), max(y0 + 1, int(np.ceil(f.box[3] * self.scale)))
            m = np.zeros((y1 - y0, x1 - x0), np.uint8)
            if f.polygon is not None:
                cv2.fillPoly(m, [np.round(f.polygon * self.scale - (x0, y0)).astype(np.int32)], 1)
            if not m.any():
                m[:] = 1
            self.boxes.append((x0, y0, x1, y1))
            self.masks.append(m.astype(bool))
        self.areas = [int(m.sum()) for m in self.masks]
        # Each outline with its dents and notches filled in (its convex hull): the model sometimes cuts a
        # picture printed on a bag out of the bag's outline, but the picture is still within the bag.
        self.hulls = []
        for m in self.masks:
            pts = cv2.findNonZero(m.astype(np.uint8))
            hull = np.zeros(m.shape, np.uint8)
            if pts is not None:
                cv2.fillConvexPoly(hull, cv2.convexHull(pts), 1)
            self.hulls.append(hull.astype(bool) | m)

    def overlap(self, i: int, j: int) -> int:
        """Pixels inside both outlines."""
        a, b = self.boxes[i], self.boxes[j]
        x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
        if x1 <= x0 or y1 <= y0:
            return 0
        mi = self.masks[i][y0 - a[1]:y1 - a[1], x0 - a[0]:x1 - a[0]]
        mj = self.masks[j][y0 - b[1]:y1 - b[1], x0 - b[0]:x1 - b[0]]
        return int((mi & mj).sum())

    def within_hull(self, i: int, j: int) -> int:
        """Pixels of outline i inside outline j with j's dents filled in."""
        a, b = self.boxes[i], self.boxes[j]
        x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
        if x1 <= x0 or y1 <= y0:
            return 0
        mi = self.masks[i][y0 - a[1]:y1 - a[1], x0 - a[0]:x1 - a[0]]
        hj = self.hulls[j][y0 - b[1]:y1 - b[1], x0 - b[0]:x1 - b[0]]
        return int((mi & hj).sum())

    def iou(self, i: int, j: int) -> float:
        inter = self.overlap(i, j)
        return inter / max(1, self.areas[i] + self.areas[j] - inter)

    def within(self, i: int, box: Box) -> int:
        """Pixels of an outline inside a box given in full-size pixels (the mat)."""
        b, s = self.boxes[i], self.scale
        x0, y0 = max(b[0], int(box[0] * s)), max(b[1], int(box[1] * s))
        x1, y1 = min(b[2], int(np.ceil(box[2] * s))), min(b[3], int(np.ceil(box[3] * s)))
        if x1 <= x0 or y1 <= y0:
            return 0
        return int(self.masks[i][y0 - b[1]:y1 - b[1], x0 - b[0]:x1 - b[0]].sum())


def tidy(found: list[Found], roi: Box, exclude: Box | None, cfg: DetectorCfg, seg: SegmentCfg,
         width: int | None = None) -> list[Found]:
    """The items among the detector's outlines (see the module notes), biggest first. `width`: of the
    photo (the mat's right edge when not given)."""
    shapes = Shapes(found, min(1.0, OUTLINE_WIDTH / max(1, width or roi[2])))
    mat = max(1, _area(roi)) * shapes.scale ** 2
    candidates = []
    for i, f in enumerate(found):
        cx, cy = (f.box[0] + f.box[2]) / 2, (f.box[1] + f.box[3]) / 2
        if not (roi[0] <= cx <= roi[2] and roi[1] <= cy <= roi[3]):
            continue                                          # off the mat
        if exclude is not None and exclude[0] <= cx <= exclude[2] and exclude[1] <= cy <= exclude[3]:
            continue                                          # on the tablet tray
        if not seg.min_area_frac <= shapes.within(i, roi) / mat <= seg.max_area_frac:
            continue                                          # a speck, or the whole scene
        candidates.append(i)
    keep = sorted(candidates, key=lambda i: shapes.areas[i], reverse=True)

    def inside(small: int, big: int) -> bool:
        # A part (a label, a cap, a picture printed on a bag) lies within the bigger object's outline,
        # once the outline's dents are filled in (the model may cut the picture out of the bag's
        # outline). A sweet in the corner of a pen's box is in the box but not within the pen.
        return (shapes.areas[small] < shapes.areas[big]
                and shapes.within_hull(small, big) >= cfg.part_of * shapes.areas[small])

    dropped: set[int] = set()
    for big in keep:
        if big in dropped:
            continue
        within = [j for j in keep if j != big and j not in dropped and inside(j, big)]
        top = [j for j in within if not any(inside(j, k) for k in within if k != j)]
        items = [j for j in top if shapes.areas[j] >= cfg.group_min * shapes.areas[big]]
        # An outline around a group is a weaker guess than the items in it (0.11 around two cans of 0.89
        # and 0.82); a whole bottle is surer than the pieces of its label (0.92 around 0.81 and 0.27).
        if (len(items) >= 2 and all(found[j].conf > found[big].conf for j in items)
                and all(shapes.iou(a, b) < cfg.apart_iou for a, b in combinations(items, 2))):
            dropped.add(big)          # around several items side by side: keep the items
        else:
            dropped.update(within)    # parts of one item: keep the item
    out: list[int] = []
    for i in keep:                    # the same object twice under two names: keep the surer one
        if i in dropped:
            continue
        twin = next((o for o in out if shapes.iou(o, i) >= cfg.same_iou), None)
        if twin is None:
            out.append(i)
        elif found[i].conf > found[twin].conf:
            out[out.index(twin)] = i
    return [found[i] for i in out]


def outline_mask(found: list[Found], w: int, h: int, grow: int = 0) -> np.ndarray:
    """All outlines filled in, full size (0/255), each widened by `grow` pixels."""
    m = np.zeros((h, w), np.uint8)
    for f in found:
        if f.polygon is not None:
            cv2.fillPoly(m, [np.round(f.polygon).astype(np.int32)], 255)
        else:
            m[f.box[1]:f.box[3], f.box[0]:f.box[2]] = 255
    if grow > 0:
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
    return m


def blobs(frame: np.ndarray, found: list[Found], roi: Box, mat: np.ndarray, seg: SegmentCfg) -> list[segment.Blob]:
    """Crops for the pipeline, made the way segment.py makes them (the box plus a margin). Where a
    neighbour's outline reaches into an item's crop, it is painted out with the mat (the empty-mat
    photo, or its colour guessed from the edges of the view), so each crop shows one product."""
    h, w = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mat_rgb = cv2.cvtColor(mat, cv2.COLOR_BGR2RGB)
    total = max(1, _area(roi))
    masks = []
    for f in found:
        m = np.zeros((h, w), np.uint8)
        if f.polygon is not None:
            cv2.fillPoly(m, [np.round(f.polygon).astype(np.int32)], 255)
        else:
            m[f.box[1]:f.box[3], f.box[0]:f.box[2]] = 255
        masks.append(m)
    edge = max(2, int(0.01 * w))
    out = []
    for i, f in enumerate(found):
        x0, y0, x1, y1 = f.box
        pad = int(max(x1 - x0, y1 - y0) * seg.pad_frac)
        cx0, cy0, cx1, cy1 = padded(f.box, w, h, seg)
        crop = rgb[cy0:cy1, cx0:cx1].copy()
        if len(found) > 1:
            own = cv2.dilate(masks[i][cy0:cy1, cx0:cx1], np.ones((max(3, pad // 2) | 1,) * 2, np.uint8))
            others = np.zeros_like(own)
            for j, m in enumerate(masks):
                if j != i:
                    others |= m[cy0:cy1, cx0:cx1]
            paint = (others > 0) & (own == 0)
            crop[paint] = mat_rgb[cy0:cy1, cx0:cx1][paint]
        blob = segment.Blob((cx0, cy0, cx1 - cx0, cy1 - cy0), round(float((masks[i] > 0).sum()) / total, 4),
                            Image.fromarray(crop))
        blob.touches_edge = x0 <= roi[0] + edge or y0 <= roi[1] + edge or x1 >= roi[2] - edge or y1 >= roi[3] - edge
        out.append(blob)
    return out


def something_there(frame: np.ndarray, reference: np.ndarray, found: list[Found], seg: SegmentCfg,
                    need: float) -> list[Found]:
    """Objects where the empty-mat photo shows a real change under their outline: one on bare mat is
    its texture or print (or a shadow, which segment.changed leaves out)."""
    h, w = frame.shape[:2]
    if reference.shape[:2] != (h, w):
        reference = cv2.resize(reference, (w, h), interpolation=cv2.INTER_AREA)
    changed = segment.changed(frame, reference, seg)
    ch, cw = changed.shape
    shapes = Shapes(found, cw / w)   # outlines at the size the change is worked out at
    out = []
    for i, f in enumerate(found):
        x0, y0, x1, y1 = shapes.boxes[i]
        x1c, y1c = min(x1, cw), min(y1, ch)
        under = shapes.masks[i][:max(0, y1c - y0), :max(0, x1c - x0)]
        if under.any() and float(changed[y0:y1c, x0:x1c][under].mean()) >= need:
            out.append(f)
    return out


def padded(box: Box, w: int, h: int, seg: SegmentCfg) -> Box:
    """The box with the crop margin around it, inside the frame."""
    x0, y0, x1, y1 = box
    pad = int(max(x1 - x0, y1 - y0) * seg.pad_frac)
    return max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)


# --- the shared detector ---------------------------------------------------------------------------

_lock = threading.Lock()
_detector: Detector | None = None
_tried = False


def get_detector(cfg: Config) -> Detector | None:
    """The detector, loaded once; None when it is switched off or can't load (then the mat
    difference finds items on its own, as before)."""
    global _detector, _tried
    dc = cfg.recognition.detector
    if not dc.enabled:
        return None
    with _lock:
        if _detector is None and not _tried:
            _tried = True
            weights = cfg.resolve(dc.weights)
            if not weights.exists():
                log.warning("detector weights %s not found: finding items by the mat difference only", weights)
                return None
            try:
                det = YoloDetector(weights, dc)
                det.detect(np.zeros((480, 640, 3), np.uint8))  # the first prediction is slow: not at a sale
            except Exception as exc:  # noqa: BLE001 - any failure here means "no detector", never "no sales"
                log.warning("detector %s didn't load (%s): finding items by the mat difference only", weights, exc)
                return None
            _detector = det
            log.info("finding items with %s", weights.name)
        return _detector


def set_detector(detector: Detector | None) -> None:
    """For tests: use this detector (None: none at all)."""
    global _detector, _tried
    _detector, _tried = detector, True


def reset() -> None:
    global _detector, _tried
    _detector, _tried = None, False
