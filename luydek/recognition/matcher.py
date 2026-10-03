"""Match a crop against the store's gallery and decide how sure we are.

A pack's score is the mean of its best k gallery similarities (one lucky photo
should not win on its own). Confidence tiers use the score and the margin to
the runner-up together: look-alikes such as Ichitan 420ml vs Oishi 380ml both
score high, and it is the small margin between them that sends the item to
the owner instead of auto-accepting it.

Look-alikes that differ only in colour (a blue pen and a black pen, chocolate
and strawberry milk) defeat CLIP, which pays little attention to colour. For
those, a colour histogram of the middle of the crop breaks the tie, but only
when the colours differ clearly.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..config import RecognitionCfg

Label = tuple[str, object]  # ("pack", pack_id) or ("ignore", "phone")

ACTIVE_GALLERY = """
    FROM gallery_images g
    LEFT JOIN product_packs pk ON pk.id = g.pack_id
    LEFT JOIN products p ON p.id = pk.product_id
    WHERE g.ignore_label IS NOT NULL OR p.active = 1
"""


@dataclass
class Match:
    pack_id: int | None
    ignore_label: str | None
    score: float
    margin: float
    candidates: list[tuple[int, float]] = field(default_factory=list)  # packs only, best first
    ignore_score: float = 0.0  # best ignore class (phone, cash...), a runner-up the candidates leave out
    method: str = "image"


HUES, DARK_V, GREY_S = 12, 50, 50
SIG_DIM = 1 + 3 + HUES * 2  # black, three greys, then each hue pale and vivid


def colour_signature(img: Image.Image) -> np.ndarray:
    """Named-colour histogram of the product in a crop, leaving out the mat around it.

    Crops are padded, so the crop's border is mostly mat: pixels close to the
    border's median colour are dropped. Black and grey pixels get their own
    bins, because their hue is noise. Square-rooted and normalised, so the dot
    product of two signatures is their Bhattacharyya coefficient: 1 for the
    same colours, 0 for nothing in common.
    """
    rgb = np.ascontiguousarray(np.asarray(img.convert("RGB").resize((64, 64))))
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.int16)
    border = np.concatenate([lab[:4].reshape(-1, 3), lab[-4:].reshape(-1, 3),
                             lab[:, :4].reshape(-1, 3), lab[:, -4:].reshape(-1, 3)])
    fg = (np.abs(lab - np.median(border, axis=0)) * (0.8, 1, 1)).sum(axis=-1) > 30
    if fg.mean() < 0.05:  # nothing stands out from the border: a close-up, use it all
        fg[:] = True
    h, sat, v = (c[fg].astype(np.int32) for c in cv2.split(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)))
    bins = np.where(v < DARK_V, 0,
                    np.where(sat < GREY_S, 1 + np.minimum(v * 3 // 256, 2),
                             4 + (h * HUES // 180) * 2 + (sat >= 150)))
    hist = np.bincount(bins, minlength=SIG_DIM).astype(np.float32)
    return np.sqrt(hist / max(hist.sum(), 1.0))


class Gallery:
    def __init__(self, vectors: np.ndarray, labels: list[Label], colours: np.ndarray | None = None) -> None:
        self.vectors = vectors
        self.labels = labels
        self.colours = colours  # (N, D) colour signatures aligned with labels, when available
        self.index: dict[Label, np.ndarray] = {}
        for i, lab in enumerate(labels):
            self.index.setdefault(lab, []).append(i)
        self.index = {lab: np.asarray(ix) for lab, ix in self.index.items()}

    @classmethod
    def load(cls, conn: sqlite3.Connection, media_dir: Path | None = None,
             colour_cache: dict[int, np.ndarray] | None = None, active_only: bool = True) -> "Gallery":
        """Photos of products still in the catalog, plus the ignore classes.

        A removed product's photos stay on disk (its history is kept) but must not
        be matched: a sale can't be of something the shop no longer stocks.
        `active_only=False` includes them, for calibration.
        """
        source = ACTIVE_GALLERY if active_only else "FROM gallery_images g"
        rows = conn.execute(
            f"SELECT g.id, g.pack_id, g.ignore_label, g.embedding, g.crop_path {source} ORDER BY g.id"
        ).fetchall()
        if not rows:
            return cls(np.zeros((0, 1), np.float32), [])
        vectors = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        labels = [("pack", r["pack_id"]) if r["pack_id"] is not None else ("ignore", r["ignore_label"]) for r in rows]
        colours = None
        if media_dir is not None:
            cache = colour_cache if colour_cache is not None else {}
            sigs = []
            for r in rows:
                if r["id"] not in cache:
                    try:
                        cache[r["id"]] = colour_signature(Image.open(media_dir / r["crop_path"]))
                    except OSError:
                        cache[r["id"]] = np.zeros(SIG_DIM, np.float32)  # photo file gone: never wins
                sigs.append(cache[r["id"]])
            colours = np.stack(sigs)
        return cls(vectors, labels, colours)

    def __len__(self) -> int:
        return len(self.labels)

    def match(self, query: np.ndarray, k: int = 3) -> Match:
        if not self.labels:
            return Match(None, None, 0.0, 0.0, [])
        sims = self.vectors @ query
        scores = {lab: float(np.sort(sims[ix])[::-1][:k].mean()) for lab, ix in self.index.items()}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        (best_label, best), runner_up = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
        candidates = [(int(lab[1]), round(s, 4)) for lab, s in ranked if lab[0] == "pack"][:6]
        ignore_score = round(max((s for lab, s in ranked if lab[0] == "ignore"), default=0.0), 4)
        if best_label[0] == "ignore":
            return Match(None, str(best_label[1]), round(best, 4), round(best - runner_up, 4), candidates, ignore_score)
        return Match(int(best_label[1]), None, round(best, 4), round(best - runner_up, 4), candidates, ignore_score)

    def colour_score(self, signature: np.ndarray, pack_id: int, k: int = 3) -> float:
        """How well the crop's colours match a pack's photos (mean of the best k)."""
        ix = self.index.get(("pack", pack_id))
        if self.colours is None or ix is None:
            return 0.0
        return float(np.sort(self.colours[ix] @ signature)[::-1][:k].mean())


def colour_tiebreak(gallery: Gallery, match: Match, signature: np.ndarray, cfg: RecognitionCfg) -> Match | None:
    """Settle a look-alike tie by colour; None when there is no tie or colour can't tell.

    Look-alikes are the candidates CLIP scores within `lookalike_gap` of its top
    choice. The pack whose colours match best must beat the runner-up look-alike
    by `colour_margin`. The returned match carries that pack's own CLIP score and
    its margin over everything outside the tie (other packs and the ignore
    classes), so the usual tiers still apply: colour settles which look-alike it
    is, not whether the item is known at all.
    """
    if gallery.colours is None or match.pack_id is None:
        return None
    close = [p for p, s in match.candidates if match.score - s <= cfg.lookalike_gap]
    if len(close) < 2:
        return None
    scored = sorted(((gallery.colour_score(signature, p, cfg.top_k_per_pack), p) for p in close), reverse=True)
    (best, pack_id), (second, _) = scored[0], scored[1]
    if best - second < cfg.colour_margin:
        return None
    clip = dict(match.candidates)
    outside = max([s for p, s in match.candidates if p not in close] + [match.ignore_score])
    candidates = [(pack_id, clip[pack_id])] + [c for c in match.candidates if c[0] != pack_id]
    return Match(pack_id, None, clip[pack_id], round(clip[pack_id] - outside, 4), candidates, match.ignore_score,
                 "colour")


def tier(score: float, margin: float, cfg: RecognitionCfg) -> str:
    if score >= cfg.t_high and margin >= cfg.m_high:
        return "high"
    if score >= cfg.t_low:
        return "medium"
    return "low"


class GalleryCache:
    """Reload the gallery only when rows were added or removed."""

    def __init__(self, media_dir: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._gallery: Gallery | None = None
        self._media_dir = media_dir
        self._colours: dict[int, np.ndarray] = {}  # by gallery id: each photo is read from disk once

    def get(self, conn: sqlite3.Connection) -> Gallery:
        # Removing or restoring a product changes the count of matchable photos.
        key = tuple(conn.execute(f"SELECT COUNT(*), COALESCE(MAX(g.id), 0) {ACTIVE_GALLERY}").fetchone())
        with self._lock:
            if key != self._key or self._gallery is None:
                self._gallery, self._key = Gallery.load(conn, self._media_dir, self._colours), key
            return self._gallery
