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


class RecognitionCfg(BaseModel):
    model_name: str = "openai/clip-vit-base-patch32"
    device: str = "auto"
    t_high: float = 0.88
    m_high: float = 0.03
    t_low: float = 0.76
    top_k_per_pack: int = 3
    segment: SegmentCfg = Field(default_factory=SegmentCfg)


class VerificationCfg(BaseModel):
    digest_times: list[str] = ["09:30", "13:00", "20:30"]
    end_of_day: str = "22:00"
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
