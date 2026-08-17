# src/facebook_marketplace_scraper/models.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl


def utc_now() -> datetime:
    return datetime.now(UTC)


class RawListing(BaseModel):
    """DOM-derived listing candidate before normalization."""

    url: str
    text: str = ""
    title_hint: str | None = None
    price_text: str | None = None
    location_hint: str | None = None
    image_url: str | None = None
    description_hint: str | None = None
    category_hint: str | None = None
    condition_hint: str | None = None


class SearchSpec(BaseModel):
    query: str
    city_id: str
    radius_km: float = Field(default=5, ge=0)
<<<<<<< HEAD
    category_id: str | None = None
=======
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
    max_items: int = Field(default=20, ge=1, le=500)
    min_price: float | None = Field(default=None, ge=0)
    max_price: float | None = Field(default=None, ge=0)
    default_currency: str = "ILS"


class MarketplaceListing(BaseModel):
    """Normalized Marketplace listing persisted by the pipeline."""

    listing_id: str
    title: str
    normalized_title: str
    fingerprint: str
    url: HttpUrl
    price_text: str | None = None
    price_value: float | None = None
    currency: str | None = None
    location: str | None = None
    image_url: HttpUrl | None = None
    seller_name: str | None = None
    description: str | None = None
    category: str = "other"
    condition: str = "unknown"
    classification_source: str = "heuristic"
    classification_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    restricted: bool = False
    source_query: str
    captured_at: datetime = Field(default_factory=utc_now)


class ClassificationResult(BaseModel):
    category: str
    condition: str = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    restricted: bool = False
    source: str = "heuristic"


class PriceStats(BaseModel):
    sample_size: int = 0
    median_price: float | None = None
    min_price: float | None = None
    max_price: float | None = None
    previous_price: float | None = None
    category: str = "other"
    similarity_threshold: float = 0.6
    sample_target: int = 8


class ScoredListing(BaseModel):
    listing: MarketplaceListing
    deal_score: float
    confidence: float
    price_stats: PriceStats
    reasons: list[str] = Field(default_factory=list)


class Watchlist(BaseModel):
    id: int | None = None
    name: str
    query: str
    city_id: str
    radius_km: float = Field(default=5, ge=0)
<<<<<<< HEAD
    category_id: str | None = None
=======
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
    min_price: float | None = None
    max_price: float | None = None
    target_price: float | None = None
    max_items: int = Field(default=50, ge=1, le=500)
    default_currency: str = "ILS"
    interval_seconds: int = Field(default=1800, ge=60)
    enabled: bool = True
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class CollectionResult(BaseModel):
    query: str
    run_id: int
    extracted: int
    normalized: int
    inserted: int
    updated: int
    price_changes: int
    notifications: int = 0
    listings: list[ScoredListing] = Field(default_factory=list)


class NotificationEvent(BaseModel):
    listing_id: str
    watchlist_id: int | None = None
    event_type: str
    dedupe_key: str
    score: float
    payload: dict[str, object]
    created_at: datetime = Field(default_factory=utc_now)


class RetentionPolicy(BaseModel):
    price_history_days: int = Field(default=365, ge=1)
    search_run_days: int = Field(default=90, ge=1)
    notification_days: int = Field(default=90, ge=1)
    listing_days: int = Field(default=365, ge=1)
    interval_seconds: int = Field(default=21600, ge=300)


class FixtureCaptureSettings(BaseModel):
    output: Path
    max_items: int = Field(default=30, ge=1, le=500)
