"""Facebook Marketplace source adapter for NADLANFIX.

Wraps facebook_feed.py to implement the unified BaseSourceAdapter interface.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sources.base.adapter import BaseSourceAdapter, SourceStatus
from sources.base.exceptions import SourceFetchError, SourcePersistError


class FacebookSourceAdapter(BaseSourceAdapter):
    """Facebook Marketplace source adapter implementing unified interface."""

    @property
    def source_name(self) -> str:
        return "facebook"

    @property
    def display_name(self) -> str:
        return "Facebook Marketplace"

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create Facebook tables if they don't exist."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS facebook_listings (
                listing_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                url TEXT NOT NULL,
                price_text TEXT,
                price_value REAL,
                currency TEXT,
                location TEXT,
                image_url TEXT,
                seller_name TEXT,
                description TEXT,
                category TEXT DEFAULT 'other',
                condition TEXT DEFAULT 'unknown',
                classification_source TEXT DEFAULT 'heuristic',
                classification_confidence REAL NOT NULL DEFAULT 0,
                restricted INTEGER NOT NULL DEFAULT 0,
                city_id TEXT,
                city_name TEXT,
                source_query TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                delisted_at TEXT,
                raw_json TEXT,
                captured_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS facebook_price_history (
                listing_id TEXT NOT NULL,
                price_text TEXT,
                price_value REAL,
                currency TEXT,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (listing_id, observed_at)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS facebook_harvest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                pages_fetched INTEGER NOT NULL DEFAULT 0,
                pages_failed INTEGER NOT NULL DEFAULT 0,
                rows_seen INTEGER NOT NULL DEFAULT 0,
                rows_new INTEGER NOT NULL DEFAULT 0,
                target INTEGER,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS facebook_cities (
                city_name TEXT PRIMARY KEY,
                city_id TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_facebook_city ON facebook_listings (city_name);
            CREATE INDEX IF NOT EXISTS idx_facebook_price ON facebook_listings (price_value);
            CREATE INDEX IF NOT EXISTS idx_facebook_last_seen ON facebook_listings (last_seen_at);
        """)
        conn.commit()

    def fetch(self, conn: sqlite3.Connection, target: int, **kwargs) -> list[dict]:
        """Fetch listings from Facebook Marketplace."""
        from ingestion.feeds import facebook_feed
        facebook_feed.harvest(target=target, **kwargs)
        return facebook_feed.rows(limit=target)

    def normalize(self, raw: dict) -> dict:
        """Normalize Facebook listing to canonical format."""
        return {
            "canonical_id": f"facebook:{raw.get('listing_id', '')}",
            "source": "facebook",
            "external_id": raw.get("listing_id", ""),
            "title": raw.get("title", ""),
            "description": raw.get("description"),
            "normalized_title": raw.get("normalized_title", ""),
            "price": raw.get("price_value"),
            "price_text": raw.get("price_text"),
            "currency": raw.get("currency", "ILS"),
            "previous_price": None,
            "property_type": raw.get("category", "other"),
            "condition": raw.get("condition", "unknown"),
            "rooms": None,
            "area_sqm": None,
            "floor": None,
            "bathrooms": None,
            "parking": None,
            "city": raw.get("city_name", ""),
            "neighborhood": raw.get("location", ""),
            "street": "",
            "address_text": raw.get("location", ""),
            "lat": None,
            "lon": None,
            "gush": None,
            "helka": None,
            "image_url": raw.get("image_url"),
            "images": [raw.get("image_url")] if raw.get("image_url") else [],
            "url": raw.get("url"),
            "source_query": raw.get("source_query", ""),
            "category": raw.get("category", "other"),
            "classification_source": raw.get("classification_source", "heuristic"),
            "classification_confidence": raw.get("classification_confidence", 0.0),
            "restricted": bool(raw.get("restricted", False)),
            "deal_score": raw.get("deal_score", 0.0),
            "score_confidence": raw.get("score_confidence", 0.0),
            "score_reasons": raw.get("score_reasons", []),
            "seller_name": raw.get("seller_name"),
            "agency": None,
            "phone": None,
            "phone_source": None,
            "first_seen_at": raw.get("first_seen_at", ""),
            "last_seen_at": raw.get("last_seen_at", ""),
            "captured_at": raw.get("last_seen_at", ""),
            "is_active": True,
            "delisted_at": raw.get("delisted_at"),
            "seen_count": raw.get("seen_count", 1),
            "raw_json": json.dumps(raw, ensure_ascii=False),
            "schema_version": "1.0",
            "app_version": None,
        }

    def count(self, conn: sqlite3.Connection) -> dict:
        """Return count and freshness info."""
        from ingestion.feeds import facebook_feed
        return facebook_feed.counts()

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
                    listing.get("previous_price"), listing.get("property_type", "other"),
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
