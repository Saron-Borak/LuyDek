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
import json
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


NAME_PROMPT = """This photo shows one product on a shop counter in Cambodia. The shop's product list
doesn't have it yet. Read the package and give its name the way a shopkeeper would write it
on a shelf label: brand, product, variant or flavour, and size, e.g. "Oishi green tea honey
lemon 380ml" or "Paracetamol 500mg (10 tablets)". Only use what is printed or unmistakable
(a well-known brand's logo); don't invent a size you can't read. If there is Khmer text naming
it, give that too. If you can't tell what product it is, answer with sure = false."""


class NameReading(BaseModel):
    name_en: str = Field(description="Brand, product, variant and size, in English/Latin letters; empty if unknown")
    name_km: str = Field(description="The Khmer name printed on the package, or empty")
    sure: bool = Field(description="True only if the brand and product are readable or unmistakable")


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


def _image_block(crop: Image.Image) -> dict:
    img = crop.convert("RGB")
    img.thumbnail((768, 768))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def read_name(crop: Image.Image, cfg: Config, client: anthropic.Anthropic | None = None) -> NameReading:
    """What a product the shop hasn't listed yet is called, read off its package."""
    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=cfg.labels.model,
        max_tokens=4096,
        messages=[{"role": "user", "content": [_image_block(crop), {"type": "text", "text": NAME_PROMPT}]}],
        output_format=NameReading,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return NameReading(name_en="", name_km="", sure=False)
    return response.parsed_output


def name_new(cfg: Config, txn_id: int | None = None, client: anthropic.Anthropic | None = None) -> int:
    """Read a name for unanswered items the camera couldn't place (low confidence), so the owner can
    add the product with one tap. Returns how many got a name.

    If the name read is already a product ("Coca-Cola can 330ml"), that product becomes the item's
    first suggestion instead: it was a known product in an unusual pose, not a new one.
    """
    if not cfg.labels.enabled or not cfg.labels.name_new or (client is None and not cfg.anthropic_key_present):
        return 0
    with db.session(cfg) as conn:
        rows = [r for r in transactions.pending_items(conn)
                if r["name_checked_at"] is None and r["crop_path"] and r["method"] != "tray" and r["tier"] == "low"
                and (txn_id is None or r["txn_id"] == txn_id)]
    named = 0
    for r in rows:
        try:
            reading = read_name(Image.open(cfg.media_dir / r["crop_path"]), cfg, client)
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            log.info("name reader offline, will retry later: %s", exc)
            continue
        except (anthropic.APIStatusError, OSError) as exc:
            log.warning("name reader failed for item %s: %s", r["id"], exc)
            reading = NameReading(name_en="", name_km="", sure=False)
        with db.session(cfg) as conn:
            guess = reading.name_en.strip() if reading.sure else ""
            guess_km = reading.name_km.strip() if reading.sure else ""
            known = catalog.resolve_alias(conn, guess) if guess else None
            if known is None and guess:
                scored = catalog.fuzzy_scored(conn, guess, 1)
                known = scored[0][0] if scored and scored[0][1] >= cfg.labels.name_match else None
            if known is not None:
                pack_id = catalog.default_pack_id(conn, known)
                cands = [(pack_id, r["confidence"])] + [(p, s) for p, s in transactions.candidates(r) if p != pack_id]
                conn.execute("UPDATE transaction_items SET candidates_json = ?, name_checked_at = ? WHERE id = ?",
                             (json.dumps([[p, round(s, 4)] for p, s in cands[:5]]), clock.stamp(), r["id"]))
            else:
                conn.execute("UPDATE transaction_items SET name_guess = ?, name_guess_km = ?, name_checked_at = ? "
                             "WHERE id = ?", (guess or None, guess_km or None, clock.stamp(), r["id"]))
                named += bool(guess)
    return named


def settle(cfg: Config, txn_id: int | None = None, client: anthropic.Anthropic | None = None) -> int:
    """Try the label reader on pending look-alike items not yet looked at. Returns how many it settled.

    Safe to call often: each item is looked at once, and nothing is sent without
    an API key or when the feature is switched off.
    """
    if not cfg.labels.enabled or (client is None and not cfg.anthropic_key_present):
        return 0
    with db.session(cfg) as conn:
        rows = [r for r in transactions.pending_items(conn)
                if r["label_checked_at"] is None and r["crop_path"] and r["method"] != "tray"  # tray.settle's
                and (txn_id is None or r["txn_id"] == txn_id)]
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
