"""Normalized listings table and pipeline for NADLANFIX.

All sources write to a central normalized_listings table after normalization.
This is the single source of truth for listing data, used by:
- API endpoints
- Firestore sync
- Analytics and scoring
- Public site
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class NormalizedStore:
    """Central store for normalized listings."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create normalized listings table if it doesn't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS normalized_listings (
                    canonical_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    normalized_title TEXT NOT NULL,
                    price REAL,
                    price_text TEXT,
                    currency TEXT DEFAULT 'ILS',
                    previous_price REAL,
                    property_type TEXT DEFAULT 'other',
                    condition TEXT DEFAULT 'unknown',
                    rooms REAL,
                    area_sqm REAL,
                    floor TEXT,
                    bathrooms REAL,
                    parking REAL,
                    city TEXT NOT NULL,
                    neighborhood TEXT,
                    street TEXT,
                    address_text TEXT,
                    lat REAL,
                    lon REAL,
                    gush TEXT,
                    helka TEXT,
                    image_url TEXT,
                    images_json TEXT,
                    url TEXT,
                    source_query TEXT,
                    category TEXT DEFAULT 'other',
                    classification_source TEXT DEFAULT 'heuristic',
                    classification_confidence REAL NOT NULL DEFAULT 0,
                    restricted INTEGER NOT NULL DEFAULT 0,
                    deal_score REAL NOT NULL DEFAULT 0,
                    score_confidence REAL NOT NULL DEFAULT 0,
                    score_reasons_json TEXT,
                    seller_name TEXT,
                    agency TEXT,
                    phone TEXT,
                    phone_source TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    delisted_at TEXT,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    raw_json TEXT,
                    schema_version TEXT DEFAULT '1.0',
                    app_version TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS listing_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (canonical_id) REFERENCES normalized_listings(canonical_id)
                );

                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_id TEXT NOT NULL,
                    price REAL,
                    price_text TEXT,
                    currency TEXT DEFAULT 'ILS',
                    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (canonical_id) REFERENCES normalized_listings(canonical_id)
                );

                CREATE INDEX IF NOT EXISTS idx_normalized_source
                    ON normalized_listings (source, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_normalized_city
                    ON normalized_listings (city, is_active);
                CREATE INDEX IF NOT EXISTS idx_normalized_price
                    ON normalized_listings (price);
                CREATE INDEX IF NOT EXISTS idx_normalized_score
                    ON normalized_listings (deal_score DESC);
                CREATE INDEX IF NOT EXISTS idx_normalized_active
                    ON normalized_listings (is_active, last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_listing_history_canonical
                    ON listing_history (canonical_id, observed_at);
                CREATE INDEX IF NOT EXISTS idx_price_history_canonical
                    ON price_history (canonical_id, observed_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def upsert(self, listing: dict) -> bool:
        """Upsert a normalized listing.

        Args:
            listing: Canonical listing dict

        Returns:
            True if inserted, False if updated
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            existing = conn.execute(
                "SELECT canonical_id, price, title FROM normalized_listings WHERE canonical_id = ?",
                (listing["canonical_id"],)
            ).fetchone()

            if existing is None:
                # Insert new
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
                # Update existing - track changes
                changes = []
                fields = {
                    "price": (existing["price"], listing.get("price")),
                    "title": (existing["title"], listing.get("title")),
                    "is_active": (1, 1),  # Always mark as active on update
                }

                for field_name, (old_val, new_val) in fields.items():
                    if old_val != new_val:
                        changes.append((field_name, old_val, new_val))

                # Record history for changes
                for field_name, old_val, new_val in changes:
                    conn.execute(
                        "INSERT INTO listing_history (canonical_id, source, field_name, old_value, new_value) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (listing["canonical_id"], listing["source"], field_name,
                         str(old_val) if old_val is not None else None,
                         str(new_val) if new_val is not None else None)
                    )

                # Update the record
                conn.execute(
                    """UPDATE normalized_listings
                       SET last_seen_at = ?, seen_count = seen_count + 1,
                           price = ?, price_text = ?, title = ?, is_active = 1,
                           updated_at = ?
                       WHERE canonical_id = ?""",
                    (now, listing.get("price"), listing.get("price_text"),
                     listing.get("title"), now, listing["canonical_id"])
                )
                return False
        finally:
            conn.commit()
            conn.close()

    def get(self, canonical_id: str) -> Optional[dict]:
        """Get a normalized listing by canonical ID."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM normalized_listings WHERE canonical_id = ?",
                (canonical_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_active(self, source: str | None = None, limit: int = 1000, offset: int = 0) -> list[dict]:
        """Get active listings, optionally filtered by source."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM normalized_listings WHERE is_active = 1"
            params = []

            if source:
                query += " AND source = ?"
                params.append(source)

            query += " ORDER BY last_seen_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_active_filtered(self, source=None, city=None, property_type=None,
                            min_price=None, max_price=None, min_rooms=None, max_rooms=None,
                            limit=1000, offset=0) -> list[dict]:
        """Get active listings with optional filters."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM normalized_listings WHERE is_active = 1"
            params = []

            if source:
                query += " AND source = ?"
                params.append(source)
            if city:
                query += " AND city = ?"
                params.append(city)
            if property_type:
                query += " AND property_type = ?"
                params.append(property_type)
            if min_price is not None:
                query += " AND price >= ?"
                params.append(min_price)
            if max_price is not None:
                query += " AND price <= ?"
                params.append(max_price)
            if min_rooms is not None:
                query += " AND rooms >= ?"
                params.append(min_rooms)
            if max_rooms is not None:
                query += " AND rooms <= ?"
                params.append(max_rooms)

            query += " ORDER BY last_seen_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def count(self, source: str | None = None) -> dict:
        """Return count and freshness info."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            base_query = "SELECT COUNT(*) FROM normalized_listings WHERE is_active = 1"
            count_params = []
            if source:
                base_query += " AND source = ?"
                count_params.append(source)

            total = conn.execute(base_query, count_params).fetchone()[0]

            # Last seen
            last_seen = conn.execute(
                "SELECT MAX(last_seen_at) FROM normalized_listings WHERE is_active = 1"
            ).fetchone()[0]

            # New today
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            new_today = conn.execute(
                "SELECT COUNT(*) FROM normalized_listings WHERE is_active = 1 AND first_seen_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]

            return {
                "total": total,
                "last_seen": last_seen,
                "new_today": new_today,
            }
        finally:
            conn.close()

    def count_filtered(self, source=None, city=None, property_type=None,
                       min_price=None, max_price=None, min_rooms=None, max_rooms=None) -> dict:
        """Return count and freshness info with optional filters."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            query = "SELECT COUNT(*) FROM normalized_listings WHERE is_active = 1"
            params = []

            if source:
                query += " AND source = ?"
                params.append(source)
            if city:
                query += " AND city = ?"
                params.append(city)
            if property_type:
                query += " AND property_type = ?"
                params.append(property_type)
            if min_price is not None:
                query += " AND price >= ?"
                params.append(min_price)
            if max_price is not None:
                query += " AND price <= ?"
                params.append(max_price)
            if min_rooms is not None:
                query += " AND rooms >= ?"
                params.append(min_rooms)
            if max_rooms is not None:
                query += " AND rooms <= ?"
                params.append(max_rooms)

            total = conn.execute(query, params).fetchone()[0]

            last_seen = conn.execute(
                "SELECT MAX(last_seen_at) FROM normalized_listings WHERE is_active = 1"
            ).fetchone()[0]

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            new_today = conn.execute(
                "SELECT COUNT(*) FROM normalized_listings WHERE is_active = 1 AND first_seen_at LIKE ?",
                (f"{today}%",)
            ).fetchone()[0]

            return {
                "total": total,
                "last_seen": last_seen,
                "new_today": new_today,
            }
        finally:
            conn.close()

    def mark_delisted(self, canonical_id: str) -> None:
        """Mark a listing as delisted."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE normalized_listings SET is_active = 0, delisted_at = ? WHERE canonical_id = ?",
                (now, canonical_id)
            )
            conn.commit()
        finally:
            conn.close()

    def get_history(self, canonical_id: str, limit: int = 50) -> list[dict]:
        """Get change history for a listing."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM listing_history WHERE canonical_id = ? ORDER BY observed_at DESC LIMIT ?",
                (canonical_id, limit)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_price_history(self, canonical_id: str, limit: int = 50) -> list[dict]:
        """Get price history for a listing."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM price_history WHERE canonical_id = ? ORDER BY observed_at DESC LIMIT ?",
                (canonical_id, limit)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
