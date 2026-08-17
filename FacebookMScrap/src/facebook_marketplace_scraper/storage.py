# src/facebook_marketplace_scraper/storage.py
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from .comparables import comparable_anchor, title_similarity
from .israel_cities import ISRAEL_CITY_IDS
from .models import MarketplaceListing, NotificationEvent, PriceStats, RetentionPolicy, Watchlist
from .valuation import valuation_profile

<<<<<<< HEAD
LATEST_SCHEMA_VERSION = 6
=======
LATEST_SCHEMA_VERSION = 4
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a

_BASE_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    url TEXT NOT NULL,
    currency TEXT,
    location TEXT,
    image_url TEXT,
    seller_name TEXT,
    source_query TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    latest_price_text TEXT,
    latest_price_value REAL,
    deal_score REAL NOT NULL DEFAULT 0,
    score_confidence REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listings_normalized_title ON listings(normalized_title);
CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_listings_deal_score ON listings(deal_score DESC);

CREATE TABLE IF NOT EXISTS listing_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    price_text TEXT,
    price_value REAL,
    currency TEXT,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_listing_time ON listing_prices(listing_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    extracted INTEGER NOT NULL DEFAULT 0,
    normalized INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    price_changes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS search_run_listings (
    run_id INTEGER NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    listing_id TEXT NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    PRIMARY KEY (run_id, listing_id)
);

CREATE TABLE IF NOT EXISTS watchlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    query TEXT NOT NULL,
    min_price REAL,
    max_price REAL,
    target_price REAL,
    max_items INTEGER NOT NULL DEFAULT 50,
    default_currency TEXT NOT NULL DEFAULT 'ILS',
    interval_seconds INTEGER NOT NULL DEFAULT 1800,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_matches (
    watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    listing_id TEXT NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    first_matched TEXT NOT NULL,
    last_matched TEXT NOT NULL,
    latest_score REAL NOT NULL,
    PRIMARY KEY (watchlist_id, listing_id)
);
"""

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

_DAEMON_SCHEMA = """
CREATE TABLE IF NOT EXISTS daemon_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'stopped',
    started_at TEXT,
    heartbeat_at TEXT,
    last_cycle_at TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    active_watchlist TEXT,
    pid INTEGER,
    updated_at TEXT NOT NULL
)
"""

_NOTIFICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id TEXT NOT NULL REFERENCES listings(listing_id) ON DELETE CASCADE,
    watchlist_id INTEGER REFERENCES watchlists(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    score REAL NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_created ON notification_events(created_at DESC);

CREATE TABLE IF NOT EXISTS retention_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    deleted_price_points INTEGER NOT NULL DEFAULT 0,
    deleted_search_runs INTEGER NOT NULL DEFAULT 0,
    deleted_notifications INTEGER NOT NULL DEFAULT 0,
    deleted_listings INTEGER NOT NULL DEFAULT 0
);
"""


_CITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS city_ids (
    city_name TEXT PRIMARY KEY,
    city_id TEXT,
    status TEXT NOT NULL DEFAULT 'unverified',
    confidence REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'static',
    previous_city_id TEXT,
    matched_label TEXT,
    conflict_with TEXT,
    verified_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_city_ids_city_id ON city_ids(city_id);

CREATE TABLE IF NOT EXISTS city_discovery_runs (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL DEFAULT 'idle',
    started_at TEXT,
    finished_at TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    changed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    conflicts INTEGER NOT NULL DEFAULT 0,
    current_city TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
)
"""


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _column_names(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(
    db: sqlite3.Connection,
    *,
    table: str,
    column: str,
    definition: str,
) -> None:
    if column not in _column_names(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _apply_migration_1(db: sqlite3.Connection) -> None:
    db.executescript(_BASE_SCHEMA)


def _apply_migration_2(db: sqlite3.Connection) -> None:
    _add_column_if_missing(db, table="watchlists", column="last_success_at", definition="TEXT")
    _add_column_if_missing(db, table="watchlists", column="last_error_at", definition="TEXT")
    _add_column_if_missing(db, table="watchlists", column="last_error", definition="TEXT")
    db.execute(_DAEMON_SCHEMA)
    now = _iso(datetime.now(UTC))
    db.execute(
        "INSERT OR IGNORE INTO daemon_status(id,state,updated_at) VALUES (1,'stopped',?)",
        (now,),
    )


def _apply_migration_3(db: sqlite3.Connection) -> None:
    _add_column_if_missing(db, table="listings", column="description", definition="TEXT")
    _add_column_if_missing(db, table="listings", column="category", definition="TEXT NOT NULL DEFAULT 'other'")
    _add_column_if_missing(db, table="listings", column="condition", definition="TEXT NOT NULL DEFAULT 'unknown'")
    _add_column_if_missing(
        db,
        table="listings",
        column="classification_source",
        definition="TEXT NOT NULL DEFAULT 'heuristic'",
    )
    _add_column_if_missing(
        db,
        table="listings",
        column="classification_confidence",
        definition="REAL NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(db, table="listings", column="restricted", definition="INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(
        db,
        table="listings",
        column="score_reasons",
        definition="TEXT NOT NULL DEFAULT '[]'",
    )
    _add_column_if_missing(db, table="search_runs", column="duration_ms", definition="REAL")
    db.executescript(_NOTIFICATION_SCHEMA)
    db.execute("CREATE INDEX IF NOT EXISTS idx_listings_category ON listings(category)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_listings_condition ON listings(condition)")


def _apply_migration_4(db: sqlite3.Connection) -> None:
    """Israel localization: city_id/radius_km on watchlists, city_id on listings."""
    _add_column_if_missing(db, table="watchlists", column="city_id", definition="TEXT")
    _add_column_if_missing(db, table="watchlists", column="radius_km", definition="REAL NOT NULL DEFAULT 5")
    _add_column_if_missing(db, table="listings", column="city_id", definition="TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_listings_city_id ON listings(city_id)")


<<<<<<< HEAD
def _apply_migration_5(db: sqlite3.Connection) -> None:
    """Add category_id filter (Facebook Marketplace's own category query param) to watchlists."""
    _add_column_if_missing(db, table="watchlists", column="category_id", definition="TEXT")


def _apply_migration_6(db: sqlite3.Connection) -> None:
    """city_id verification: DB-backed resolver + background discovery run tracking.

    Facebook Marketplace city_id values are internal, unofficial identifiers (see
    israel_cities.py) and some in the static map turn out to be wrong or duplicated
    across cities. This table becomes the source of truth going forward; the static
    dict is only the Last-Known-Good seed / fallback when a city was never (re)verified.
    """
    db.executescript(_CITY_SCHEMA)
    now = _iso(datetime.now(UTC))
    db.execute(
        "INSERT OR IGNORE INTO city_discovery_runs(id,state,updated_at) VALUES (1,'idle',?)",
        (now,),
    )
    for name, city_id in ISRAEL_CITY_IDS.items():
        if not city_id:
            continue
        db.execute(
            """INSERT OR IGNORE INTO city_ids(city_name,city_id,status,confidence,source,updated_at)
               VALUES (?,?,?,?,?,?)""",
            (name, city_id, "legacy", 0.4, "static", now),
        )


=======
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _apply_migration_1),
    (2, _apply_migration_2),
    (3, _apply_migration_3),
    (4, _apply_migration_4),
<<<<<<< HEAD
    (5, _apply_migration_5),
    (6, _apply_migration_6),
=======
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
)


class MarketplaceStore:
    """Non-blocking async facade around thread-isolated sqlite3 connections."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect_sync(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def _thread[T](self, fn: Callable[[], T]) -> T:
        return await asyncio.to_thread(fn)

    async def initialize(self) -> None:
        def work() -> None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect_sync() as db:
                db.execute(_MIGRATION_TABLE)
                db.commit()
                current = db.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
                for version, migration in _MIGRATIONS:
                    if version <= current:
                        continue
                    migration(db)
                    db.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, _iso(datetime.now(UTC))),
                    )
                    db.commit()

        await self._thread(work)
        await self._clear_stale_city_discovery_run()

    async def _clear_stale_city_discovery_run(self) -> None:
        """A run left in state='running' means the process died mid-run
        (crash, kill, browser closed) without reaching city_discovery_finish().
        Since this state lives in SQLite, not process memory, a plain restart
        never clears it on its own. Called once at the end of initialize()
        so every startup self-heals instead of getting stuck forever.
        """
        def work() -> None:
            with self._connect_sync() as db:
                db.execute(
                    """UPDATE city_discovery_runs SET state='error',
                       last_error='run interrupted by restart - cleared automatically',
                       finished_at=?, updated_at=?
                       WHERE id=1 AND state='running'""",
                    (_iso(datetime.now(UTC)), _iso(datetime.now(UTC))),
                )
                db.commit()

        await self._thread(work)

    async def schema_version(self) -> int:
        def work() -> int:
            with self._connect_sync() as db:
                row = db.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()
                return int(row[0])

        return await self._thread(work)

    async def start_search_run(self, query: str) -> int:
        def work() -> int:
            with self._connect_sync() as db:
                cursor = db.execute(
                    "INSERT INTO search_runs(query, started_at) VALUES (?, ?)",
                    (query, _iso(datetime.now(UTC))),
                )
                db.commit()
                return int(cursor.lastrowid)

        return await self._thread(work)

    async def finish_search_run(self, run_id: int, **counts: int | float) -> None:
        def work() -> None:
            with self._connect_sync() as db:
                db.execute(
                    """UPDATE search_runs SET finished_at=?, extracted=?, normalized=?, inserted=?,
                       updated=?, price_changes=?, duration_ms=? WHERE id=?""",
                    (
                        _iso(datetime.now(UTC)),
                        counts.get("extracted", 0),
                        counts.get("normalized", 0),
                        counts.get("inserted", 0),
                        counts.get("updated", 0),
                        counts.get("price_changes", 0),
                        counts.get("duration_ms"),
                        run_id,
                    ),
                )
                db.commit()

        await self._thread(work)

    async def upsert_listing(
        self,
        listing: MarketplaceListing,
        *,
        run_id: int,
        city_id: str | None = None,
    ) -> tuple[bool, bool]:
        def work() -> tuple[bool, bool]:
            with self._connect_sync() as db:
                existing = db.execute(
                    "SELECT latest_price_value, latest_price_text FROM listings WHERE listing_id=?",
                    (listing.listing_id,),
                ).fetchone()
                captured = _iso(listing.captured_at)
                image_url = str(listing.image_url) if listing.image_url else None
                url = str(listing.url)
                inserted = existing is None
                common = (
                    listing.title,
                    listing.normalized_title,
                    listing.fingerprint,
                    url,
                    listing.currency,
                    listing.location,
                    image_url,
                    listing.seller_name,
                    listing.description,
                    listing.category,
                    listing.condition,
                    listing.classification_source,
                    listing.classification_confidence,
                    int(listing.restricted),
                    listing.source_query,
                    city_id,
                )

                if inserted:
                    db.execute(
                        """INSERT INTO listings(
                            listing_id,title,normalized_title,fingerprint,url,currency,location,image_url,
                            seller_name,description,category,condition,classification_source,
                            classification_confidence,restricted,source_query,city_id,first_seen,last_seen,
                            latest_price_text,latest_price_value
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (listing.listing_id, *common, captured, captured, listing.price_text, listing.price_value),
                    )
                else:
                    db.execute(
                        """UPDATE listings SET title=?, normalized_title=?, fingerprint=?, url=?, currency=?,
                           location=?, image_url=?, seller_name=?, description=?, category=?, condition=?,
                           classification_source=?, classification_confidence=?, restricted=?, source_query=?,
                           city_id=?, last_seen=?, latest_price_text=?, latest_price_value=? WHERE listing_id=?""",
                        (*common, captured, listing.price_text, listing.price_value, listing.listing_id),
                    )

                old_value = existing["latest_price_value"] if existing else None
                old_text = existing["latest_price_text"] if existing else None
                changed = bool(
                    not inserted
                    and (listing.price_value != old_value or listing.price_text != old_text)
                )
                if (inserted or changed) and (listing.price_value is not None or listing.price_text):
                    db.execute(
                        """INSERT INTO listing_prices(listing_id,price_text,price_value,currency,captured_at)
                           VALUES (?,?,?,?,?)""",
                        (
                            listing.listing_id,
                            listing.price_text,
                            listing.price_value,
                            listing.currency,
                            captured,
                        ),
                    )
                db.execute(
                    "INSERT OR IGNORE INTO search_run_listings(run_id, listing_id) VALUES (?, ?)",
                    (run_id, listing.listing_id),
                )
                db.commit()
                return inserted, changed

        return await self._thread(work)

    async def price_stats(self, listing: MarketplaceListing) -> PriceStats:
        profile = valuation_profile(listing.category)

        def work() -> PriceStats:
            with self._connect_sync() as db:
                anchor = comparable_anchor(listing.title)
                params: list[object] = [listing.listing_id, listing.currency]
                sql = """SELECT title,latest_price_value,category FROM listings
                         WHERE listing_id<>? AND currency IS ? AND latest_price_value IS NOT NULL
                         AND restricted=0"""
                if listing.category != "other":
                    sql += " AND category=?"
                    params.append(listing.category)
                if anchor:
                    sql += " AND normalized_title LIKE ?"
                    params.append(f"%{anchor}%")
                sql += " ORDER BY last_seen DESC LIMIT 750"
                rows = db.execute(sql, params).fetchall()
                prices = [
                    float(row["latest_price_value"])
                    for row in rows
                    if title_similarity(listing.title, str(row["title"]))
                    >= profile.comparable_threshold
                ]
                history = db.execute(
                    """SELECT price_value FROM listing_prices WHERE listing_id=?
                       AND price_value IS NOT NULL ORDER BY captured_at DESC, id DESC LIMIT 2""",
                    (listing.listing_id,),
                ).fetchall()
                previous_price = float(history[1][0]) if len(history) > 1 else None
                return PriceStats(
                    sample_size=len(prices),
                    median_price=float(median(prices)) if prices else None,
                    min_price=min(prices) if prices else None,
                    max_price=max(prices) if prices else None,
                    previous_price=previous_price,
                    category=listing.category,
                    similarity_threshold=profile.comparable_threshold,
                    sample_target=profile.sample_target,
                )

        return await self._thread(work)

    async def update_score(
        self,
        listing_id: str,
        score: float,
        confidence: float,
        reasons: list[str] | None = None,
    ) -> None:
        def work() -> None:
            with self._connect_sync() as db:
                db.execute(
                    "UPDATE listings SET deal_score=?, score_confidence=?, score_reasons=? WHERE listing_id=?",
                    (score, confidence, json.dumps(reasons or []), listing_id),
                )
                db.commit()

        await self._thread(work)

    async def create_watchlist(self, watchlist: Watchlist) -> int:
        def work() -> int:
            now = _iso(datetime.now(UTC))
            with self._connect_sync() as db:
                cursor = db.execute(
<<<<<<< HEAD
                    """INSERT INTO watchlists(name,query,city_id,radius_km,category_id,min_price,max_price,
                       target_price,max_items,default_currency,interval_seconds,enabled,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
=======
                    """INSERT INTO watchlists(name,query,city_id,radius_km,min_price,max_price,target_price,
                       max_items,default_currency,interval_seconds,enabled,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
                    (
                        watchlist.name,
                        watchlist.query,
                        watchlist.city_id,
                        watchlist.radius_km,
<<<<<<< HEAD
                        watchlist.category_id,
=======
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
                        watchlist.min_price,
                        watchlist.max_price,
                        watchlist.target_price,
                        watchlist.max_items,
                        watchlist.default_currency,
                        watchlist.interval_seconds,
                        int(watchlist.enabled),
                        now,
                        now,
                    ),
                )
                db.commit()
                return int(cursor.lastrowid)

        return await self._thread(work)

    async def get_watchlist(self, watchlist_id: int) -> Watchlist | None:
        def work() -> Watchlist | None:
            with self._connect_sync() as db:
                row = db.execute("SELECT * FROM watchlists WHERE id=?", (watchlist_id,)).fetchone()
                return self._watchlist_from_row(row) if row else None

        return await self._thread(work)

    def _watchlist_from_row(self, row: sqlite3.Row) -> Watchlist:
        return Watchlist(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            city_id=row["city_id"],
            radius_km=row["radius_km"],
<<<<<<< HEAD
            category_id=row["category_id"],
=======
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
            min_price=row["min_price"],
            max_price=row["max_price"],
            target_price=row["target_price"],
            max_items=row["max_items"],
            default_currency=row["default_currency"],
            interval_seconds=row["interval_seconds"],
            enabled=bool(row["enabled"]),
            last_run_at=_dt(row["last_run_at"]),
            last_success_at=_dt(row["last_success_at"]),
            last_error_at=_dt(row["last_error_at"]),
            last_error=row["last_error"],
            created_at=_dt(row["created_at"]) or datetime.now(UTC),
        )

    async def list_watchlists(self, *, enabled_only: bool = False) -> list[Watchlist]:
        def work() -> list[Watchlist]:
            with self._connect_sync() as db:
                sql = "SELECT * FROM watchlists"
                if enabled_only:
                    sql += " WHERE enabled=1"
                sql += " ORDER BY id"
                return [self._watchlist_from_row(row) for row in db.execute(sql).fetchall()]

        return await self._thread(work)

    async def update_watchlist(
        self,
        watchlist_id: int,
        updates: dict[str, object],
    ) -> Watchlist | None:
        allowed = {
<<<<<<< HEAD
            "name", "query", "city_id", "radius_km", "category_id", "min_price", "max_price",
            "target_price", "max_items", "default_currency", "interval_seconds", "enabled",
=======
            "name", "query", "city_id", "radius_km", "min_price", "max_price", "target_price",
            "max_items", "default_currency", "interval_seconds", "enabled",
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
        }
        invalid = set(updates) - allowed
        if invalid:
            raise ValueError(f"unsupported watchlist fields: {', '.join(sorted(invalid))}")
        if not updates:
            return await self.get_watchlist(watchlist_id)

        def work() -> Watchlist | None:
            values = dict(updates)
            if "enabled" in values:
                values["enabled"] = int(bool(values["enabled"]))
            values["updated_at"] = _iso(datetime.now(UTC))
            assignments = ", ".join(f"{key}=?" for key in values)
            params = [*values.values(), watchlist_id]
            with self._connect_sync() as db:
                cursor = db.execute(f"UPDATE watchlists SET {assignments} WHERE id=?", params)
                db.commit()
                if cursor.rowcount == 0:
                    return None
                row = db.execute("SELECT * FROM watchlists WHERE id=?", (watchlist_id,)).fetchone()
                return self._watchlist_from_row(row) if row else None

        return await self._thread(work)

    async def delete_watchlist(self, watchlist_id: int) -> bool:
        def work() -> bool:
            with self._connect_sync() as db:
                cursor = db.execute("DELETE FROM watchlists WHERE id=?", (watchlist_id,))
                db.commit()
                return cursor.rowcount > 0

        return await self._thread(work)

    async def mark_watchlist_run(
        self,
        watchlist_id: int,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        def work() -> None:
            now = _iso(datetime.now(UTC))
            with self._connect_sync() as db:
                if success:
                    db.execute(
                        """UPDATE watchlists SET last_run_at=?, last_success_at=?, last_error=NULL,
                           updated_at=? WHERE id=?""",
                        (now, now, now, watchlist_id),
                    )
                else:
                    db.execute(
                        """UPDATE watchlists SET last_run_at=?, last_error_at=?, last_error=?,
                           updated_at=? WHERE id=?""",
                        (now, now, (error or "Unknown error")[:2000], now, watchlist_id),
                    )
                db.commit()

        await self._thread(work)

    async def record_watchlist_match(self, watchlist_id: int, listing_id: str, score: float) -> None:
        def work() -> None:
            now = _iso(datetime.now(UTC))
            with self._connect_sync() as db:
                db.execute(
                    """INSERT INTO watchlist_matches(watchlist_id,listing_id,first_matched,last_matched,latest_score)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(watchlist_id,listing_id) DO UPDATE SET
                         last_matched=excluded.last_matched, latest_score=excluded.latest_score""",
                    (watchlist_id, listing_id, now, now, score),
                )
                db.commit()

        await self._thread(work)

    async def due_watchlists(self) -> list[Watchlist]:
        now = datetime.now(UTC)
        watchlists = await self.list_watchlists(enabled_only=True)
        return [
            item
            for item in watchlists
            if item.last_run_at is None
            or item.last_run_at + timedelta(seconds=item.interval_seconds) <= now
        ]

    async def notification_exists(self, dedupe_key: str) -> bool:
        def work() -> bool:
            with self._connect_sync() as db:
                return db.execute(
                    "SELECT 1 FROM notification_events WHERE dedupe_key=?",
                    (dedupe_key,),
                ).fetchone() is not None

        return await self._thread(work)

    async def record_notification(self, event: NotificationEvent) -> bool:
        def work() -> bool:
            with self._connect_sync() as db:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO notification_events(
                       listing_id,watchlist_id,event_type,dedupe_key,score,payload_json,created_at
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        event.listing_id,
                        event.watchlist_id,
                        event.event_type,
                        event.dedupe_key,
                        event.score,
                        json.dumps(event.payload, separators=(",", ":")),
                        _iso(event.created_at),
                    ),
                )
                db.commit()
                return cursor.rowcount > 0

        return await self._thread(work)

    async def recent_notifications(self, *, limit: int = 50) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            with self._connect_sync() as db:
                rows = db.execute(
                    """SELECT id,listing_id,watchlist_id,event_type,score,payload_json,created_at
                       FROM notification_events ORDER BY created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                output = []
                for row in rows:
                    item = dict(row)
                    item["payload"] = json.loads(item.pop("payload_json"))
                    output.append(item)
                return output

        return await self._thread(work)

    async def daemon_started(self, pid: int) -> None:
        now = _iso(datetime.now(UTC))
        await self._write_daemon_status(
            state="running", pid=pid, started_at=now, heartbeat_at=now,
            active_watchlist=None, clear_error=True,
        )

    async def daemon_heartbeat(self, *, active_watchlist: str | None = None) -> None:
        await self._write_daemon_status(
            heartbeat_at=_iso(datetime.now(UTC)), active_watchlist=active_watchlist
        )

    async def daemon_cycle_completed(self, *, success: bool, error: str | None = None) -> None:
        now = _iso(datetime.now(UTC))
        updates: dict[str, object | None] = {
            "state": "running" if success else "error",
            "heartbeat_at": now,
            "last_cycle_at": now,
            "active_watchlist": None,
        }
        if success:
            updates["last_success_at"] = now
            updates["last_error"] = None
        else:
            updates["last_error_at"] = now
            updates["last_error"] = (error or "Unknown error")[:2000]
        await self._write_daemon_status(**updates)

    async def daemon_stopped(self, *, error: str | None = None) -> None:
        now = _iso(datetime.now(UTC))
        updates: dict[str, object | None] = {
            "state": "error" if error else "stopped",
            "heartbeat_at": now,
            "active_watchlist": None,
        }
        if error:
            updates["last_error_at"] = now
            updates["last_error"] = error[:2000]
        await self._write_daemon_status(**updates)

    async def _write_daemon_status(self, **updates: object | None) -> None:
        allowed = {
            "state", "started_at", "heartbeat_at", "last_cycle_at", "last_success_at",
            "last_error_at", "last_error", "active_watchlist", "pid", "clear_error",
        }
        invalid = set(updates) - allowed
        if invalid:
            raise ValueError(f"unsupported daemon status fields: {', '.join(sorted(invalid))}")

        def work() -> None:
            values = dict(updates)
            clear_error = bool(values.pop("clear_error", False))
            if clear_error:
                values["last_error_at"] = None
                values["last_error"] = None
            values["updated_at"] = _iso(datetime.now(UTC))
            assignments = ", ".join(f"{key}=?" for key in values)
            params = [*values.values(), 1]
            with self._connect_sync() as db:
                db.execute(f"UPDATE daemon_status SET {assignments} WHERE id=?", params)
                db.commit()

        await self._thread(work)

    async def daemon_status(self, *, stale_after_seconds: int = 300) -> dict[str, object]:
        def work() -> dict[str, object]:
            with self._connect_sync() as db:
                row = db.execute("SELECT * FROM daemon_status WHERE id=1").fetchone()
                if row is None:
                    return {"state": "unknown", "effective_state": "unknown", "stale": False}
                data = dict(row)
                heartbeat = _dt(data.get("heartbeat_at"))
                age = None
                stale = False
                if heartbeat is not None:
                    age = max(0.0, (datetime.now(UTC) - heartbeat).total_seconds())
                    stale = data.get("state") == "running" and age > stale_after_seconds
                data["heartbeat_age_seconds"] = round(age, 1) if age is not None else None
                data["stale"] = stale
                data["effective_state"] = "stale" if stale else data.get("state", "unknown")
                return data

        return await self._thread(work)

    @staticmethod
    def _listing_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        reasons = item.get("score_reasons")
        if isinstance(reasons, str):
            try:
                item["score_reasons"] = json.loads(reasons)
            except json.JSONDecodeError:
                item["score_reasons"] = []
        item["restricted"] = bool(item.get("restricted", 0))
        return item

    async def recent_listings(self, *, limit: int = 100) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            with self._connect_sync() as db:
                rows = db.execute(
                    """SELECT listing_id,title,url,latest_price_text,latest_price_value,currency,location,
                       image_url,first_seen,last_seen,deal_score,score_confidence,source_query,category,
                       condition,classification_source,classification_confidence,restricted,score_reasons,
<<<<<<< HEAD
                       city_id,description
=======
                       city_id
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
                       FROM listings WHERE restricted=0
                       ORDER BY deal_score DESC, last_seen DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                return [self._listing_dict(row) for row in rows]

        return await self._thread(work)

    async def delete_listing(self, listing_id: str) -> bool:
        def work() -> bool:
            with self._connect_sync() as db:
                cursor = db.execute("DELETE FROM listings WHERE listing_id=?", (listing_id,))
                db.commit()
                return cursor.rowcount > 0

        return await self._thread(work)

    async def delete_all_listings(self) -> int:
        def work() -> int:
            with self._connect_sync() as db:
                cursor = db.execute("DELETE FROM listings")
                db.commit()
                return max(0, cursor.rowcount)

        return await self._thread(work)

    async def listing_detail(self, listing_id: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            with self._connect_sync() as db:
                row = db.execute(
                    """SELECT * FROM listings WHERE listing_id=? AND restricted=0""",
                    (listing_id,),
                ).fetchone()
                return self._listing_dict(row) if row else None

        return await self._thread(work)

    async def listing_history(self, listing_id: str) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            with self._connect_sync() as db:
                rows = db.execute(
                    """SELECT price_text,price_value,currency,captured_at FROM listing_prices
                       WHERE listing_id=? ORDER BY captured_at DESC, id DESC""",
                    (listing_id,),
                ).fetchall()
                return [dict(row) for row in rows]

        return await self._thread(work)

    async def search_run_metrics(self, *, limit: int = 30) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            with self._connect_sync() as db:
                rows = db.execute(
                    """SELECT id,query,started_at,finished_at,extracted,normalized,inserted,updated,
                       price_changes,duration_ms FROM search_runs ORDER BY id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]

        return await self._thread(work)

    async def prune(self, policy: RetentionPolicy) -> dict[str, int]:
        def work() -> dict[str, int]:
            started = datetime.now(UTC)
            price_cutoff = _iso(started - timedelta(days=policy.price_history_days))
            run_cutoff = _iso(started - timedelta(days=policy.search_run_days))
            notification_cutoff = _iso(started - timedelta(days=policy.notification_days))
            listing_cutoff = _iso(started - timedelta(days=policy.listing_days))
            with self._connect_sync() as db:
                price_cursor = db.execute(
                    """DELETE FROM listing_prices WHERE captured_at < ? AND id NOT IN (
                       SELECT MAX(id) FROM listing_prices GROUP BY listing_id)""",
                    (price_cutoff,),
                )
                run_cursor = db.execute(
                    "DELETE FROM search_runs WHERE finished_at IS NOT NULL AND finished_at < ?",
                    (run_cutoff,),
                )
                notification_cursor = db.execute(
                    "DELETE FROM notification_events WHERE created_at < ?",
                    (notification_cutoff,),
                )
                listing_cursor = db.execute(
                    """DELETE FROM listings WHERE last_seen < ? AND listing_id NOT IN (
                       SELECT listing_id FROM watchlist_matches)""",
                    (listing_cutoff,),
                )
                result = {
                    "price_points": max(0, price_cursor.rowcount),
                    "search_runs": max(0, run_cursor.rowcount),
                    "notifications": max(0, notification_cursor.rowcount),
                    "listings": max(0, listing_cursor.rowcount),
                }
                finished = datetime.now(UTC)
                db.execute(
                    """INSERT INTO retention_runs(started_at,finished_at,deleted_price_points,
                       deleted_search_runs,deleted_notifications,deleted_listings) VALUES (?,?,?,?,?,?)""",
                    (
                        _iso(started), _iso(finished), result["price_points"], result["search_runs"],
                        result["notifications"], result["listings"],
                    ),
                )
                db.commit()
                return result

        return await self._thread(work)

    async def get_city_id_row(self, city_name: str) -> dict[str, Any] | None:
        def work() -> dict[str, Any] | None:
            with self._connect_sync() as db:
                row = db.execute("SELECT * FROM city_ids WHERE city_name=?", (city_name,)).fetchone()
                return dict(row) if row else None

        return await self._thread(work)

    async def list_city_ids(self) -> list[dict[str, Any]]:
        def work() -> list[dict[str, Any]]:
            with self._connect_sync() as db:
                rows = db.execute("SELECT * FROM city_ids ORDER BY city_name").fetchall()
                return [dict(row) for row in rows]

        return await self._thread(work)

    async def upsert_city_id(
        self,
        *,
        city_name: str,
        city_id: str | None,
        status: str,
        confidence: float,
        source: str,
        matched_label: str | None = None,
        conflict_with: str | None = None,
    ) -> dict[str, object]:
        def work() -> dict[str, object]:
            now = _iso(datetime.now(UTC))
            with self._connect_sync() as db:
                existing = db.execute(
                    "SELECT city_id FROM city_ids WHERE city_name=?", (city_name,)
                ).fetchone()
                previous = existing["city_id"] if existing else None
                verified_at = now if status == "verified" else None
                db.execute(
                    """INSERT INTO city_ids(
                        city_name,city_id,status,confidence,source,previous_city_id,
                        matched_label,conflict_with,verified_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(city_name) DO UPDATE SET
                         city_id=excluded.city_id, status=excluded.status, confidence=excluded.confidence,
                         source=excluded.source, previous_city_id=excluded.previous_city_id,
                         matched_label=excluded.matched_label, conflict_with=excluded.conflict_with,
                         verified_at=excluded.verified_at, updated_at=excluded.updated_at""",
                    (
                        city_name, city_id, status, confidence, source, previous,
                        matched_label, conflict_with, verified_at, now,
                    ),
                )
                db.commit()
                return {
                    "changed": bool(previous and city_id and previous != city_id),
                    "previous_city_id": previous,
                }

        return await self._thread(work)

    async def set_city_conflict(self, city_name: str, conflict_with: str | None) -> None:
        def work() -> None:
            with self._connect_sync() as db:
                db.execute(
                    "UPDATE city_ids SET conflict_with=?, updated_at=? WHERE city_name=?",
                    (conflict_with, _iso(datetime.now(UTC)), city_name),
                )
                db.commit()

        await self._thread(work)

    async def city_discovery_start(self, *, total: int) -> None:
        def work() -> None:
            now = _iso(datetime.now(UTC))
            with self._connect_sync() as db:
                db.execute(
                    """UPDATE city_discovery_runs SET state='running', started_at=?, finished_at=NULL,
                       total=?, processed=0, verified=0, changed=0, failed=0, conflicts=0,
                       current_city=NULL, last_error=NULL, updated_at=? WHERE id=1""",
                    (now, total, now),
                )
                db.commit()

        await self._thread(work)

    async def city_discovery_progress(self, **updates: object | None) -> None:
        allowed = {"processed", "verified", "changed", "failed", "conflicts", "current_city", "last_error"}
        invalid = set(updates) - allowed
        if invalid:
            raise ValueError(f"unsupported city discovery fields: {', '.join(sorted(invalid))}")
        if not updates:
            return

        def work() -> None:
            values = dict(updates)
            values["updated_at"] = _iso(datetime.now(UTC))
            assignments = ", ".join(f"{key}=?" for key in values)
            params = [*values.values()]
            with self._connect_sync() as db:
                db.execute(f"UPDATE city_discovery_runs SET {assignments} WHERE id=1", params)
                db.commit()

        await self._thread(work)

    async def city_discovery_finish(self, *, success: bool, error: str | None = None) -> None:
        def work() -> None:
            now = _iso(datetime.now(UTC))
            with self._connect_sync() as db:
                db.execute(
                    """UPDATE city_discovery_runs SET state=?, finished_at=?, last_error=?, updated_at=?
                       WHERE id=1""",
                    ("done" if success else "error", now, error, now),
                )
                db.commit()

        await self._thread(work)

    async def city_discovery_status(self) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            with self._connect_sync() as db:
                row = db.execute("SELECT * FROM city_discovery_runs WHERE id=1").fetchone()
                return dict(row) if row else {"state": "idle"}

        return await self._thread(work)

    async def dashboard_stats(self) -> dict[str, object]:
        def work() -> dict[str, object]:
            with self._connect_sync() as db:
                listings = db.execute("SELECT COUNT(*) FROM listings WHERE restricted=0").fetchone()
                watchlists = db.execute("SELECT COUNT(*) FROM watchlists WHERE enabled=1").fetchone()
                runs = db.execute("SELECT COUNT(*) FROM search_runs").fetchone()
                changes = db.execute("SELECT COUNT(*) FROM listing_prices").fetchone()
                best = db.execute("SELECT MAX(deal_score) FROM listings WHERE restricted=0").fetchone()
                notifications = db.execute("SELECT COUNT(*) FROM notification_events").fetchone()
                version = db.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()
                return {
                    "listings": int(listings[0]),
                    "active_watchlists": int(watchlists[0]),
                    "search_runs": int(runs[0]),
                    "price_points": int(changes[0]),
                    "notifications": int(notifications[0]),
                    "best_deal_score": float(best[0] or 0),
                    "schema_version": int(version[0]),
                }

        return await self._thread(work)
