"""Canonical listing model for NADLANFIX.

All sources normalize their data to this canonical format.
This ensures consistent handling across the system regardless of source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class CanonicalListing:
    """Unified listing format across all sources."""

    # Identity
    listing_id: str
    source: str  # 'yad2', 'facebook', 'onmap', 'ad', 'madlan', 'komo'
    external_id: str  # Original ID from the source

    # Content
    title: str
    description: Optional[str] = None
    normalized_title: str = ""

    # Pricing
    price: Optional[float] = None
    price_text: Optional[str] = None
    currency: str = "ILS"
    previous_price: Optional[float] = None

    # Property details
    property_type: str = "other"
    condition: str = "unknown"
    rooms: Optional[float] = None
    area_sqm: Optional[float] = None
    floor: Optional[str] = None
    bathrooms: Optional[float] = None
    parking: Optional[float] = None

    # Location
    city: str = ""
    neighborhood: str = ""
    street: str = ""
    address_text: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    gush: Optional[str] = None
    helka: Optional[str] = None

    # Media
    image_url: Optional[str] = None
    images: list[str] = field(default_factory=list)
    url: Optional[str] = None

    # Source metadata
    source_query: str = ""
    category: str = "other"
    classification_source: str = "heuristic"
    classification_confidence: float = 0.0
    restricted: bool = False

    # Scoring
    deal_score: float = 0.0
    score_confidence: float = 0.0
    score_reasons: list[str] = field(default_factory=list)

    # Seller info
    seller_name: Optional[str] = None
    agency: Optional[str] = None
    phone: Optional[str] = None
    phone_source: Optional[str] = None

    # Timestamps
    first_seen_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    last_seen_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # Lifecycle
    is_active: bool = True
    delisted_at: Optional[str] = None
    seen_count: int = 1

    # Raw data
    raw_json: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "listing_id": self.listing_id,
            "source": self.source,
            "external_id": self.external_id,
            "title": self.title,
            "description": self.description,
            "normalized_title": self.normalized_title,
            "price": self.price,
            "price_text": self.price_text,
            "currency": self.currency,
            "previous_price": self.previous_price,
            "property_type": self.property_type,
            "condition": self.condition,
            "rooms": self.rooms,
            "area_sqm": self.area_sqm,
            "floor": self.floor,
            "bathrooms": self.bathrooms,
            "parking": self.parking,
            "city": self.city,
            "neighborhood": self.neighborhood,
            "street": self.street,
            "address_text": self.address_text,
            "lat": self.lat,
            "lon": self.lon,
            "gush": self.gush,
            "helka": self.helka,
            "image_url": self.image_url,
            "images": self.images,
            "url": self.url,
            "source_query": self.source_query,
            "category": self.category,
            "classification_source": self.classification_source,
            "classification_confidence": self.classification_confidence,
            "restricted": self.restricted,
            "deal_score": self.deal_score,
            "score_confidence": self.score_confidence,
            "score_reasons": self.score_reasons,
            "seller_name": self.seller_name,
            "agency": self.agency,
            "phone": self.phone,
            "phone_source": self.phone_source,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "captured_at": self.captured_at,
            "is_active": self.is_active,
            "delisted_at": self.delisted_at,
            "seen_count": self.seen_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> CanonicalListing:
        """Create from dictionary."""
        return cls(
            listing_id=data["listing_id"],
            source=data["source"],
            external_id=data["external_id"],
            title=data.get("title", ""),
            description=data.get("description"),
            normalized_title=data.get("normalized_title", ""),
            price=data.get("price"),
            price_text=data.get("price_text"),
            currency=data.get("currency", "ILS"),
            previous_price=data.get("previous_price"),
            property_type=data.get("property_type", "other"),
            condition=data.get("condition", "unknown"),
            rooms=data.get("rooms"),
            area_sqm=data.get("area_sqm"),
            floor=data.get("floor"),
            bathrooms=data.get("bathrooms"),
            parking=data.get("parking"),
            city=data.get("city", ""),
            neighborhood=data.get("neighborhood", ""),
            street=data.get("street", ""),
            address_text=data.get("address_text", ""),
            lat=data.get("lat"),
            lon=data.get("lon"),
            gush=data.get("gush"),
            helka=data.get("helka"),
            image_url=data.get("image_url"),
            images=data.get("images", []),
            url=data.get("url"),
            source_query=data.get("source_query", ""),
            category=data.get("category", "other"),
            classification_source=data.get("classification_source", "heuristic"),
            classification_confidence=data.get("classification_confidence", 0.0),
            restricted=data.get("restricted", False),
            deal_score=data.get("deal_score", 0.0),
            score_confidence=data.get("score_confidence", 0.0),
            score_reasons=data.get("score_reasons", []),
            seller_name=data.get("seller_name"),
            agency=data.get("agency"),
            phone=data.get("phone"),
            phone_source=data.get("phone_source"),
            first_seen_at=data.get("first_seen_at", ""),
            last_seen_at=data.get("last_seen_at", ""),
            captured_at=data.get("captured_at", ""),
            is_active=data.get("is_active", True),
            delisted_at=data.get("delisted_at"),
            seen_count=data.get("seen_count", 1),
        )
