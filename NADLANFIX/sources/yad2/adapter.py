"""Yad2 source adapter for NADLANFIX.

Wraps yad2_feed.py to implement the unified BaseSourceAdapter interface.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sources.base.adapter import BaseSourceAdapter, SourceStatus
from sources.base.exceptions import SourceFetchError, SourcePersistError


class Yad2SourceAdapter(BaseSourceAdapter):
    """Yad2 source adapter implementing unified interface."""

    @property
    def source_name(self) -> str:
        return "yad2"

    @property
    def display_name(self) -> str:
        return "יד 2"

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create Yad2 tables if they don't exist."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS yad2_listings (
                token TEXT PRIMARY KEY,
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
                dom_days INTEGER,
                dom_basis TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL DEFAULT 1,
                delisted_at TEXT,
                raw_json TEXT,
                captured_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS yad2_price_history (
                token TEXT NOT NULL,
                price REAL,
                price_text TEXT,
                currency TEXT DEFAULT 'ILS',
                observed_at TEXT NOT NULL,
                PRIMARY KEY (token, observed_at)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS yad2_harvest_runs (
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

            CREATE INDEX IF NOT EXISTS idx_yad2_city ON yad2_listings (city);
            CREATE INDEX IF NOT EXISTS idx_yad2_price ON yad2_listings (price);
            CREATE INDEX IF NOT EXISTS idx_yad2_last_seen ON yad2_listings (last_seen_at);
        """)
        conn.commit()

    def fetch(self, conn: sqlite3.Connection, target: int, **kwargs) -> list[dict]:
        """Fetch listings from Yad2."""
        from ingestion.feeds import yad2_feed
        return yad2_feed.harvest(target=target, **kwargs)

    def normalize(self, raw: dict) -> dict:
        """Normalize Yad2 listing to canonical format."""
        return {
            "listing_id": raw.get("token", ""),
            "source": "yad2",
            "external_id": raw.get("token", ""),
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
            "bathrooms": None,
            "parking": None,
            "city": raw.get("city", ""),
            "neighborhood": raw.get("neighborhood", ""),
            "street": raw.get("address", ""),
            "address_text": raw.get("address", ""),
            "lat": None,
            "lon": None,
            "gush": None,
            "helka": None,
            "image_url": raw.get("image_url"),
            "images": raw.get("images", []),
            "url": raw.get("url"),
            "source_query": "",
            "category": "real_estate",
            "classification_source": "heuristic",
            "classification_confidence": 1.0,
            "restricted": False,
            "deal_score": raw.get("deal_score", 0.0),
            "score_confidence": raw.get("score_confidence", 0.0),
            "score_reasons": raw.get("score_reasons", []),
            "seller_name": raw.get("contact_name"),
            "agency": raw.get("agency"),
            "phone": raw.get("phone"),
            "phone_source": raw.get("phone_source"),
            "first_seen_at": raw.get("first_seen_at", ""),
            "last_seen_at": raw.get("last_seen_at", ""),
            "captured_at": raw.get("captured_at", ""),
            "is_active": True,
            "delisted_at": raw.get("delisted_at"),
            "seen_count": raw.get("seen_count", 1),
        }

    def count(self, conn: sqlite3.Connection) -> dict:
        """Return count and freshness info."""
        from ingestion.feeds import yad2_feed
        return yad2_feed.counts()

    def _persist_listing(self, conn: sqlite3.Connection, listing: dict) -> bool:
        """Persist a normalized listing. Returns True if inserted."""
        existing = conn.execute(
            "SELECT token FROM yad2_listings WHERE token = ?",
            (listing["listing_id"],)
        ).fetchone()

        now = self._now()

        if existing is None:
            conn.execute(
                """INSERT INTO yad2_listings
                   (token, title, normalized_title, fingerprint, url, price, price_text,
                    currency, city, neighborhood, address, rooms, area_sqm, floor,
                    property_type, condition, image_url, images, agency, contact_name,
                    phone, phone_source, phone_basis, description, listing_type,
                    is_private, is_broker, brokerage, seller_listings,
                    seller_portfolio_censored, seller_basis, dom_days, dom_basis,
                    first_seen_at, last_seen_at, seen_count, captured_at, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing["listing_id"], listing["title"], listing["normalized_title"],
                    "", listing["url"], listing["price"], listing["price_text"],
                    listing["currency"], listing["city"], listing["neighborhood"],
                    listing["street"], listing["rooms"], listing["area_sqm"], listing["floor"],
                    listing["property_type"], listing["condition"], listing["image_url"],
                    json.dumps(listing.get("images", [])), listing["agency"],
                    listing["seller_name"], listing["phone"], listing["phone_source"],
                    "", listing["description"], "", 0, 0, "", 0, 0, "", 0, "",
                    listing["first_seen_at"], now, 1, now, 1,
                )
            )
            return True
        else:
            conn.execute(
                """UPDATE yad2_listings
                   SET last_seen_at = ?, seen_count = seen_count + 1,
                       price = ?, price_text = ?, is_active = 1
                   WHERE token = ?""",
                (now, listing["price"], listing["price_text"], listing["listing_id"])
            )
            return False
