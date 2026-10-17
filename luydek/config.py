"""Typed configuration loaded from configs/config.yaml plus secrets from .env."""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class ShopCfg(BaseModel):
    name: str = "Demo Mini-Mart"
    timezone: str = "Asia/Phnom_Penh"
    currency: str = "USD"
    pin: str = "2468"
    default_lang: str = "km"
    # The web pages' language until a phone picks its own (km or en). Shops set km in their config.
    web_lang: str = "en"
    # Tools for the install team and demos: "send now" buttons, the photo upload box, the
    # pilot report and the Telegram previews. Owners see a calmer app without them.
    team_tools: bool = False


class PathsCfg(BaseModel):
    db: str = "data/luydek.db"
    media: str = "data/media"
    seed_catalog: str = "data/seed_catalog.yaml"


class SegmentCfg(BaseModel):
    work_width: int = 960
    diff_threshold: float = 30
    lightness_weight: float = 0.8
    min_area_frac: float = 0.004
    max_area_frac: float = 0.60
    pad_frac: float = 0.08
    split_touching: bool = True
    split_solidity: float = 0.93  # only outlines less convex than this can be two items
    split_peak: float = 0.75     # an item's core: distance-to-edge above this x the highest
    split_min_frac: float = 0.12  # a split piece smaller than this share of the blob is noise
    scene_change_frac: float = 0.85  # this much of the mat unlike the empty photo: camera moved or light changed
    # Shadows and light changes: a patch of mat that is only darker or lighter than the empty-mat photo,
    # by about the same amount all over (smooth inside, soft at its edge), and keeps its colour, is not an
    # item. An item has edges and print inside it, or a sharp outline, even when it is dark grey on grey.
    shadow_filter: bool = True
    shadow_smooth: float = 0.6     # inside and at its edge, the brightness change varies at most this much
    shadow_darker: float = 0.3     # a shadow keeps at least this share of the light...
    shadow_lighter: float = 2.2    # ...and a shadow that has gone brightens the mat at most this much
    shadow_chroma: float = 0.08    # the colour (chromaticity) changes at most this much


class TrayCfg(BaseModel):
    """The dispensing tray: a small black card where loose tablets are put down before bagging."""
    diff_threshold: float = 40
    lightness_weight: float = 1.0   # white tablets on a black card differ mostly in lightness
    min_piece_frac: float = 0.002   # of the tray: smaller is a crumb or dust
    max_piece_frac: float = 0.30    # of the tray: bigger is not tablets (a box put on the tray)
    unit_tolerance: float = 0.35    # a piece must be within this many tablets of a whole number
    min_unit_samples: int = 2       # checked counts needed before the tray trusts its tablet size
    keep_unit_samples: int = 12


class DetectorCfg(BaseModel):
    """YOLOE-26 finds each item on the mat (recognition/detector.py). Switched off, or without its
    weights, the mat difference finds items on its own, as before."""
    enabled: bool = True
    weights: str = "models/yoloe-26s-seg-pf.pt"
    imgsz: int = 960           # the size the photo is looked at: smaller is faster, bigger sees small items
    conf: float = 0.25
    max_det: int = 100
    part_of: float = 0.8       # an outline this much within a bigger one (its dents filled) is part of it...
    group_min: float = 0.15    # ...unless the bigger one holds two boxes at least this big (share of it)
    apart_iou: float = 0.2     # that overlap each other less than this: then it is a box around a group
    same_iou: float = 0.6      # two boxes overlapping this much are the same object under two names
    min_changed: float = 0.15  # counter camera: share of a box that must differ from the empty mat


class RecognitionCfg(BaseModel):
    model_name: str = "openai/clip-vit-base-patch32"
    device: str = "auto"
    t_high: float = 0.88
    m_high: float = 0.03
    t_low: float = 0.76
    top_k_per_pack: int = 3
    learn_cap: int = 20          # most photos learned from sales kept per product (oldest dropped)
    # Hold-to-record: a photo this similar to one the product already has is skipped. Measured on
    # the shop's gallery: near-copies of one frame score 0.97-0.995, genuinely new angles mostly below.
    enroll_duplicate: float = 0.985
    # Things the shop taught the camera to ignore (phone, cash, a hand) are only set aside when they
    # clearly are one: that class must score this well AND beat every product by this much.
    ignore_min: float = 0.85
    ignore_margin: float = 0.03
    # "other" (learned from "Not a sale" answers) can be a product or the background, so it is
    # always asked about, never dropped. Only a patch of background this close to one the owner
    # already said wasn't a sale is set aside, and even that is kept in the camera log.
    junk_repeat: float = 0.95
    # A "detection" that looks like the same spot of the empty mat is usually a shifted paper, a
    # table edge or a patch of light: at or above this it is never recorded automatically, only
    # asked about. Measured on the shop's own frames: background changes 0.90-0.97, products put
    # down 0.57-0.74 (thin pens can reach 0.95, which is why it asks rather than drops).
    background_sim: float = 0.9
    ignored_keep_days: int = 14  # ignored detections (and their photos) stay reviewable this long
    colour_tiebreak: bool = True
    lookalike_gap: float = 0.05  # candidates within this CLIP score of the top one are look-alikes
    colour_margin: float = 0.15  # colour must beat the runner-up look-alike by this much to decide
    # Size on the mat: a 330ml can and a 1.5L bottle of the same brand look alike but aren't the same
    # size. From the calibrated counter camera, an item outside the size range of a pack's own photos
    # (widened by this factor both ways) is asked about, never accepted on its own. Needs at least
    # `size_min_photos` photos of the pack taken with the counter camera.
    size_check: bool = True
    size_tolerance: float = 1.4
    size_min_photos: int = 3
    # Look-alike pairs: two packs whose photos the camera mixes up with each other (found by matching
    # every photo against all the others). When the top two guesses are such a pair, the item is
    # asked about unless the first beats the second by at least this much.
    pair_margin: float = 0.08
    segment: SegmentCfg = Field(default_factory=SegmentCfg)
    detector: DetectorCfg = Field(default_factory=DetectorCfg)
    tray: TrayCfg = Field(default_factory=TrayCfg)


class VerificationCfg(BaseModel):
    digest_times: list[str] = ["09:30", "13:00", "20:30"]
    end_of_day: str = "22:00"
    daily_summary: bool = True   # right after end_of_day: what sold today and what is running out
    stock_alerts: bool = True    # a Telegram message the moment a product runs out or runs low
    spot_checks: int = 3         # products to count quickly each day (0: none)
    spot_check_time: str = "15:00"
    shelf_check_weekday: int = 0  # the weekly walk round the shop: what's expired, what goes in front (0 = Monday)
    shelf_check_time: str = "08:30"
    weekly_summary_time: str = "20:45"
    delete_photos_after_verify: bool = True
    stale_transaction_minutes: int = 10


class ForecastCfg(BaseModel):
    history_days: int = 28
    week_weights: list[float] = [4, 3, 2, 1]
    safety_days: float = 2
    safety_days_critical: float = 3
    overstock_extra_days: int = 14
    skip_no_sales_days: int = 30
    sleeping_days: int = 45
    sleeping_cover_days: int = 90
    expiry_warning_days: int = 30
    order_list_time: str = "18:00"
    observe_until: str | None = None


class LabelsCfg(BaseModel):
    # Read the printed size/strength/flavour when the camera's top guesses are look-alikes.
    enabled: bool = True
    model: str = "claude-opus-5"
    gap: float = 0.05            # candidates within this of the top score count as look-alikes
    max_options: int = 4
    # Read the name off a product the camera couldn't place, so the owner can add it with one tap.
    name_new: bool = True
    name_match: float = 0.9      # a name read this close to an existing product's name is that product


class PilotCfg(BaseModel):
    # Day 0 of the pilot. Empty: the first day any stock was recorded.
    start: str | None = None
    baseline_weeks: int = 2      # "observe" weeks: no recommendations yet, the before-picture
    report_weeks: int = 12       # at most this many weeks on the report


class TelegramCfg(BaseModel):
    # Only one server may poll a bot token. A second copy (the synthetic demo
    # shop next to the real one) must turn this off.
    enabled: bool = True
    # The Mini App (swipe to check items inside Telegram) needs an https address Telegram can open
    # on the phone, e.g. from `tailscale serve`. Empty: the bot sends one photo message per item.
    miniapp_url: str | None = None
    # On a PUBLIC address (a tunnel), serve only the Mini App there: the dashboard has no login yet.
    miniapp_host_only: bool = False


class InvoicesCfg(BaseModel):
    model: str = "claude-opus-5"
    max_long_edge: int = 1568
    khr_per_usd: float = 4100


class Config(BaseModel):
    shop: ShopCfg = Field(default_factory=ShopCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    recognition: RecognitionCfg = Field(default_factory=RecognitionCfg)
    verification: VerificationCfg = Field(default_factory=VerificationCfg)
    forecast: ForecastCfg = Field(default_factory=ForecastCfg)
    invoices: InvoicesCfg = Field(default_factory=InvoicesCfg)
    telegram: TelegramCfg = Field(default_factory=TelegramCfg)
    pilot: PilotCfg = Field(default_factory=PilotCfg)
    labels: LabelsCfg = Field(default_factory=LabelsCfg)
    root: Path = ROOT

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        return path if path.is_absolute() else self.root / path

    @property
    def db_path(self) -> Path:
        return self.resolve(self.paths.db)

    @property
    def media_dir(self) -> Path:
        return self.resolve(self.paths.media)

    @property
    def tz(self) -> timezone:
        """The shop's UTC offset as a fixed timezone.

        Cambodia has no daylight saving, and a fixed offset is accepted by every
        scheduler backend, which a ZoneInfo is not.
        """
        offset = datetime.now(ZoneInfo(self.shop.timezone)).utcoffset() or timedelta(0)
        return timezone(offset)

    @property
    def telegram_token(self) -> str | None:
        if not self.telegram.enabled:
            return None
        return os.environ.get("TELEGRAM_BOT_TOKEN") or None

    @property
    def bot_token(self) -> str | None:
        """The bot's token even where this server doesn't poll it: checking the Mini App's signed
        launch data needs only the token, not the bot."""
        return os.environ.get("TELEGRAM_BOT_TOKEN") or None

    @property
    def anthropic_key_present(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def parse_hhmm(value: str, tz: timezone | None = None) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes), tzinfo=tz)


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path or os.environ.get("LUYDEK_CONFIG") or ROOT / "configs" / "config.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = Config(**(data or {}))
    if os.environ.get("LUYDEK_DB"):
        cfg.paths.db = os.environ["LUYDEK_DB"]
    return cfg


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()
