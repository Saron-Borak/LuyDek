"""Match a crop against the store's gallery and decide how sure we are.

A pack's score is the mean of its best k gallery similarities (one lucky photo
should not win on its own). Confidence tiers use the score and the margin to
the runner-up together: look-alikes such as Ichitan 420ml vs Oishi 380ml both
score high, and it is the small margin between them that sends the item to
the owner instead of auto-accepting it.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field

import numpy as np

from ..config import RecognitionCfg

Label = tuple[str, object]  # ("pack", pack_id) or ("ignore", "phone")


@dataclass
class Match:
    pack_id: int | None
    ignore_label: str | None
    score: float
    margin: float
    candidates: list[tuple[int, float]] = field(default_factory=list)  # packs only, best first


class Gallery:
    def __init__(self, vectors: np.ndarray, labels: list[Label]) -> None:
        self.vectors = vectors
        self.labels = labels
        self.index: dict[Label, np.ndarray] = {}
        for i, lab in enumerate(labels):
            self.index.setdefault(lab, []).append(i)
        self.index = {lab: np.asarray(ix) for lab, ix in self.index.items()}

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "Gallery":
        rows = conn.execute("SELECT pack_id, ignore_label, embedding FROM gallery_images ORDER BY id").fetchall()
        if not rows:
            return cls(np.zeros((0, 1), np.float32), [])
        vectors = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        labels = [("pack", r["pack_id"]) if r["pack_id"] is not None else ("ignore", r["ignore_label"]) for r in rows]
        return cls(vectors, labels)

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
        if best_label[0] == "ignore":
            return Match(None, str(best_label[1]), round(best, 4), round(best - runner_up, 4), candidates)
        return Match(int(best_label[1]), None, round(best, 4), round(best - runner_up, 4), candidates)


def tier(score: float, margin: float, cfg: RecognitionCfg) -> str:
    if score >= cfg.t_high and margin >= cfg.m_high:
        return "high"
    if score >= cfg.t_low:
        return "medium"
    return "low"


class GalleryCache:
    """Reload the gallery only when rows were added or removed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._gallery: Gallery | None = None

    def get(self, conn: sqlite3.Connection) -> Gallery:
        key = tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM gallery_images").fetchone())
        with self._lock:
            if key != self._key or self._gallery is None:
                self._gallery, self._key = Gallery.load(conn), key
            return self._gallery
