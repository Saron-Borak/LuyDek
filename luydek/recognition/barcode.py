"""Barcode pass. A decoded barcode is the one identification that needs no check."""

from __future__ import annotations

import numpy as np

try:
    import zxingcpp
except ImportError:  # recognition still works on images alone
    zxingcpp = None


def read(frame_bgr: np.ndarray) -> list[tuple[str, tuple[int, int]]]:
    """Decoded barcodes as (text, centre point) in frame pixels."""
    if zxingcpp is None:
        return []
    found = []
    for r in zxingcpp.read_barcodes(frame_bgr[..., ::-1].copy()):
        p = r.position
        xs = [p.top_left.x, p.top_right.x, p.bottom_left.x, p.bottom_right.x]
        ys = [p.top_left.y, p.top_right.y, p.bottom_left.y, p.bottom_right.y]
        found.append((r.text, (sum(xs) // 4, sum(ys) // 4)))
    return found
