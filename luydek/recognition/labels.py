"""Read the label when the camera's best guesses are look-alikes.

CLIP sees shape and colour, so Paracetamol 250mg and 500mg, or Ichitan 420ml
and a 380ml bottle, look the same to it. When the top candidates for a pending
item score within `labels.gap` of each other, the product crop (not the whole
counter photo) goes to Claude with just those options, and Claude reads the
printed size, strength or flavour. A clear answer settles the item as an
automatic sale; anything unclear stays with the owner, as before.
"""

from __future__ import annotations

import base64
import io
import logging

import anthropic
from PIL import Image
from pydantic import BaseModel, Field

from .. import clock, db
from ..config import Config
from ..services import catalog, transactions

log = logging.getLogger("luydek.labels")

PROMPT = """This photo shows one product on a shop counter in Cambodia. It is one of these options,
which look alike:

{options}

Decide ONLY from text printed on the package: size (ml, g, L), strength (mg), flavour or
variant name, in Khmer or English. Colour and shape don't count; the options already share them.
If the words or numbers that tell the options apart are not readable in this photo, answer
with sure = false rather than guessing."""


class LabelReading(BaseModel):
    text_seen: str = Field(description="The size, strength, flavour or name text readable on the package, verbatim")
    choice: int | None = Field(description="The number of the matching option, or null if none matches")
    sure: bool = Field(description="True only if readable printed text clearly decides between the options")


def lookalikes(candidates: list[tuple[int, float]], gap: float, limit: int) -> list[int]:
    """Candidate pack ids scoring within `gap` of the best one, if there are at least two."""
    if len(candidates) < 2:
        return []
    top = candidates[0][1]
    close = [pack_id for pack_id, score in candidates if top - score <= gap][:limit]
    return close if len(close) >= 2 else []


def read(crop: Image.Image, options: list[str], cfg: Config, client: anthropic.Anthropic | None = None) -> LabelReading:
    client = client or anthropic.Anthropic()
    img = crop.convert("RGB")
    img.thumbnail((768, 768))  # a product crop; more pixels don't read small print better than this
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    response = client.beta.messages.parse(
        model=cfg.labels.model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": PROMPT.format(options="\n".join(f"{i}. {o}" for i, o in enumerate(options, 1)))},
            ],
        }],
        output_format=LabelReading,
        output_config={"effort": "low"},  # a short look-and-pick; low effort is plenty
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return LabelReading(text_seen="", choice=None, sure=False)
    return response.parsed_output


def settle(cfg: Config, txn_id: int | None = None, client: anthropic.Anthropic | None = None) -> int:
    """Try the label reader on pending look-alike items not yet looked at. Returns how many it settled.

    Safe to call often: each item is looked at once, and nothing is sent without
    an API key or when the feature is switched off.
    """
    if not cfg.labels.enabled or (client is None and not cfg.anthropic_key_present):
        return 0
    with db.session(cfg) as conn:
        rows = [r for r in transactions.pending_items(conn)
                if r["label_checked_at"] is None and r["crop_path"] and (txn_id is None or r["txn_id"] == txn_id)]
        jobs = []
        for r in rows:
            options = lookalikes(transactions.candidates(r), cfg.labels.gap, cfg.labels.max_options)
            if options:
                labels = [catalog.pack_label(catalog.pack(conn, p), "en") for p in options]
                jobs.append((r["id"], r["crop_path"], options, labels))
            else:
                conn.execute("UPDATE transaction_items SET label_checked_at = ? WHERE id = ?", (clock.stamp(), r["id"]))

    settled = 0
    for item_id, crop_path, options, labels in jobs:
        try:
            reading = read(Image.open(cfg.media_dir / crop_path), labels, cfg, client)
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            log.info("label reader offline, will retry later: %s", exc)
            continue  # not marked checked: the next sweep tries again
        except (anthropic.APIStatusError, OSError) as exc:
            log.warning("label reader failed for item %s: %s", item_id, exc)
            reading = LabelReading(text_seen="", choice=None, sure=False)
        with db.session(cfg) as conn:
            if reading.sure and reading.choice is not None and 1 <= reading.choice <= len(options):
                if transactions.settle_by_label(conn, item_id, options[reading.choice - 1], reading.text_seen,
                                                cfg.media_dir, cfg.verification.delete_photos_after_verify):
                    settled += 1
            else:
                conn.execute("UPDATE transaction_items SET label_text = ?, label_checked_at = ? WHERE id = ?",
                             (reading.text_seen or None, clock.stamp(), item_id))
    return settled
