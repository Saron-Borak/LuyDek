"""Counter photo -> identified items.

    segment (mat difference) -> barcode pass -> CLIP match against the gallery
    -> multipack multiplier -> confidence tier; phones, cash and bags dropped
"""

from __future__ import annotations

import io
import sqlite3
import threading
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from .. import clock, db
from ..config import Config
from ..services import catalog
from ..services.transactions import DetectionRecord
from . import barcode, segment
from .encoder import Encoder, get_encoder, loaded
from .matcher import GalleryCache, tier

REFERENCE_KEY = "reference_path"


@dataclass
class Detection:
    record: DetectionRecord
    label: str               # display name for the counter overlay
    ignored: str | None = None


class Recognizer:
    def __init__(self, cfg: Config, encoder: Encoder) -> None:
        self.cfg = cfg
        self.encoder = encoder
        self.cache = GalleryCache()

    # --- recognition -----------------------------------------------------------

    def recognize(self, conn: sqlite3.Connection, frame: np.ndarray, crop_prefix: str,
                  use_reference: bool = True) -> list[Detection]:
        """`use_reference=False` for photos from another camera (the upload backup):
        the empty-mat photo would not line up, so the mat colour is guessed instead."""
        rc = self.cfg.recognition
        reference = load_reference(conn, self.cfg) if use_reference else None
        blobs = segment.segment(frame, reference, rc.segment)
        if not blobs:
            return []
        codes = barcode.read(frame)
        vectors = self.encoder.encode_images([b.crop for b in blobs])
        gallery = self.cache.get(conn)

        crops_dir = self.cfg.media_dir / "crops"
        crops_dir.mkdir(parents=True, exist_ok=True)
        out: list[Detection] = []
        for i, (blob, vec) in enumerate(zip(blobs, vectors, strict=True)):
            code_pack = _barcode_in(conn, codes, blob.bbox)
            if code_pack is not None:
                pack_id, score, t, method, cands, ignored = code_pack, 1.0, "high", "barcode", [(code_pack, 1.0)], None
            else:
                m = gallery.match(vec, rc.top_k_per_pack)
                if m.ignore_label:
                    out.append(Detection(DetectionRecord(None, m.score, "high", "image", blob.bbox), m.ignore_label,
                                         ignored=m.ignore_label))
                    continue
                pack_id, score, cands, ignored = m.pack_id, m.score, m.candidates, None
                t = tier(m.score, m.margin, rc) if pack_id is not None else "low"
                method = "image"
            pk = catalog.pack(conn, pack_id) if pack_id is not None else None
            if pk is not None and pk["units"] > 1 and method == "image":
                method = "multipack"
            rel = f"crops/{crop_prefix}_{i}.jpg"
            blob.crop.save(self.cfg.media_dir / rel, quality=90)
            label = catalog.pack_label(pk, "en") if pk is not None else "?"
            out.append(Detection(DetectionRecord(pack_id, score, t, method, blob.bbox, rel, cands), label, ignored))
        return out

    # --- the gallery -------------------------------------------------------------

    def add_example(self, conn: sqlite3.Connection, image: Image.Image, pack_id: int | None,
                    ignore_label: str | None, source: str) -> int:
        rel = f"gallery/{uuid.uuid4().hex[:12]}.jpg"
        path = self.cfg.media_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(path, quality=90)
        vec = self.encoder.encode_images([image])[0]
        cur = conn.execute(
            "INSERT INTO gallery_images (pack_id, ignore_label, crop_path, embedding, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pack_id, ignore_label, rel, vec.astype(np.float32).tobytes(), source, clock.stamp()),
        )
        return cur.lastrowid

    def enroll(self, conn: sqlite3.Connection, frame: np.ndarray, pack_id: int | None,
               ignore_label: str | None = None, use_reference: bool = True) -> tuple[int, tuple[int, int, int, int]]:
        """One enrollment shot: the largest item on the mat joins the gallery.

        A photo with nothing found on the mat (a close-up taken elsewhere) is
        enrolled whole.
        """
        reference = load_reference(conn, self.cfg) if use_reference else None
        blob = segment.largest(frame, reference, self.cfg.recognition.segment)
        if blob is None:
            h, w = frame.shape[:2]
            crop, bbox = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), (0, 0, w, h)
        else:
            crop, bbox = blob.crop, blob.bbox
        return self.add_example(conn, crop, pack_id, ignore_label, "enrollment"), bbox

    def learn(self, conn: sqlite3.Connection, crop_path: str, pack_id: int | None, ignore_label: str | None) -> None:
        """An owner correction becomes a new gallery example for this store."""
        path = self.cfg.media_dir / crop_path
        if path.exists():
            self.add_example(conn, Image.open(path), pack_id, ignore_label, "correction")


def _barcode_in(conn: sqlite3.Connection, codes: list[tuple[str, tuple[int, int]]],
                bbox: tuple[int, int, int, int]) -> int | None:
    x, y, w, h = bbox
    for text, (cx, cy) in codes:
        if x <= cx <= x + w and y <= cy <= y + h:
            pack_id = catalog.pack_by_barcode(conn, text)
            if pack_id is not None:
                return pack_id
    return None


# --- the empty-mat reference -----------------------------------------------------

_ref_lock = threading.Lock()
_ref_cache: dict[str, np.ndarray] = {}


def load_reference(conn: sqlite3.Connection, cfg: Config) -> np.ndarray | None:
    rel = db.get_setting(conn, REFERENCE_KEY)
    if not rel:
        return None
    with _ref_lock:
        if rel not in _ref_cache:
            img = cv2.imread(str(cfg.media_dir / rel))
            if img is None:
                return None
            _ref_cache.clear()
            _ref_cache[rel] = img
        return _ref_cache[rel]


def save_reference(conn: sqlite3.Connection, cfg: Config, data: bytes) -> str:
    frame = segment.decode(data)
    rel = f"reference/empty_{uuid.uuid4().hex[:8]}.jpg"
    path = cfg.media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), frame)
    db.set_setting(conn, REFERENCE_KEY, rel)
    with _ref_lock:
        _ref_cache.clear()
    return rel


def save_frame(cfg: Config, data: bytes, name: str) -> str:
    rel = f"frames/{name}.jpg"
    path = cfg.media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(data)).convert("RGB").save(path, quality=85)
    return rel


# --- process-wide recognizer ---------------------------------------------------

_rec_lock = threading.Lock()
_recognizer: Recognizer | None = None


def get_recognizer(cfg: Config) -> Recognizer:
    global _recognizer
    with _rec_lock:
        if _recognizer is None:
            _recognizer = Recognizer(cfg, get_encoder(cfg.recognition.model_name, cfg.recognition.device))
        return _recognizer


def ready() -> bool:
    return _recognizer is not None


def learner(cfg: Config):
    """The correction hook for `transactions.resolve`, if the encoder is already loaded.

    Never loads CLIP just to learn: an owner's tap must stay instant.
    """
    if _recognizer is not None:
        return _recognizer.learn
    enc = loaded()
    return Recognizer(cfg, enc).learn if enc is not None else None


def reset() -> None:
    global _recognizer
    _recognizer = None
    _ref_cache.clear()
