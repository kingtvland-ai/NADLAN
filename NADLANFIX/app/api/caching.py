"""Caching and read optimization for NADLANFIX.

Provides:
- In-memory cache for frequently accessed data
- Cache invalidation strategies
- Precomputed summaries
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class CacheEntry:
    """A single cache entry."""
    key: str
    value: Any
    expires_at: float
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    metadata: dict = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class Cache:
    """In-memory cache with TTL support."""

    def __init__(self, default_ttl: int = 300):
        self._cache: dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        """Get a value from cache."""
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.is_expired:
            del self._cache[key]
            self._misses += 1
            return None
        self._hits += 1
        return entry.value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Set a value in cache."""
        ttl = ttl or self._default_ttl
        self._cache[key] = CacheEntry(
            key=key,
            value=value,
            expires_at=time.time() + ttl,
        )

    def delete(self, key: str) -> None:
        """Delete a value from cache."""
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all entries matching a pattern."""
        keys_to_delete = [k for k in self._cache if pattern in k]
        for key in keys_to_delete:
            del self._cache[key]
        return len(keys_to_delete)

    def get_stats(self) -> dict:
        """Get cache statistics."""
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 2) if total > 0 else 0.0,
        }

    def cleanup_expired(self) -> int:
        """Remove expired entries."""
        expired = [k for k, v in self._cache.items() if v.is_expired]
        for key in expired:
            del self._cache[key]
        return len(expired)


class ReadOptimizer:
    """Read optimization for frequently accessed queries."""

    def __init__(self, db_path: Path | str, cache: Cache | None = None):
        self.db_path = Path(db_path)
        self.cache = cache or Cache(default_ttl=300)

    def get_listings_summary(self, source: str | None = None) -> dict:
        """Get cached listings summary."""
        cache_key = f"listings:summary:{source or 'all'}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        # Compute summary
        conn = sqlite3.connect(str(self.db_path))
        try:
            query = "SELECT COUNT(*) as total, MIN(price) as min_price, MAX(price) as max_price, AVG(price) as avg_price FROM normalized_listings WHERE is_active = 1"
            params = []
            if source:
                query += " AND source = ?"
                params.append(source)

            row = conn.execute(query, params).fetchone()
            summary = {
                "total": row[0],
                "min_price": row[1],
                "max_price": row[2],
                "avg_price": round(row[3], 2) if row[3] else None,
            }

            self.cache.set(cache_key, summary, ttl=600)
            return summary
        finally:
            conn.close()

    def get_city_stats(self, city: str) -> dict | None:
        """Get cached city statistics."""
        cache_key = f"city:stats:{city}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                """SELECT COUNT(*) as total, MIN(price) as min_price, MAX(price) as max_price,
                          AVG(price) as avg_price, AVG(rooms) as avg_rooms, AVG(area_sqm) as avg_area
                   FROM normalized_listings
                   WHERE is_active = 1 AND city = ?""",
                (city,)
            ).fetchone()

            if row[0] == 0:
                return None

            stats = {
                "city": city,
                "total": row[0],
                "min_price": row[1],
                "max_price": row[2],
                "avg_price": round(row[3], 2) if row[3] else None,
                "avg_rooms": round(row[4], 2) if row[4] else None,
                "avg_area": round(row[5], 2) if row[5] else None,
            }

            self.cache.set(cache_key, stats, ttl=900)
            return stats
        finally:
            conn.close()

    def invalidate_listings_cache(self) -> None:
        """Invalidate all listings-related cache entries."""
        self.cache.invalidate_pattern("listings:")
        self.cache.invalidate_pattern("city:")
