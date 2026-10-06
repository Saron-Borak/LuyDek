"""Counter photo -> identified items.

    segment (mat difference) -> barcode pass -> CLIP match against the gallery
    -> multipack multiplier -> confidence tier; phones, cash and bags dropped
"""

from __future__ import annotations

import io
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from .. import clock, db
from ..config import Config
from ..services import catalog, transactions
from ..services.transactions import DetectionRecord
from . import barcode, detector, segment, tray
from .encoder import Encoder, get_encoder, loaded
from .matcher import GalleryCache, colour_signature, colour_tiebreak, tier

REFERENCE_KEY = "reference_path"
LEARNED_JUNK = "other"  # learned from "Not a sale" answers: clutter, but sometimes a product or the mat
ROI_KEY = "mat_roi"
TRAY_KEY = "tray_roi"


@dataclass
class Detection:
    record: DetectionRecord
    label: str               # display name for the counter overlay
    ignored: str | None = None  # why it wasn't recorded: an ignore class, or "background"
    background: float = 0.0     # how much it looks like the same spot of the empty mat (0 without one)


class Skipped(Exception):
    """A hold-to-record frame not worth keeping: "hand" (in the way), "empty" (lifted off the mat) or "same"
    (a near-copy of a photo the product already has)."""


class Recognizer:
    def __init__(self, cfg: Config, encoder: Encoder, finder: detector.Detector | None = None) -> None:
        self.cfg = cfg
        self.encoder = encoder
        self.finder = finder  # YOLOE-26: each item on the mat; None: the mat difference alone
        # Colour signatures are read from the gallery photos only when the tie-break is on.
        self.cache = GalleryCache(cfg.media_dir if cfg.recognition.colour_tiebreak else None)

    # --- recognition -----------------------------------------------------------

    def recognize(self, conn: sqlite3.Connection, frame: np.ndarray, crop_prefix: str,
                  use_reference: bool = True) -> list[Detection]:
        """`use_reference=False` for photos from another camera (the upload backup):
        the empty-mat photo and the marked mat area would not line up, so the mat
        colour is guessed instead. Raises `segment.SceneChanged` when the counter
        camera no longer sees what it was calibrated on."""
        rc = self.cfg.recognition
        reference = load_reference(conn, self.cfg) if use_reference else None
        roi = load_roi(conn) if use_reference else None
        tray_roi = load_roi(conn, TRAY_KEY) if use_reference else None
        blobs = segment.segment(frame, reference, rc.segment, roi, check_scene=reference is not None, exclude=tray_roi)
        if self.finder is not None:
            blobs = self._detect(frame, reference, roi, tray_roi, blobs)
        if not blobs:
            return []
        codes = barcode.read(frame)
        vectors = self.encoder.encode_images([b.crop for b in blobs])
        gallery = self.cache.get(conn)
        blobs, vectors = self._choose_parts(blobs, vectors, gallery)
        backgrounds = self._background_likeness(reference, frame, blobs, vectors)

        crops_dir = self.cfg.media_dir / "crops"
        crops_dir.mkdir(parents=True, exist_ok=True)
        out: list[Detection] = []
        for i, (blob, vec, bg) in enumerate(zip(blobs, vectors, backgrounds, strict=True)):
            rel = f"crops/{crop_prefix}_{i}.jpg"
            blob.crop.save(self.cfg.media_dir / rel, quality=90)  # kept for ignored ones too: the camera log shows them
            code_pack = _barcode_in(conn, codes, blob.bbox)
            if code_pack is not None:
                pack_id, score, t, method, cands = code_pack, 1.0, "high", "barcode", [(code_pack, 1.0)]
            else:
                m = gallery.match(vec, rc.top_k_per_pack)
                best_product = m.candidates[0][1] if m.candidates else 0.0
                # Set aside only what the shop taught it to ignore, when clearly that, and the same
                # patch of mat the owner already said wasn't a sale. Everything else is asked about,
                # so a product that looks like clutter is never lost.
                why = None
                if m.ignore_label and m.ignore_label != LEARNED_JUNK:
                    if m.score >= rc.ignore_min and m.score - best_product >= rc.ignore_margin:
                        why, how_sure = m.ignore_label, m.score
                elif (m.ignore_label == LEARNED_JUNK and m.score >= rc.junk_repeat and bg >= rc.background_sim
                      and best_product < rc.t_low):
                    why, how_sure = "background", m.score
                if why is not None:
                    best = m.candidates[0] if m.candidates else (None, None)
                    out.append(Detection(DetectionRecord(best[0], round(how_sure, 4), "high", "image", blob.bbox, rel,
                                                         m.candidates), why, ignored=why, background=round(bg, 4)))
                    continue
                hint = None
                if m.ignore_label:
                    # Looked like clutter: ask what it is, with the closest products as the choices.
                    pack_id = m.candidates[0][0] if m.candidates else None
                    score, cands, t, method, hint = best_product, m.candidates, "low", "image", "junk"
                else:
                    if rc.colour_tiebreak and m.pack_id is not None:
                        m = colour_tiebreak(gallery, m, colour_signature(blob.crop), rc) or m
                    pack_id, score, cands = m.pack_id, m.score, m.candidates
                    t = tier(m.score, m.margin, rc) if pack_id is not None else "low"
                    method = m.method
                if bg >= rc.background_sim:
                    hint = hint or "background"
                    if t == "high":
                        t = "medium"  # it looks like the mat itself (a paper strip is not a pen): ask, never assume
            pk = catalog.pack(conn, pack_id) if pack_id is not None else None
            if pk is not None and pk["units"] > 1 and method == "image":
                method = "multipack"
            label = catalog.pack_label(pk, "en") if pk is not None else "?"
            out.append(Detection(DetectionRecord(pack_id, score, t, method, blob.bbox, rel, cands,
                                                 None if code_pack is not None else hint), label,
                                 background=round(bg, 4)))
        return out

    def _detect(self, frame: np.ndarray, reference: np.ndarray | None, roi, tray_roi,
                mat_blobs: list[segment.Blob]) -> list[segment.Blob]:
        """Each item the detector sees, plus anything the mat difference sees that it didn't: an object
        the detector doesn't know is still an item, never a lost sale."""
        rc = self.cfg.recognition
        h, w = frame.shape[:2]
        roi_px = segment.roi_box(roi, w, h)
        tray_px = segment.roi_box(tray_roi, w, h) if tray_roi is not None else None
        found = detector.tidy(self.finder.detect(frame), roi_px, tray_px, rc.detector, rc.segment, w)
        if reference is not None:
            found = detector.something_there(frame, reference, found, rc.segment, rc.detector.min_changed)
        if not found:
            return mat_blobs
        if reference is not None and reference.shape[:2] != (h, w):
            reference = cv2.resize(reference, (w, h), interpolation=cv2.INTER_AREA)
        mat = reference if reference is not None else segment.synthetic_reference(frame)
        items = detector.blobs(frame, found, roi_px, mat, rc.segment)
        # Search again outside each item found and its margin (its whole box: the outlines the detector
        # draws are rough, and the bits of an item sticking out of its outline would come back as items).
        covered = np.zeros((h, w), np.uint8)
        for f in found:
            x0, y0, x1, y1 = detector.padded(f.box, w, h, rc.segment)
            covered[y0:y1, x0:x1] = 255
        rest = segment.segment(frame, reference, rc.segment, roi, exclude=tray_roi, covered=covered)
        out = items + rest
        out.sort(key=lambda bl: (bl.bbox[1] // 50, bl.bbox[0]))
        return out

    def _choose_parts(self, blobs: list, vectors: np.ndarray, gallery) -> tuple[list, np.ndarray]:
        """An outline with separate pieces inside (`segment.split_apart`): one item, or several?

        Decided by what the pieces look like. When every piece looks like the same thing, it is
        one item (a pen's cap and tip). When they look like different things (a tube, a keychain
        and a coaster touching on the mat), each is its own item, and pieces that match the same
        product are put back together, so nothing is asked about in bulk or counted twice.
        """
        if not any(b.parts for b in blobs):
            return blobs, vectors
        rc = self.cfg.recognition
        out_blobs, out_vecs, pending = [], [], []
        for b, v in zip(blobs, vectors, strict=True):
            if not b.parts:
                out_blobs.append(b)
                out_vecs.append(v)
                continue
            pv = self.encoder.encode_images([p.crop for p in b.parts])
            unit = pv / (np.linalg.norm(pv, axis=1, keepdims=True) + 1e-9)
            matches = [gallery.match(vec, rc.top_k_per_pack) for vec in pv]
            # Pieces are one item when they match the same product well (a pen's cap and tip), or
            # look like each other. A weak "closest product" says nothing: a tube matched at 1.00
            # and a keychain matched at 0.70 to the same product are two things.
            groups: list[list[int]] = []
            for i, m in enumerate(matches):
                for g in groups:
                    j = g[0]
                    same = m.pack_id is not None and m.pack_id == matches[j].pack_id \
                        and min(m.score, matches[j].score) >= rc.t_low
                    if same or float(unit[i] @ unit[j]) >= 0.85:
                        g.append(i)
                        break
                else:
                    groups.append([i])
            if len(groups) == 1:
                out_blobs.append(b)       # one thing in pieces
                out_vecs.append(v)
                continue
            for members in groups:
                if len(members) == 1:
                    out_blobs.append(b.parts[members[0]])
                    out_vecs.append(pv[members[0]])
                else:
                    out_blobs.append(self._union(b.parts, members))
                    out_vecs.append(None)
                    pending.append(len(out_blobs) - 1)
        if pending:
            for i, vec in zip(pending, self.encoder.encode_images([out_blobs[i].crop for i in pending]), strict=True):
                out_vecs[i] = vec
        order = sorted(range(len(out_blobs)), key=lambda i: (out_blobs[i].bbox[1] // 50, out_blobs[i].bbox[0]))
        return [out_blobs[i] for i in order], np.stack([out_vecs[i] for i in order])

    def _union(self, parts: list, members: list[int]):
        """Pieces that are one product, as one item: the box around them, cropped again from the pieces."""
        boxes = [parts[i].bbox for i in members]
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes)
        canvas = Image.new("RGB", (x1 - x0, y1 - y0))
        for i in members:
            bx, by = parts[i].bbox[:2]
            canvas.paste(parts[i].crop, (bx - x0, by - y0))
        return segment.Blob((x0, y0, x1 - x0, y1 - y0), round(sum(parts[i].area_frac for i in members), 4), canvas,
                            any(parts[i].touches_edge for i in members))

    def _background_likeness(self, reference: np.ndarray | None, frame: np.ndarray, blobs: list,
                             vectors: np.ndarray) -> list[float]:
        """How much each item looks like the same spot of the empty mat (cosine, 0 with no reference)."""
        if reference is None:
            return [0.0] * len(blobs)
        h, w = frame.shape[:2]
        if reference.shape[:2] != (h, w):
            reference = cv2.resize(reference, (w, h), interpolation=cv2.INTER_AREA)
        spots = []
        for b in blobs:
            x, y, bw, bh = b.bbox
            spots.append(Image.fromarray(cv2.cvtColor(reference[y:y + bh, x:x + bw], cv2.COLOR_BGR2RGB)))
        empty = self.encoder.encode_images(spots)
        a = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)
        b = empty / (np.linalg.norm(empty, axis=1, keepdims=True) + 1e-9)
        return [float(v) for v in (a * b).sum(axis=1)]

    def read_tray(self, conn: sqlite3.Connection, frame: np.ndarray) -> tray.TrayReading | None:
        """What lies on the tablet tray, if a tray is marked and the counter is calibrated."""
        tray_roi = load_roi(conn, TRAY_KEY)
        reference = load_reference(conn, self.cfg)
        if tray_roi is None or reference is None:
            return None
        return tray.read(frame, reference, tray_roi, self.cfg.recognition.tray)

    # --- the gallery -------------------------------------------------------------

    def add_example(self, conn: sqlite3.Connection, image: Image.Image, pack_id: int | None,
                    ignore_label: str | None, source: str, vec: np.ndarray | None = None) -> int:
        rel = f"gallery/{uuid.uuid4().hex[:12]}.jpg"
        path = self.cfg.media_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(path, quality=90)
        if vec is None:
            vec = self.encoder.encode_images([image])[0]
        cur = conn.execute(
            "INSERT INTO gallery_images (pack_id, ignore_label, crop_path, embedding, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pack_id, ignore_label, rel, vec.astype(np.float32).tobytes(), source, clock.stamp()),
        )
        return cur.lastrowid

    def enroll(self, conn: sqlite3.Connection, frame: np.ndarray, pack_id: int | None,
               ignore_label: str | None = None, use_reference: bool = True,
               burst: bool = False, whole_if_empty: bool = True) -> tuple[int, tuple[int, int, int, int]]:
        """One enrollment shot: the largest item on the mat joins the gallery.

        A photo from elsewhere with nothing found on it (a close-up) is enrolled
        whole, unless `whole_if_empty` is off. From the calibrated counter camera an
        empty mat is refused instead: a photo of nothing would only teach the gallery noise.

        `burst` is for hold-to-record, where frames arrive a few a second while the
        owner turns the product: frames with the hand in them and near-copies of a
        photo the product already has raise `Skipped` instead of joining the gallery.
        """
        reference = load_reference(conn, self.cfg) if use_reference else None
        roi = load_roi(conn) if use_reference else None
        tray_roi = load_roi(conn, TRAY_KEY) if use_reference else None
        seg = self.cfg.recognition.segment
        blob = segment.largest(frame, reference, seg, roi, check_scene=reference is not None, exclude=tray_roi,
                               avoid_edge=burst and reference is not None)
        if blob is None and reference is not None:
            if burst:  # the product is in the hand for a moment, or only the hand is on the mat
                raise Skipped("hand" if segment.largest(frame, reference, seg, roi, exclude=tray_roi) else "empty")
            raise ValueError("Nothing found on the mat. Put the product inside the mat area and try again.")
        if blob is None and not whole_if_empty:
            if burst:
                raise Skipped("empty")
            raise ValueError("Nothing stood out from the background. Put the item on a plain surface, "
                             "with a little space around it, and try again.")
        if blob is None:
            h, w = frame.shape[:2]
            crop, bbox = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), (0, 0, w, h)
        else:
            crop, bbox = blob.crop, blob.bbox
        vec = self.encoder.encode_images([crop])[0]
        if burst:
            where, arg = ("pack_id = ?", pack_id) if pack_id is not None else ("ignore_label = ?", ignore_label)
            have = [np.frombuffer(r[0], np.float32)
                    for r in conn.execute(f"SELECT embedding FROM gallery_images WHERE {where}", (arg,))]
            if have:
                v = vec / (np.linalg.norm(vec) + 1e-9)
                m = np.stack(have)
                m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
                if float((m @ v).max()) >= self.cfg.recognition.enroll_duplicate:
                    raise Skipped("same")
        return self.add_example(conn, crop, pack_id, ignore_label, "enrollment", vec), bbox

    def learn(self, conn: sqlite3.Connection, crop_path: str, pack_id: int | None, ignore_label: str | None,
              source: str = "correction") -> None:
        """An owner answer becomes a new gallery example for this store.

        Learned photos are capped per product (newest kept) so the gallery keeps
        tracking today's packaging and lighting; enrollment photos are never dropped.
        """
        path = self.cfg.media_dir / crop_path
        if not path.exists():
            return
        self.add_example(conn, Image.open(path), pack_id, ignore_label, source)
        where, arg = ("pack_id = ?", pack_id) if pack_id is not None else ("ignore_label = ?", ignore_label)
        cap = self.cfg.recognition.learn_cap * (2 if pack_id is None else 1)  # "other" junk is more varied
        for old in conn.execute(
            f"SELECT id, crop_path FROM gallery_images WHERE {where} AND source IN ('confirmed', 'correction') "
            "ORDER BY id DESC LIMIT -1 OFFSET ?", (arg, cap),
        ).fetchall():
            (self.cfg.media_dir / old["crop_path"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM gallery_images WHERE id = ?", (old["id"],))

    def regroup(self, conn: sqlite3.Connection, pack_id: int, skip: int | None = None) -> list[int]:
        """After the owner names a new product, unanswered sales that look like it are re-guessed as it.

        Only items whose best match is now that product, at `t_high` or better, change. They
        stay questions (see `transactions.reguess`), so one "All correct" confirms them all.
        """
        rc = self.cfg.recognition
        rows = [r for r in [*transactions.pending_items(conn), *transactions.unknown_items(conn)]
                if r["id"] != skip and r["crop_path"] and r["method"] != "tray" and r["pack_id"] != pack_id
                and (self.cfg.media_dir / r["crop_path"]).exists()]
        if not rows:
            return []
        crops = []
        for r in rows:
            with Image.open(self.cfg.media_dir / r["crop_path"]) as im:
                crops.append(im.convert("RGB"))
        gallery = self.cache.get(conn)
        changed = []
        for r, vec in zip(rows, self.encoder.encode_images(crops), strict=True):
            m = gallery.match(vec, rc.top_k_per_pack)
            if m.pack_id == pack_id and m.ignore_label is None and m.score >= rc.t_high:
                if transactions.reguess(conn, r["id"], pack_id, m.score, m.candidates):
                    changed.append(r["id"])
        return changed


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


def load_roi(conn: sqlite3.Connection, key: str = ROI_KEY) -> segment.Roi | None:
    """The marked mat (or, with TRAY_KEY, the tablet tray) as fractions of the frame."""
    raw = db.get_setting(conn, key)
    return tuple(json.loads(raw)) if raw else None


def save_roi(conn: sqlite3.Connection, roi: segment.Roi | None, key: str = ROI_KEY) -> segment.Roi | None:
    """Mark the mat or the tablet tray (fractions of the frame, clamped to it); None clears it."""
    if roi is None:
        db.set_setting(conn, key, "")
        return None
    x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in roi)
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    smallest = 0.04 if key == TRAY_KEY else 0.1  # a tray is a card beside the till; a mat is most of the view
    if x1 - x0 < smallest or y1 - y0 < smallest:
        what = "tray" if key == TRAY_KEY else "mat"
        raise ValueError(f"The {what} area is too small: drag a box around the whole {what}.")
    roi = (round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4))
    db.set_setting(conn, key, json.dumps(roi))
    return roi


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
            _recognizer = Recognizer(cfg, get_encoder(cfg.recognition.model_name, cfg.recognition.device),
                                     detector.get_detector(cfg))
        return _recognizer


def ready() -> bool:
    return _recognizer is not None


def finder_name() -> str:
    """What finds the items on the mat, for the Counter page."""
    if _recognizer is not None and _recognizer.finder is not None:
        return getattr(_recognizer.finder, "name", "detector")
    return "mat difference"


def learner(cfg: Config):
    """The correction hook for `transactions.resolve`, if the encoder is already loaded.

    Never loads CLIP just to learn: an owner's tap must stay instant.
    """
    if _recognizer is not None:
        return _recognizer.learn
    enc = loaded()
    return Recognizer(cfg, enc).learn if enc is not None else None


def regrouper(cfg: Config):
    """`Recognizer.regroup` if the encoder is already loaded, else None (like `learner`)."""
    if _recognizer is not None:
        return _recognizer.regroup
    enc = loaded()
    return get_recognizer(cfg).regroup if enc is not None else None


def reset() -> None:
    global _recognizer
    _recognizer = None
    _ref_cache.clear()
