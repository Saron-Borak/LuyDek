"""Shared fixtures. Tests use a cheap colour-histogram encoder instead of CLIP,
a frozen clock, and a fresh database per test."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from luydek import clock, db
from luydek.config import Config, DetectorCfg, PathsCfg, RecognitionCfg, SegmentCfg
from luydek.recognition import encoder as encoder_mod
from luydek.recognition import pipeline
from luydek.services import catalog

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 27, 18, 0, 0)  # a Sunday evening
MAT = (190, 200, 205)  # BGR: a pale grey-beige counter mat


class HistogramEncoder:
    """Colour histogram in HSV, L2-normalised. Enough to tell painted boxes apart."""

    dim = 8 * 4 * 4

    def encode_images(self, images):
        out = []
        for im in images:
            rgb = np.asarray(im.convert("RGB").resize((48, 48)))
            hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
            hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).flatten()
            out.append(hist / (np.linalg.norm(hist) + 1e-9))
        return np.asarray(out, dtype=np.float32).reshape(len(images), self.dim)


@pytest.fixture(autouse=True)
def frozen_clock():
    clock.freeze(NOW)
    yield
    clock.freeze(None)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        paths=PathsCfg(db=str(tmp_path / "test.db"), media=str(tmp_path / "media"),
                       seed_catalog=str(ROOT / "data" / "seed_catalog.yaml")),
        # The detector (YOLOE-26) is off: tests use painted boxes and a fake detector where they need one.
        recognition=RecognitionCfg(t_high=0.97, m_high=0.02, t_low=0.6,
                                   segment=SegmentCfg(work_width=640, min_area_frac=0.004),
                                   detector=DetectorCfg(enabled=False)),
    )


@pytest.fixture
def conn(cfg) -> sqlite3.Connection:
    c = db.connect(cfg.db_path)
    db.init_schema(c)
    catalog.load_seed(c, Path(cfg.paths.seed_catalog))
    c.commit()
    yield c
    c.close()


@pytest.fixture
def stub_encoder():
    enc = HistogramEncoder()
    encoder_mod.set_encoder(enc)
    pipeline.reset()
    yield enc
    encoder_mod.set_encoder(None)
    pipeline.reset()


def pid(conn: sqlite3.Connection, sku: str) -> int:
    return conn.execute("SELECT id FROM products WHERE sku = ?", (sku,)).fetchone()["id"]


def pack_of(conn: sqlite3.Connection, sku: str, units: int = 1) -> int:
    return conn.execute(
        "SELECT pk.id FROM product_packs pk JOIN products p ON p.id = pk.product_id WHERE p.sku = ? AND pk.units = ?",
        (sku, units),
    ).fetchone()["id"]


# Painted "products": each a distinct colour with a stripe, so crops are recognisable.
PRODUCT_COLOURS = {
    "coke-330": ((30, 30, 200), (240, 240, 240)),     # red can, white stripe
    "sprite-1500": ((60, 160, 40), (40, 200, 230)),   # green, yellow stripe
    "ichitan-420": ((40, 140, 60), (30, 30, 30)),     # dark green, black stripe
    "dumex-400": ((200, 120, 30), (250, 250, 250)),   # blue tin
}


def mat_frame(w: int = 640, h: int = 480) -> np.ndarray:
    frame = np.zeros((h, w, 3), np.uint8)
    frame[:] = MAT
    return frame


def paint(frame: np.ndarray, sku: str, x: int, y: int, w: int = 90, h: int = 130) -> None:
    body, stripe = PRODUCT_COLOURS[sku]
    cv2.rectangle(frame, (x, y), (x + w, y + h), body, -1)
    cv2.rectangle(frame, (x, y + h // 3), (x + w, y + h // 3 + h // 6), stripe, -1)


def jpeg(frame: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    return buf.tobytes()
