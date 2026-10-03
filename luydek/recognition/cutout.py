"""Product photos cut out of their background.

Two cuts, for two jobs:

* `cutout` - fast and simple: the biggest region unlike the photo's border (the
  mat). Good enough for the simulated counter test, which lays real products on
  a fresh counter.
* `on_white` - for the thumbnails people look at. GrabCut from a centred box,
  then checks that the cut is believable: one solid piece, not running along
  the box edge, and its outline following real edges in the photo. A cut that
  fails any check returns None and the plain photo is shown instead, so a bad
  cut-out (a pen plus a slab of table) never reaches the screen.
"""

from __future__ import annotations

import cv2
import numpy as np

WORK_SIDE = 256        # GrabCut runs on a copy this size: about 0.2 s a photo, done once and cached
MIN_ALIGN = 0.85       # share of the outline on a real edge in the photo (a pen plus table slab scores ~0.8)
EDGE_STRENGTH = 60     # Sobel magnitude that counts as "a real edge"


def cutout(rgb: np.ndarray) -> np.ndarray | None:
    """RGBA of the object in a gallery photo, cropped to it; None if nothing stands out."""
    h, w = rgb.shape[:2]
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.int16)
    b = max(3, min(h, w) // 25)
    border = np.concatenate([lab[:b].reshape(-1, 3), lab[-b:].reshape(-1, 3),
                             lab[:, :b].reshape(-1, 3), lab[:, -b:].reshape(-1, 3)])
    dist = (np.abs(lab - np.median(border, axis=0)) * (0.8, 1, 1)).sum(axis=2)
    mask = (dist > 30).astype(np.uint8)
    k = max(3, min(h, w) // 60) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2 * k + 1, 2 * k + 1), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if n < 2:
        return None
    big = (labels == 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))).astype(np.uint8)
    contours, _ = cv2.findContours(big, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    solid = np.zeros_like(big)
    cv2.drawContours(solid, contours, -1, 1, thickness=cv2.FILLED)  # fill holes (a pen's clear body)
    if not 0.02 <= solid.mean() <= 0.85:
        return None  # nothing stands out, or the "object" is the whole photo
    x, y, bw, bh = cv2.boundingRect(solid)
    alpha = cv2.GaussianBlur(solid.astype(np.float32), (5, 5), 0)[y:y + bh, x:x + bw]
    return np.dstack([rgb[y:y + bh, x:x + bw], (alpha * 255).astype(np.uint8)])


def product_mask(rgb: np.ndarray) -> np.ndarray | None:
    """0/1 mask of the product at the photo's own size, or None when the cut isn't trustworthy."""
    h0, w0 = rgb.shape[:2]
    scale = min(1.0, WORK_SIDE / max(h0, w0))
    small = cv2.resize(rgb, (max(8, round(w0 * scale)), max(8, round(h0 * scale))), interpolation=cv2.INTER_AREA)
    h, w = small.shape[:2]
    m = max(4, round(min(h, w) * 0.04))
    mask = np.zeros((h, w), np.uint8)
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(cv2.cvtColor(small, cv2.COLOR_RGB2BGR), mask, (m, m, w - 2 * m, h - 2 * m), bgd, fgd, 4,
                    cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None  # a flat photo: nothing for GrabCut to model
    fg = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)
    k = max(3, min(h, w) // 60) | 1
    fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(fg)
    if n < 2:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    if areas.max() / areas.sum() < 0.85:
        return None  # in pieces: probably background mixed in
    big = (labels == 1 + int(np.argmax(areas))).astype(np.uint8)
    contours, _ = cv2.findContours(big, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    solid = np.zeros_like(big)
    cv2.drawContours(solid, contours, -1, 1, thickness=cv2.FILLED)
    if not 0.03 <= solid.mean() <= 0.75:
        return None
    hull = cv2.convexHull(max(contours, key=cv2.contourArea))
    if solid.sum() / max(1.0, cv2.contourArea(hull)) < 0.6:
        return None  # ragged outline
    ring = np.zeros_like(solid)
    cv2.rectangle(ring, (m, m), (w - m - 1, h - m - 1), 1, thickness=3)
    if (solid & ring).sum() / ring.sum() > 0.08:
        return None  # runs along the box edge: the table or wall came with it
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (3, 3), 0).astype(np.float32)
    edges = cv2.dilate(np.hypot(cv2.Sobel(gray, cv2.CV_32F, 1, 0), cv2.Sobel(gray, cv2.CV_32F, 0, 1)),
                       np.ones((5, 5), np.uint8))
    outline = solid - cv2.erode(solid, np.ones((3, 3), np.uint8))
    if not outline.any() or (edges[outline > 0] > EDGE_STRENGTH).mean() < MIN_ALIGN:
        return None  # the outline crosses flat areas: not the product's own edge
    return (cv2.resize(solid.astype(np.float32), (w0, h0), interpolation=cv2.INTER_LINEAR) > 0.5).astype(np.uint8)


def on_white(rgb: np.ndarray, size: int = 160, margin: float = 0.1) -> np.ndarray | None:
    """The product centred on a white square with a soft contact shadow, like a shop catalogue.

    None when the cut isn't trustworthy; the caller then shows the plain photo instead.
    """
    solid = product_mask(rgb)
    if solid is None:
        return None
    x, y, bw, bh = cv2.boundingRect(solid)
    blur = max(3, (min(rgb.shape[:2]) // 200) | 1)
    alpha = cv2.GaussianBlur(solid.astype(np.float32), (blur, blur), 0)[y:y + bh, x:x + bw]
    obj = np.dstack([rgb[y:y + bh, x:x + bw].astype(np.float32), alpha])
    scale = size * (1 - 2 * margin) / max(bw, bh)
    ow, oh = max(1, round(bw * scale)), max(1, round(bh * scale))
    obj = cv2.resize(obj, (ow, oh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    ox, oy = (size - ow) // 2, (size - oh) // 2
    a = np.zeros((size, size), np.float32)
    a[oy:oy + oh, ox:ox + ow] = np.clip(obj[..., 3], 0, 1)

    # A faint shadow a few pixels below, so the product sits on the page instead of floating.
    shift = max(2, size // 50)
    shadow = np.zeros_like(a)
    shadow[shift:] = a[:-shift]
    shadow = cv2.GaussianBlur(shadow, (0, 0), max(2.0, size / 40)) * 0.18
    out = np.full((size, size, 3), 255, np.float32) * (1 - shadow[..., None])

    colour = np.zeros((size, size, 3), np.float32)
    colour[oy:oy + oh, ox:ox + ow] = obj[..., :3]
    out = out * (1 - a[..., None]) + colour * a[..., None]
    return np.clip(out, 0, 255).astype(np.uint8)
