"""Find the items on the counter mat.

The MVP stand-in for a trained product detector: compare the frame with a
photo of the empty mat and take whatever differs as items. It relies on the
counter mat from the hardware kit (plain, matte, one colour), and items that
touch merge into one blob; a YOLO detector replaces this later without
changing anything downstream.

The owner can mark the mat on the Counter page. Only that area is searched,
so hands at the edge, a phone beside the till or the desk behind the mat are
never taken for items. If nearly all of the mat differs from the empty-mat
photo, the camera moved or the light changed: `SceneChanged` is raised
instead of guessing at a sale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

from ..config import SegmentCfg


Roi = tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions of the frame


class SceneChanged(Exception):
    """Most of the mat looks different from the empty-mat photo."""

    def __init__(self, changed: float) -> None:
        super().__init__(f"{changed:.0%} of the mat looks different from the empty-counter photo")
        self.changed = changed


def roi_box(roi: Roi | None, w: int, h: int) -> tuple[int, int, int, int]:
    """The ROI in pixels of a w x h image; the whole image when no ROI is set."""
    if roi is None:
        return 0, 0, w, h
    x0, y0, x1, y1 = roi
    return (int(round(x0 * w)), int(round(y0 * h)), max(int(round(x1 * w)), 1), max(int(round(y1 * h)), 1))


@dataclass
class Blob:
    bbox: tuple[int, int, int, int]   # x, y, w, h in full-resolution pixels
    area_frac: float
    crop: Image.Image
    touches_edge: bool = False        # runs into the edge of the mat: usually a hand or arm reaching in
    # Separate pieces inside this outline (see `split_apart`): maybe several items the gap-filling
    # joined, maybe pieces of one. The recognizer decides by what each piece looks like.
    parts: list["Blob"] = field(default_factory=list)


def decode(data: bytes) -> np.ndarray:
    """JPEG/PNG bytes -> BGR array."""
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("not an image")
    return frame


def synthetic_reference(frame: np.ndarray) -> np.ndarray:
    """An 'empty mat' guessed from the frame's border colour, for uncalibrated photos."""
    h, w = frame.shape[:2]
    band = max(4, min(h, w) // 25)
    border = np.concatenate([
        frame[:band].reshape(-1, 3), frame[-band:].reshape(-1, 3),
        frame[:, :band].reshape(-1, 3), frame[:, -band:].reshape(-1, 3),
    ])
    return np.full_like(frame, np.median(border, axis=0).astype(np.uint8))


def segment(frame: np.ndarray, reference: np.ndarray | None, cfg: SegmentCfg, roi: Roi | None = None,
            check_scene: bool = False, exclude: Roi | None = None) -> list[Blob]:
    """Items on the mat, in reading order.

    `check_scene` (only meaningful with a real empty-mat photo) raises
    `SceneChanged` when `scene_change_frac` or more of the mat differs from it.
    `exclude` is an area never searched: the tablet tray, which is counted on its own.
    """
    h, w = frame.shape[:2]
    if reference is None:
        reference = synthetic_reference(frame)
    elif reference.shape[:2] != (h, w):
        reference = cv2.resize(reference, (w, h), interpolation=cv2.INTER_AREA)

    scale = min(1.0, cfg.work_width / w)
    small = (max(1, int(w * scale)), max(1, int(h * scale)))
    a = cv2.GaussianBlur(cv2.resize(frame, small, interpolation=cv2.INTER_AREA), (5, 5), 0)
    b = cv2.GaussianBlur(cv2.resize(reference, small, interpolation=cv2.INTER_AREA), (5, 5), 0)

    # Distance in LAB with lightness slightly down-weighted: soft shadows only
    # change L, while most packaging also differs from the mat in colour. A
    # white pharmacy box differs from a pale mat in L alone, which is why the
    # kit's mat should be dark and saturated.
    la = cv2.cvtColor(a, cv2.COLOR_BGR2LAB).astype(np.int16)
    lb = cv2.cvtColor(b, cv2.COLOR_BGR2LAB).astype(np.int16)
    diff = np.abs(la - lb)
    dist = cfg.lightness_weight * diff[..., 0] + diff[..., 1] + diff[..., 2]
    mask = (dist > cfg.diff_threshold).astype(np.uint8) * 255
    rx0, ry0, rx1, ry1 = roi_box(roi, *small)
    inside = np.zeros_like(mask)
    inside[ry0:ry1, rx0:rx1] = 255
    if exclude is not None:
        ex0, ey0, ex1, ey1 = roi_box(exclude, *small)
        inside[ey0:ey1, ex0:ex1] = 0
    mask &= inside
    total = max(1, (rx1 - rx0) * (ry1 - ry0))  # areas are shares of the mat, not of the whole view
    if check_scene:
        changed = float((mask[ry0:ry1, rx0:rx1] > 0).mean())
        if changed >= cfg.scene_change_frac:
            raise SceneChanged(changed)

    k = max(3, int(round(small[0] / 160)) | 1)
    # Separate items, before the gap-filling below joins them: hairline seams inside one item
    # (where two printed colours blur into something mat-coloured) are healed first, then thin
    # bridges between items (a keychain strap, a cable) are cut.
    seam = max(3, k // 2 * 2 + 1)
    apart = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((seam, seam), np.uint8))
    apart = cv2.morphologyEx(apart, cv2.MORPH_OPEN, np.ones((k, k), np.uint8)) & inside
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k * 3, k * 3), np.uint8), iterations=2)

    mask &= inside  # closing can bleed a little past the edge of the mat
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mat_rgb = cv2.cvtColor(reference, cv2.COLOR_BGR2RGB)
    blobs = []
    def edge(part: np.ndarray) -> bool:
        px, py, pw, ph = cv2.boundingRect(part)
        return px <= rx0 + 1 or py <= ry0 + 1 or px + pw >= rx1 - 1 or py + ph >= ry1 - 1

    for c in contours:
        area = cv2.contourArea(c) / total
        if not cfg.min_area_frac <= area <= cfg.max_area_frac:
            continue
        blob_mask = np.zeros(mask.shape, np.uint8)
        cv2.drawContours(blob_mask, [c], -1, 255, thickness=cv2.FILLED)
        pieces = split_apart(blob_mask, apart, a, cfg, total) if cfg.split_touching else []
        # A piece can itself be two items touching (a keychain lying on a coaster): split at the neck.
        pieces = [q for p in pieces for q in split_touching(p, a, cfg)]
        if len(pieces) >= 2:
            whole = _crop(blob_mask, False, rgb, mat_rgb, scale, total, cfg)
            whole.touches_edge = edge(blob_mask)
            for piece in pieces:
                p = _crop(piece, True, rgb, mat_rgb, scale, total, cfg)
                p.touches_edge = edge(piece)
                whole.parts.append(p)
            blobs.append(whole)
            continue
        parts = split_touching(blob_mask, a, cfg) if cfg.split_touching else [blob_mask]
        for part in parts:
            blob = _crop(part, len(parts) > 1, rgb, mat_rgb, scale, total, cfg)
            blob.touches_edge = edge(part)
            blobs.append(blob)
    blobs.sort(key=lambda bl: (bl.bbox[1] // 50, bl.bbox[0]))  # reading order, for stable numbering
    return blobs


def split_apart(blob_mask: np.ndarray, apart: np.ndarray, image: np.ndarray, cfg: SegmentCfg,
                total: int) -> list[np.ndarray]:
    """Pieces of one outline that were apart before the gap-filling: maybe separate items.

    Gap-filling closes cracks inside one product (a clear pen body, a label the colour of
    the mat), but it also bridges items a finger's width apart, or joined by a strap or a
    light stripe on the mat. Every piece that was on its own before the filling, and is big
    enough to be an item, seeds a watershed that shares the outline out between them. Whether
    they are several items or pieces of one is left to the recognizer, which knows what each
    piece looks like (`Blob.parts`). Returns [] when there is nothing to split.
    """
    inner = cv2.bitwise_and(apart, blob_mask)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(inner)
    size = int((blob_mask > 0).sum())
    floor = max(cfg.split_min_frac * size, cfg.min_area_frac * total)
    keep = [k for k in range(1, n) if stats[k, cv2.CC_STAT_AREA] >= floor]
    if len(keep) < 2:
        return []
    markers = np.zeros(blob_mask.shape, np.int32)
    markers[blob_mask == 0] = 1
    for j, k in enumerate(keep):
        markers[labels == k] = j + 2
    cv2.watershed(image, markers)
    parts = []
    for j in range(len(keep)):
        part = ((markers == j + 2) & (blob_mask > 0)).astype(np.uint8) * 255
        cs, _ = cv2.findContours(part, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        filled = np.zeros_like(part)
        cv2.drawContours(filled, cs, -1, 255, thickness=cv2.FILLED)  # each item whole again
        parts.append(filled)
    return parts


def split_touching(blob_mask: np.ndarray, image: np.ndarray, cfg: SegmentCfg) -> list[np.ndarray]:
    """Split one blob where two items touch, at the narrow neck between them.

    Only a clearly dented outline is considered (solidity below `split_solidity`):
    a convex blob's distance map has a single peak region, so it is always one
    item. In a dented blob the distance transform peaks in the middle of each
    item and dips where two items meet; cores above `split_peak` x the highest
    peak seed a watershed on the image, which draws the line between them. Two
    boxes pushed edge to edge into one rectangle have no dent and are not split:
    that needs a trained detector.
    """
    # A convex blob is one item, whatever its size: only a dented outline can be two.
    contours, _ = cv2.findContours(blob_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    hull_area = cv2.contourArea(cv2.convexHull(contours[0])) if contours else 0
    if not hull_area or cv2.contourArea(contours[0]) / hull_area >= cfg.split_solidity:
        return [blob_mask]
    dist = cv2.distanceTransform(blob_mask, cv2.DIST_L2, 5)
    peak = float(dist.max())
    if peak <= 0:
        return [blob_mask]
    cores = (dist >= cfg.split_peak * peak).astype(np.uint8) * 255
    n, markers = cv2.connectedComponents(cores)
    if n <= 2:  # label 0 is everything outside the cores; one core means one item
        return [blob_mask]
    markers = markers.astype(np.int32) + 1          # cores become 2..n, the rest 1
    markers[(blob_mask > 0) & (cores == 0)] = 0      # the watershed decides these pixels
    cv2.watershed(image, markers)
    size = int((blob_mask > 0).sum())
    parts = [(markers == k).astype(np.uint8) * 255 for k in range(2, n + 1)]
    parts = [pt for pt in parts if (pt > 0).sum() >= cfg.split_min_frac * size]
    return parts if len(parts) >= 2 else [blob_mask]


def _crop(part: np.ndarray, was_split: bool, rgb: np.ndarray, mat_rgb: np.ndarray, scale: float, total: int,
          cfg: SegmentCfg) -> Blob:
    h, w = rgb.shape[:2]
    x, y, bw, bh = cv2.boundingRect(part)
    area = round(float((part > 0).sum()) / total, 4)
    x, y, bw, bh = (int(v / scale) for v in (x, y, bw, bh))
    pad = int(max(bw, bh) * cfg.pad_frac)
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    crop = rgb[y0:y1, x0:x1].copy()
    if was_split:
        # Paint the neighbouring item out with the mat, so the crop shows one product.
        full = cv2.resize(part, (w, h), interpolation=cv2.INTER_NEAREST)[y0:y1, x0:x1]
        full = cv2.dilate(full, np.ones((max(3, pad // 2) | 1,) * 2, np.uint8))
        crop[full == 0] = mat_rgb[y0:y1, x0:x1][full == 0]
    return Blob((x0, y0, x1 - x0, y1 - y0), area, Image.fromarray(crop))


def largest(frame: np.ndarray, reference: np.ndarray | None, cfg: SegmentCfg, roi: Roi | None = None,
            check_scene: bool = False, exclude: Roi | None = None, avoid_edge: bool = False) -> Blob | None:
    """The biggest item on the mat (enrollment: one product at a time).

    `avoid_edge` passes over anything running into the edge of the mat: while
    photos are taken in a burst, that is the hand turning the product.
    """
    blobs = segment(frame, reference, cfg, roi, check_scene, exclude)
    if avoid_edge:
        blobs = [bl for bl in blobs if not bl.touches_edge]
    return max(blobs, key=lambda bl: bl.area_frac) if blobs else None
