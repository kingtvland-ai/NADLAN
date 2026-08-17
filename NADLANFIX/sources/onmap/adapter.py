"""ONMAP source adapter for NADLANFIX.

Wraps onmap_feed.py to implement the unified BaseSourceAdapter interface.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sources.base.adapter import BaseSourceAdapter, SourceStatus
from sources.base.exceptions import SourceFetchError, SourcePersistError


class OnmapSourceAdapter(BaseSourceAdapter):
    """ONMAP source adapter implementing unified interface."""

    @property
    def source_name(self) -> str:
        return "onmap"

    @property
    def display_name(self) -> str:
        return "על המפה"

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create ONMAP tables if they don't exist."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS onmap_listings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                url TEXT,
                price REAL,
                price_text TEXT,
                currency TEXT DEFAULT 'ILS',
                city TEXT,
                neighborhood TEXT,
                address TEXT,
                address_text TEXT,
                rooms REAL,
                area_sqm REAL,
                floor TEXT,
                property_type TEXT DEFAULT 'unknown',
                condition TEXT DEFAULT 'unknown',
                image_url TEXT,
                images_json TEXT,
                agency TEXT,
                contact_name TEXT,
                phone TEXT,
                phone_source TEXT,
                phone_basis TEXT,
                description TEXT,
                listing_type TEXT,
                is_private INTEGER DEFAULT 0,
                is_broker INTEGER DEFAULT 0,
                brokerage TEXT,
                seller_listings INTEGER DEFAULT 0,
                seller_portfolio_censored INTEGER DEFAULT 0,
                seller_basis TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                created_at TEXT,
                delisted_at TEXT,
                contact_checked_at TEXT,
                seen_count INTEGER NOT NULL DEFAULT 1,
                raw_json TEXT,
                captured_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS onmap_price_history (
                id TEXT NOT NULL,
                price REAL,
                price_text TEXT,
                currency TEXT DEFAULT 'ILS',
                observed_at TEXT NOT NULL,
                PRIMARY KEY (id, observed_at)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS onmap_contacts (
                id TEXT PRIMARY KEY,
                phone TEXT,
                phone_source TEXT,
                phone_basis TEXT,
                agency TEXT,
                contact_name TEXT,
                checked_at TEXT,
                is_valid INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_onmap_city ON onmap_listings (city);
            CREATE INDEX IF NOT EXISTS idx_onmap_price ON onmap_listings (price);
            CREATE INDEX IF NOT EXISTS idx_onmap_last_seen ON onmap_listings (last_seen_at);
        """)
        conn.commit()

    def fetch(self, conn: sqlite3.Connection, target: int, **kwargs) -> list[dict]:
        """Fetch listings from ONMAP."""
        from ingestion.feeds import onmap_feed
        onmap_feed.harvest(target=target, **kwargs)
        return onmap_feed.rows(limit=target)

    def normalize(self, raw: dict) -> dict:
        """Normalize ONMAP listing to canonical format."""
        return {
            "canonical_id": f"onmap:{raw.get('id', '')}",
            "source": "onmap",
            "external_id": raw.get("id", ""),
            "title": raw.get("title", ""),
            "description": raw.get("description"),
            "normalized_title": raw.get("normalized_title", ""),
            "price": raw.get("price"),
            "price_text": raw.get("price_text"),
            "currency": raw.get("currency", "ILS"),
            "previous_price": None,
            "property_type": raw.get("property_type", "unknown"),
            "condition": raw.get("condition", "unknown"),
            "rooms": raw.get("rooms"),
            "area_sqm": raw.get("area_sqm"),
            "floor": raw.get("floor"),
            "bathrooms": raw.get("bathrooms"),
            "parking": raw.get("parking"),
            "city": raw.get("city", ""),
            "neighborhood": raw.get("neighborhood", ""),
            "street": raw.get("street", ""),
            "address_text": raw.get("address_text", ""),
            "lat": raw.get("lat"),
            "lon": raw.get("lon"),
            "gush": None,
            "helka": None,
            "image_url": raw.get("image"),
            "images": raw.get("images", []),
            "url": raw.get("url"),
            "source_query": "",
            "category": "real_estate",
            "classification_source": "heuristic",
            "classification_confidence": 1.0,
            "restricted": False,
            "deal_score": 0.0,
            "score_confidence": 0.0,
            "score_reasons": [],
            "seller_name": raw.get("contact_name"),
            "agency": raw.get("agency"),
            "phone": raw.get("phone"),
            "phone_source": raw.get("phone_source"),
            "first_seen_at": raw.get("first_seen_at", ""),
            "last_seen_at": raw.get("last_seen_at", ""),
            "captured_at": raw.get("created_at", ""),
            "is_active": True,
            "delisted_at": raw.get("delisted_at"),
            "seen_count": raw.get("seen_count", 1),
            "raw_json": json.dumps(raw, ensure_ascii=False),
            "schema_version": "1.0",
            "app_version": None,
        }

    def count(self, conn: sqlite3.Connection) -> dict:
        """Return count and freshness info."""
        from ingestion.feeds import onmap_feed
        return onmap_feed.counts()

    def _persist_listing(self, conn: sqlite3.Connection, listing: dict) -> bool:
        """Persist a normalized listing. Returns True if inserted."""
        existing = conn.execute(
            "SELECT canonical_id FROM normalized_listings WHERE canonical_id = ?",
            (listing["canonical_id"],)
        ).fetchone()

        now = self._now()

        if existing is None:
            conn.execute(
                """INSERT INTO normalized_listings
                   (canonical_id, source, external_id, title, description,
                    normalized_title, price, price_text, currency, previous_price,
                    property_type, condition, rooms, area_sqm, floor, bathrooms,
                    parking, city, neighborhood, street, address_text, lat, lon,
                    gush, helka, image_url, images_json, url, source_query,
                    category, classification_source, classification_confidence,
                    restricted, deal_score, score_confidence, score_reasons_json,
                    seller_name, agency, phone, phone_source, first_seen_at,
                    last_seen_at, captured_at, is_active, delisted_at, seen_count,
                    raw_json, schema_version, app_version, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing["canonical_id"], listing["source"], listing["external_id"],
                    listing["title"], listing.get("description"),
                    listing.get("normalized_title", ""), listing.get("price"),
                    listing.get("price_text"), listing.get("currency", "ILS"),
                    listing.get("previous_price"), listing.get("property_type", "unknown"),
                    listing.get("condition", "unknown"), listing.get("rooms"),
                    listing.get("area_sqm"), listing.get("floor"), listing.get("bathrooms"),
                    listing.get("parking"), listing["city"], listing.get("neighborhood"),
                    listing.get("street", ""), listing.get("address_text", ""),
                    listing.get("lat"), listing.get("lon"), listing.get("gush"),
                    listing.get("helka"), listing.get("image_url"),
                    json.dumps(listing.get("images", [])), listing.get("url"),
                    listing.get("source_query", ""), listing.get("category", "other"),
                    listing.get("classification_source", "heuristic"),
                    listing.get("classification_confidence", 0.0),
                    listing.get("restricted", False), listing.get("deal_score", 0.0),
                    listing.get("score_confidence", 0.0),
                    json.dumps(listing.get("score_reasons", [])),
                    listing.get("seller_name"), listing.get("agency"),
                    listing.get("phone"), listing.get("phone_source"),
                    listing.get("first_seen_at", now), now, now, 1,
                    listing.get("delisted_at"), 1,
                    listing.get("raw_json"), "1.0", None, now, now,
                )
            )
            return True
        else:
            conn.execute(
                """UPDATE normalized_listings
                   SET last_seen_at = ?, seen_count = seen_count + 1,
                       price = ?, price_text = ?, is_active = 1
                   WHERE canonical_id = ?""",
                (now, listing.get("price"), listing.get("price_text"), listing["canonical_id"])
            )
            return False
