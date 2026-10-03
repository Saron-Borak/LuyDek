"""Count loose tablets on the dispensing tray.

A pharmacy counter gets a small matte black card, marked on the Counter page.
Cut blister pieces and loose tablets go on it before they are bagged, and the
box they came from goes down beside it. Against the empty-counter photo,
whatever differs on the card is a piece; its area over the learned size of one
tablet of that medicine is how many tablets it holds. Which medicine it is
comes from the box (see `transactions.close_transaction`).

A count is "sure" only when every piece is close to a whole number of tablets
of a size learned from earlier checked counts. Otherwise the side-by-side photo
(box and tray) goes to Claude, and if Claude can't tell either, to the owner;
their answer teaches the tablet size, so the tray needs them less over time.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass

import anthropic
import cv2
import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from .. import clock, db
from ..config import Config, TrayCfg
from ..services import catalog, transactions
from . import segment

log = logging.getLogger("luydek.tray")


@dataclass
class TrayReading:
    areas: list[float]                 # each piece's area as a share of the frame, biggest first
    oversized: bool                    # something too big to be tablets lay on the tray
    bbox: tuple[int, int, int, int]    # the tray, x, y, w, h in full-resolution pixels
    crop: Image.Image


def read(frame: np.ndarray, reference: np.ndarray, tray_roi: segment.Roi, cfg: TrayCfg) -> TrayReading:
    """The pieces lying on the tray, measured against the empty-counter photo."""
    h, w = frame.shape[:2]
    if reference.shape[:2] != (h, w):
        reference = cv2.resize(reference, (w, h), interpolation=cv2.INTER_AREA)
    x0, y0, x1, y1 = segment.roi_box(tray_roi, w, h)
    a = cv2.GaussianBlur(frame[y0:y1, x0:x1], (3, 3), 0)
    b = cv2.GaussianBlur(reference[y0:y1, x0:x1], (3, 3), 0)
    la = cv2.cvtColor(a, cv2.COLOR_BGR2LAB).astype(np.int16)
    lb = cv2.cvtColor(b, cv2.COLOR_BGR2LAB).astype(np.int16)
    diff = np.abs(la - lb)
    mask = ((cfg.lightness_weight * diff[..., 0] + diff[..., 1] + diff[..., 2]) > cfg.diff_threshold)
    mask = mask.astype(np.uint8) * 255
    k = max(3, (min(x1 - x0, y1 - y0) // 100) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))

    tray_area = max(1, (x1 - x0) * (y1 - y0))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    areas, oversized = [], False
    for i in range(1, n):  # label 0 is the empty card
        px = int(stats[i, cv2.CC_STAT_AREA])
        if px < cfg.min_piece_frac * tray_area:
            continue  # a crumb or dust
        if px > cfg.max_piece_frac * tray_area:
            oversized = True  # a box, a hand: not tablets
            continue
        areas.append(px / (w * h))
    crop = Image.fromarray(cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2RGB))
    return TrayReading(sorted(areas, reverse=True), oversized, (x0, y0, x1 - x0, y1 - y0), crop)


# --- Claude as a second pair of eyes ---------------------------------------------------------

PROMPT = """This photo is from a pharmacy counter in Cambodia. {layout}

1. Count the tablets or capsules being sold on the black tray. A loose tablet counts 1. A piece cut
   from a blister strip counts its FILLED pockets (a tablet still inside). Empty pockets, crumbs
   and packaging count 0.
{which}
If tablets overlap, are cut off at the edge, or the photo is unclear, set count_sure = false
rather than guessing."""

LAYOUT_BOTH = ("The left part shows the box or strip the tablets came from; the right part shows a small "
               "black counting tray with the tablets being sold loose.")
LAYOUT_TRAY = "It shows a small black counting tray with tablets being sold loose."
WHICH = """2. Which medicine is it? Decide ONLY from text printed on the box or the foil (name, strength
   in mg), in Khmer or English. The options:
{options}
   If no readable text decides it, set medicine_sure = false."""


class TrayCount(BaseModel):
    count: int | None = Field(description="How many tablets or capsules are being sold")
    count_sure: bool = Field(description="True only if every tablet is clearly visible and counted")
    text_seen: str = Field(description="Medicine name or strength readable on the box or foil, verbatim; empty if none")
    choice: int | None = Field(description="The number of the matching medicine option, or null")
    medicine_sure: bool = Field(description="True only if printed text clearly decides the medicine")


def read_count(image: Image.Image, options: list[str], with_source: bool, cfg: Config,
               client: anthropic.Anthropic | None = None) -> TrayCount:
    client = client or anthropic.Anthropic()
    img = image.convert("RGB")
    img.thumbnail((1024, 1024))  # small tablets need a little more resolution than a label
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    which = WHICH.format(options="\n".join(f"   {i}. {o}" for i, o in enumerate(options, 1))) if options else ""
    text = PROMPT.format(layout=LAYOUT_BOTH if with_source else LAYOUT_TRAY, which=which)
    response = client.beta.messages.parse(
        model=cfg.labels.model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": text},
            ],
        }],
        output_format=TrayCount,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return TrayCount(count=None, count_sure=False, text_seen="", choice=None, medicine_sure=False)
    return response.parsed_output


def settle(cfg: Config, txn_id: int | None = None, client: anthropic.Anthropic | None = None) -> int:
    """Ask Claude about pending tray items not yet looked at. Returns how many it settled.

    Only what the tray could not decide is asked: the count, the medicine, or
    both. A clear answer records the sale; anything else becomes the guess the
    owner confirms. Each item is looked at once; nothing is sent without an API
    key or with the label reader switched off.
    """
    if not cfg.labels.enabled or (client is None and not cfg.anthropic_key_present):
        return 0
    with db.session(cfg) as conn:
        jobs = []
        for r in transactions.pending_items(conn):
            if r["method"] != "tray" or r["label_checked_at"] is not None or not r["crop_path"]:
                continue
            if txn_id is not None and r["txn_id"] != txn_id:
                continue
            info = json.loads(r["tray_json"] or "{}")
            options: list[int] = []
            if not info.get("product_sure"):
                options = ([r["pack_id"]] if r["pack_id"] else []) + [p for p, _ in transactions.candidates(r)]
                options = list(dict.fromkeys(options))[:6]
            labels = [catalog.pack_label(catalog.pack(conn, p), "en") for p in options]
            jobs.append((r["id"], r["crop_path"], info, options, labels))

    settled = 0
    for item_id, crop_path, info, options, labels in jobs:
        try:
            reading = read_count(Image.open(cfg.media_dir / crop_path), labels, bool(info.get("with_source")),
                                 cfg, client)
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            log.info("tray reader offline, will retry later: %s", exc)
            continue  # not marked checked: the next sweep tries again
        except (anthropic.APIStatusError, OSError) as exc:
            log.warning("tray reader failed for item %s: %s", item_id, exc)
            reading = TrayCount(count=None, count_sure=False, text_seen="", choice=None, medicine_sure=False)
        with db.session(cfg) as conn:
            row = transactions.item(conn, item_id)
            if row is None or row["status"] != "pending":
                continue
            pack_id, count = row["pack_id"], row["count"]
            product_ok, count_ok, counted = bool(info.get("product_sure")), bool(info.get("count_sure")), False
            if not product_ok and reading.choice is not None and 1 <= reading.choice <= len(options):
                if options[reading.choice - 1] != pack_id:
                    count_ok = False  # the count assumed another medicine's tablet size
                pack_id = options[reading.choice - 1]
                product_ok = reading.medicine_sure
            if reading.count and reading.count > 0 and not count_ok:
                count, count_ok, counted = reading.count, reading.count_sure, reading.count_sure
            if product_ok and count_ok and pack_id is not None:
                if transactions.settle_tray(conn, item_id, pack_id, count, reading.text_seen, learn=counted,
                                            media_dir=cfg.media_dir,
                                            delete_photo=cfg.verification.delete_photos_after_verify):
                    settled += 1
            else:
                # Not decided, but a better guess for the owner's question.
                conn.execute("UPDATE transaction_items SET pack_id = ?, count = ?, label_text = ?, label_checked_at = ? "
                             "WHERE id = ?", (pack_id, count, reading.text_seen or None, clock.stamp(), item_id))
    return settled
