"""Find the items on the counter mat.

The MVP stand-in for a trained product detector: compare the frame with a
photo of the empty mat and take whatever differs as items. It relies on the
counter mat from the hardware kit (plain, matte, one colour), and items that
touch merge into one blob; a YOLO detector replaces this later without
changing anything downstream.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from ..config import SegmentCfg


@dataclass
class Blob:
    bbox: tuple[int, int, int, int]   # x, y, w, h in full-resolution pixels
    area_frac: float
    crop: Image.Image


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


def segment(frame: np.ndarray, reference: np.ndarray | None, cfg: SegmentCfg) -> list[Blob]:
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

    k = max(3, int(round(small[0] / 160)) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k * 3, k * 3), np.uint8), iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    total = small[0] * small[1]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    mat_rgb = cv2.cvtColor(reference, cv2.COLOR_BGR2RGB)
    blobs = []
    for c in contours:
        area = cv2.contourArea(c) / total
        if not cfg.min_area_frac <= area <= cfg.max_area_frac:
            continue
        blob_mask = np.zeros(mask.shape, np.uint8)
        cv2.drawContours(blob_mask, [c], -1, 255, thickness=cv2.FILLED)
        parts = split_touching(blob_mask, a, cfg) if cfg.split_touching else [blob_mask]
        for part in parts:
            blobs.append(_crop(part, len(parts) > 1, rgb, mat_rgb, scale, total, cfg))
    blobs.sort(key=lambda bl: (bl.bbox[1] // 50, bl.bbox[0]))  # reading order, for stable numbering
    return blobs


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


def largest(frame: np.ndarray, reference: np.ndarray | None, cfg: SegmentCfg) -> Blob | None:
    """The biggest item on the mat (enrollment: one product at a time)."""
    blobs = segment(frame, reference, cfg)
    return max(blobs, key=lambda bl: bl.area_frac) if blobs else None
