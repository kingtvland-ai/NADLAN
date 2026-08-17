"""
PlanWatch - Database layer
===========================
SQLite for the prototype (swap for Postgres/PostGIS in production - the schema
below uses only standard SQL so the move is mostly a connection-string change,
and the bbox columns map cleanly onto a real spatial index).

Tables
------
plans            - one row per planning entity (תוכנית) known to the system,
                   sourced from the public ArcGIS "Blue Lines" service.
plan_status_log  - append-only history of station/status changes per plan.
                   A new row here is what triggers a client notification.
subscriptions    - a client's watched גוש/חלקה (or MP_ID / area), i.e. "tell me
                   about anything touching this parcel".
notifications    - outbox of alerts to deliver to a client dashboard/email.
sync_runs        - one row per sync job: how much was pulled, how long it took,
                   and whether it failed. Needed to drive incremental syncs and
                   to notice silent breakage.
parcel_coords    - cache of resolved גוש/חלקה -> lat/lon, so a licensed
                   cadastral source is queried once per parcel.

Schema evolution
----------------
DDL is deliberately split into `TABLES` and `INDEXES` and applied in three
steps by `get_conn()`: create tables, add any missing columns, *then* create
indexes. Doing it in one script breaks upgrades - `CREATE INDEX ... (min_lon)`
runs against the old table before `_migrate()` has added `min_lon`, and the
whole connection fails with "no such column: min_lon".

Performance notes
-----------------
* Every plan row carries a precomputed bounding box (min_lon/min_lat/
  max_lon/max_lat). Point-in-polygon against ~36,700 polygons is far too slow
  to do naively per subscription; the bbox index reduces each lookup to a
  handful of candidate polygons before shapely runs an exact test.
* Writes are batched. Committing once per plan means ~36,700 fsyncs and turns
  a 1-minute sync into a many-minute one. Use `plans_writer()` for bulk loads.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

#: Override with PLANWATCH_DB to point at another location.
DB_PATH = Path(
    os.environ.get("PLANWATCH_DB", Path(__file__).parent / "data" / "planwatch.sqlite3")
)

TABLES = """
CREATE TABLE IF NOT EXISTS plans (
    object_id            INTEGER PRIMARY KEY,  -- OBJECTID from ArcGIS service
    mp_id                TEXT,                 -- id for Mavat deep-link (SV4/1/<mp_id>/310)
    pl_id                TEXT,
    pl_number            TEXT,
    pl_name              TEXT,
    pl_url               TEXT,
    county_name          TEXT,                 -- מחוז
    district_name        TEXT,
    jurisdiction_name    TEXT,                 -- רשות מקומית
    plan_area_name       TEXT,
    entity_subtype       TEXT,
    station              TEXT,                 -- current station in the approval process
    short_status         TEXT,                 -- internet_short_status
    area_dunam           REAL,
    landuse              TEXT,
    objectives           TEXT,
    housing_units        INTEGER,              -- יח"ד (bucket 120 only - see client)
    hotel_sqm            REAL,                 -- authorised hotel rooms, m^2 (105)
    special_housing_sqm  REAL,                 -- authorised special housing, m^2 (110)
    geometry_json        TEXT,                 -- GeoJSON geometry (WGS84) as text
    min_lon              REAL,                 -- bbox, for fast candidate lookup
    min_lat              REAL,
    max_lon              REAL,
    max_lat              REAL,
    last_update          TEXT,                 -- last_update_date, ISO-8601 UTC
    depositing_date      TEXT,
    open_date            TEXT,
    pl_last_deposit_date TEXT,
    pl_date_advertise    TEXT,
    pl_rejection_date    TEXT,
    first_seen_at        TEXT NOT NULL,
    last_synced_at       TEXT NOT NULL,
    /*
     * Whitespace-stripped pl_number, maintained by SQLite itself.
     *
     * The feeds disagree on spacing: `plans` has "תמל/ 1064/ א", data.gov.il
     * publishes "תמל/1064/א". Joining on pl_number silently dropped every
     * slash-form plan - 56 urban-renewal rows and 47 RAMI inventory rows.
     * A GENERATED column stays correct on every write with no importer change,
     * and unlike a SQL function it can be indexed.
     */
    pl_number_sq         TEXT GENERATED ALWAYS AS
                           (replace(replace(replace(pl_number,' ',''),
                            char(9),''), char(160),'')) VIRTUAL
);

CREATE TABLE IF NOT EXISTS plan_status_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id       INTEGER NOT NULL REFERENCES plans(object_id),
    old_station     TEXT,
    new_station     TEXT,
    detected_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL,         -- your SaaS customer's id
    gush            TEXT,
    helka           TEXT,
    label           TEXT,                  -- free-text project name for the client's UI
    lat             REAL,
    lon             REAL,
    radius_m        REAL,                  -- optional: also alert on nearby plans
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription_plan_matches (
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    object_id       INTEGER NOT NULL REFERENCES plans(object_id),
    matched_at      TEXT NOT NULL,
    match_method    TEXT,                  -- 'point_in_polygon' | 'radius' | 'manual'
    PRIMARY KEY (subscription_id, object_id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
    /*
     * NULLABLE on purpose. It was NOT NULL with an FK to plans, which was
     * right when a plan status change was the only alert there was. The
     * watchers in alerts.py raise on dangerous-building orders, permits,
     * tenders and appraisals - none of which is plan-scoped - and a 0
     * sentinel is rejected by the FK. NULL means "not about a specific plan".
     */
    object_id       INTEGER REFERENCES plans(object_id),
    kind            TEXT NOT NULL DEFAULT 'status_change',
                    -- status_change | new_plan | dangerous | permits
                    -- | renewal | tenders | appraisals
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    delivered       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    mode            TEXT,                  -- 'full' | 'incremental'
    plans_seen      INTEGER NOT NULL DEFAULT 0,
    plans_new       INTEGER NOT NULL DEFAULT 0,
    status_changes  INTEGER NOT NULL DEFAULT 0,
    notifications   INTEGER NOT NULL DEFAULT 0,
    ok              INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS parcel_coords (
    gush        TEXT NOT NULL,
    helka       TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    source      TEXT,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (gush, helka)
);

CREATE TABLE IF NOT EXISTS property_catalog (
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    listing_year    INTEGER NOT NULL,
    listing_url     TEXT NOT NULL,
    source_lastmod  TEXT,
    image_count     INTEGER NOT NULL DEFAULT 0,
    images_json     TEXT,
    imported_at     TEXT NOT NULL,
    PRIMARY KEY (source, external_id, listing_year)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,   -- scrypt hash, hex
    password_salt   TEXT NOT NULL,   -- hex
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT PRIMARY KEY,   -- secrets.token_urlsafe(32)
    user_id         INTEGER NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL
);

-- Small operator-editable key/value store - deliberately only for
-- *non-secret* integration values (e.g. the WhatsApp bot's phone number,
-- the Telegram bot's @username - needed to build wa.me/t.me share links,
-- not credentials). Real secrets (PLANWATCH_TELEGRAM_TOKEN, SMTP password,
-- PLANWATCH_BASIC_AUTH) stay environment-only, same as every other
-- credential in this project - see .gitignore's "Never publish: live
-- credentials" section and credentialed_client.py's status()-not-storage
-- posture. Mixing the two here would quietly weaken that guarantee.
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facebook_listings (
    listing_id      TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    url             TEXT NOT NULL,
    price_text      TEXT,
    price_value     REAL,
    currency        TEXT,
    location        TEXT,
    image_url       TEXT,
    seller_name     TEXT,
    description     TEXT,
    category        TEXT DEFAULT 'other',
    condition       TEXT DEFAULT 'unknown',
    city_id         TEXT,
    city_name       TEXT,
    source_query    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    delisted_at     TEXT,
    raw_json        TEXT
);

CREATE TABLE IF NOT EXISTS facebook_price_history (
    listing_id      TEXT NOT NULL REFERENCES facebook_listings(listing_id) ON DELETE CASCADE,
    price_text      TEXT,
    price_value     REAL,
    currency        TEXT,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (listing_id, observed_at)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS facebook_harvest_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,
    pages_fetched   INTEGER NOT NULL DEFAULT 0,
    pages_failed    INTEGER NOT NULL DEFAULT 0,
    rows_seen       INTEGER NOT NULL DEFAULT 0,
    rows_new        INTEGER NOT NULL DEFAULT 0,
    target          INTEGER,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS facebook_cities (
    city_name       TEXT PRIMARY KEY,
    city_id         TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

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
    source TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (canonical_id) REFERENCES normalized_listings(canonical_id)
);

CREATE TABLE IF NOT EXISTS source_health (
    source_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_runs INTEGER NOT NULL DEFAULT 0,
    successful_runs INTEGER NOT NULL DEFAULT 0,
    failed_runs INTEGER NOT NULL DEFAULT 0,
    avg_duration_seconds REAL NOT NULL DEFAULT 0,
    last_run_at TEXT,
    metadata TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_versions (
    version TEXT PRIMARY KEY,
    app_version TEXT NOT NULL,
    description TEXT,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    snapshot_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    file_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    record_count INTEGER NOT NULL DEFAULT 0,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS backup_history (
    backup_id TEXT PRIMARY KEY,
    backup_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    file_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    database_path TEXT NOT NULL,
    tables TEXT,
    metadata TEXT,
    restored_at TEXT,
    is_restored INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    document_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    document_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    duration_ms REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

#: Applied only after _migrate(), so indexed columns are guaranteed to exist.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_plans_bbox
    ON plans (min_lon, max_lon, min_lat, max_lat);
CREATE INDEX IF NOT EXISTS idx_plans_number       ON plans (pl_number);
/*
 * Whitespace-normalised plan number, as a real indexed column.
 *
 * `plans.pl_number` holds "תמל/ 1064/ א" while the data.gov.il feeds publish
 * "תמל/1064/א", so an equality join silently drops every slash-form plan.
 * A normalising SQL function would fix correctness but defeat the index on a
 * 36,739-row table joined repeatedly; a generated column keeps both.
 */
CREATE INDEX IF NOT EXISTS idx_plans_number_sq    ON plans (pl_number_sq);
CREATE INDEX IF NOT EXISTS idx_plans_mp_id        ON plans (mp_id);
CREATE INDEX IF NOT EXISTS idx_plans_last_update  ON plans (last_update);
/*
 * Grouping columns, each carrying housing_units so the index can answer alone.
 *
 * `plans` is only ~37k rows but ~1.1GB on disk: geometry_json holds 184MB of
 * polygons, so touching the table at all is expensive. Every aggregate here
 * used to be a full table scan - /api/bootstrap ran four of them and needed
 * >60s cold, which is exactly how long the dashboard sat on a spinner.
 *
 * The trailing housing_units is what makes them *covering*. /api/breakdown
 * groups by one of these columns and sums housing_units in the same query; a
 * bare single-column index finds the groups but still fetches every row from
 * the table to read the sum, which measured 37s for the status breakdown.
 * With the sum in the index the query never touches the table.
 *
 * These replace earlier single-column indexes of the same purpose; the DROPs
 * retire those names once and are no-ops thereafter.
 */
DROP INDEX IF EXISTS idx_plans_jurisdiction;
DROP INDEX IF EXISTS idx_plans_county;
DROP INDEX IF EXISTS idx_plans_status;
DROP INDEX IF EXISTS idx_plans_subtype;
CREATE INDEX IF NOT EXISTS idx_plans_jurisdiction_units
    ON plans (jurisdiction_name, housing_units);
CREATE INDEX IF NOT EXISTS idx_plans_county_units
    ON plans (county_name, housing_units);
CREATE INDEX IF NOT EXISTS idx_plans_status_units
    ON plans (short_status, housing_units);
CREATE INDEX IF NOT EXISTS idx_plans_subtype_units
    ON plans (entity_subtype, housing_units);
CREATE INDEX IF NOT EXISTS idx_plans_station_units
    ON plans (station, housing_units);
/* Partial index so the with_geometry count is an index scan, not a blob scan. */
CREATE INDEX IF NOT EXISTS idx_plans_has_geometry
    ON plans (object_id) WHERE geometry_json IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_status_log_object  ON plan_status_log (object_id);
CREATE INDEX IF NOT EXISTS idx_subs_client        ON subscriptions (client_id);
CREATE INDEX IF NOT EXISTS idx_subs_gushhelka     ON subscriptions (gush, helka);
CREATE INDEX IF NOT EXISTS idx_notif_undelivered  ON notifications (delivered, created_at);
CREATE INDEX IF NOT EXISTS idx_property_catalog_year
    ON property_catalog (listing_year, source_lastmod);
CREATE INDEX IF NOT EXISTS idx_property_catalog_external
    ON property_catalog (source, external_id);
CREATE INDEX IF NOT EXISTS idx_facebook_city
    ON facebook_listings (city_name);
CREATE INDEX IF NOT EXISTS idx_facebook_price
    ON facebook_listings (price_value);
CREATE INDEX IF NOT EXISTS idx_facebook_last_seen
    ON facebook_listings (last_seen_at);
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
CREATE INDEX IF NOT EXISTS idx_source_health_updated
    ON source_health (updated_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_source
    ON snapshots (source_name, created_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_type
    ON snapshots (snapshot_type, created_at);
CREATE INDEX IF NOT EXISTS idx_backup_history_created
    ON backup_history (created_at);
CREATE INDEX IF NOT EXISTS idx_sync_queue_next_attempt ON sync_queue (next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_sync_history_collection ON sync_history (collection, created_at);
"""

#: Columns written by the bulk upsert, in order.
PLAN_COLUMNS = (
    "object_id", "mp_id", "pl_id", "pl_number", "pl_name", "pl_url",
    "county_name", "district_name", "jurisdiction_name", "plan_area_name",
    "entity_subtype", "station", "short_status", "area_dunam", "landuse",
    "objectives", "housing_units", "hotel_sqm", "special_housing_sqm",
    "geometry_json",
    "min_lon", "min_lat", "max_lon", "max_lat",
    "last_update", "depositing_date", "open_date",
    "pl_last_deposit_date", "pl_date_advertise", "pl_rejection_date",
)

#: Never overwrite a stored value with NULL. A refresh that skipped geometry
#: (include_geometry=False) must not wipe polygons we already have - that would
#: silently disable all point-in-polygon matching for those plans.
PRESERVE_IF_NULL = frozenset(
    {"geometry_json", "min_lon", "min_lat", "max_lon", "max_lat"}
)


def _update_assignment(col: str) -> str:
    if col in PRESERVE_IF_NULL:
        return f"{col}=COALESCE(excluded.{col}, plans.{col})"
    return f"{col}=excluded.{col}"


_PLAN_SQL = f"""
INSERT INTO plans ({", ".join(PLAN_COLUMNS)}, first_seen_at, last_synced_at)
VALUES ({", ".join("?" for _ in PLAN_COLUMNS)}, ?, ?)
ON CONFLICT(object_id) DO UPDATE SET
    {", ".join(_update_assignment(c) for c in PLAN_COLUMNS if c != "object_id")},
    last_synced_at=excluded.last_synced_at
"""


def squash_plan_number(value) -> str | None:
    """
    Normalise a plan number for joining across feeds.

    `plans.pl_number` stores slash-form numbers WITH spaces around the slashes
    ("תמל/ 1064/ א"), while data.gov.il feeds publish them without
    ("תמל/1064/א"). An equality join therefore silently misses every one of
    them. Measured: normalising recovers +56 urban-renewal rows (698 -> 754)
    and +47 RAMI inventory rows (162 -> 209).

    Register this on a connection with `install_sql_functions()` to use it from
    SQL as `sqpn()`. Note it defeats the index on `pl_number`, so prefer
    joining on `mp_id` where available and use this only for feeds that carry a
    plan NUMBER and nothing else.
    """
    if value is None:
        return None
    return "".join(str(value).replace("\xa0", " ").split()) or None


def install_sql_functions(conn) -> None:
    """Expose the Python normalisers to SQL: sqpn()."""
    conn.create_function("sqpn", 1, squash_plan_number, deterministic=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_in(seconds: float = 0) -> str:
    """A UTC ISO timestamp `seconds` from now - for scheduling next-run times."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def get_conn(path: Path | str | None = None) -> sqlite3.Connection:
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL + relaxed sync: this is a cache of a public dataset, so trading a
    # little crash durability for a large bulk-load speedup is the right call.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Order matters - see "Schema evolution" in the module docstring.
    conn.executescript(TABLES)
    _migrate(conn)
    conn.executescript(INDEXES)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Add columns introduced after a database was first created. `CREATE TABLE
    IF NOT EXISTS` will not alter an existing table, so an older DB would
    otherwise keep failing on the new columns.
    """
    wanted = {
        "plans": {
            "pl_id": "TEXT", "district_name": "TEXT", "jurisdiction_name": "TEXT",
            "plan_area_name": "TEXT", "short_status": "TEXT", "area_dunam": "REAL",
            "landuse": "TEXT", "objectives": "TEXT", "housing_units": "INTEGER",
            "hotel_sqm": "REAL", "special_housing_sqm": "REAL",
            "min_lon": "REAL", "min_lat": "REAL", "max_lon": "REAL", "max_lat": "REAL",
            "depositing_date": "TEXT", "open_date": "TEXT",
            "pl_last_deposit_date": "TEXT", "pl_date_advertise": "TEXT",
            "pl_rejection_date": "TEXT",
        },
        "subscriptions": {"radius_m": "REAL", "active": "INTEGER NOT NULL DEFAULT 1"},
        "notifications": {"kind": "TEXT NOT NULL DEFAULT 'status_change'"},
        # JSON dict of {mode: bool}; a key absent from the dict means "enabled"
        # (see accounts.py DEFAULT_PERMISSIONS) - '{}' is full access, the
        # correct default for every user that existed before this column did.
        "users": {"permissions": "TEXT NOT NULL DEFAULT '{}'"},
    }
    # A GENERATED column cannot be added by the plain loop below, and an
    # existing database will not have it. VIRTUAL costs no storage and is
    # computed on read, so adding it to a 36,739-row table is instant.
    #
    # NOTE the pragma: `table_info` OMITS virtual generated columns, so probing
    # with it reports the column as missing on every run and the ALTER then
    # fails with "duplicate column name", breaking get_conn() for any existing
    # database. `table_xinfo` is the one that lists them. Same shape of bug as
    # the original min_lon migration failure - hence the explicit note.
    plans_cols = {r["name"] for r in conn.execute("PRAGMA table_xinfo(plans)")}
    if plans_cols and "pl_number_sq" not in plans_cols:
        conn.execute(
            "ALTER TABLE plans ADD COLUMN pl_number_sq TEXT GENERATED ALWAYS AS"
            " (replace(replace(replace(pl_number,' ',''),"
            " char(9),''), char(160),'')) VIRTUAL")

    # `notifications.object_id` was NOT NULL; the alert watchers need it
    # nullable. SQLite cannot drop NOT NULL with ALTER, so the table is rebuilt
    # and its rows copied. Guarded on the old shape, so it runs at most once.
    notif = list(conn.execute("PRAGMA table_info(notifications)"))
    if notif and any(r["name"] == "object_id" and r["notnull"] for r in notif):
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE notifications_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
                object_id       INTEGER REFERENCES plans(object_id),
                kind            TEXT NOT NULL DEFAULT 'status_change',
                message         TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                delivered       INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO notifications_new
                (id, subscription_id, object_id, kind, message, created_at, delivered)
                SELECT id, subscription_id, object_id, kind, message,
                       created_at, delivered FROM notifications;
            DROP TABLE notifications;
            ALTER TABLE notifications_new RENAME TO notifications;
            PRAGMA foreign_keys=ON;
        """)
        conn.commit()

    for table, columns in wanted.items():
        existing = {
            r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue
        for col, decl in columns.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def geometry_bbox(geometry_json: str | None) -> tuple:
    """
    (min_lon, min_lat, max_lon, max_lat) for a GeoJSON geometry string, without
    pulling in shapely - this runs for every row of a bulk load.
    """
    if not geometry_json:
        return (None, None, None, None)
    try:
        coords = json.loads(geometry_json).get("coordinates")
    except (ValueError, AttributeError):
        return (None, None, None, None)

    xs: list[float] = []
    ys: list[float] = []

    def walk(node):
        # A coordinate pair is a flat [x, y]; anything else is a nested list.
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                xs.append(float(node[0]))
                ys.append(float(node[1]))
            else:
                for child in node:
                    walk(child)

    walk(coords)
    if not xs:
        return (None, None, None, None)
    return (min(xs), min(ys), max(xs), max(ys))


def _plan_row(plan: dict, ts: str) -> tuple:
    bbox = plan.get("bbox") or geometry_bbox(plan.get("geometry_json"))
    values = {**plan, "min_lon": bbox[0], "min_lat": bbox[1],
              "max_lon": bbox[2], "max_lat": bbox[3]}
    return tuple(values.get(c) for c in PLAN_COLUMNS) + (ts, ts)


@contextmanager
def plans_writer(conn: sqlite3.Connection, batch_size: int = 500):
    """
    Bulk-load helper. Yields a `write(plan) -> (is_new, status_changed,
    old_station)` callable and commits every `batch_size` rows.

    Detecting new/changed rows needs the *previous* station, so the existing
    (object_id -> station) map is read up front in one query rather than
    per-row - one 36k-row scan instead of 36k point lookups.
    """
    known: dict[int, str | None] = {
        row["object_id"]: row["station"]
        for row in conn.execute("SELECT object_id, station FROM plans")
    }
    pending: list[tuple] = []
    log_pending: list[tuple] = []

    def flush():
        if pending:
            conn.executemany(_PLAN_SQL, pending)
            pending.clear()
        if log_pending:
            conn.executemany(
                """INSERT INTO plan_status_log
                   (object_id, old_station, new_station, detected_at)
                   VALUES (?,?,?,?)""",
                log_pending,
            )
            log_pending.clear()
        conn.commit()

    def write(plan: dict):
        object_id = plan["object_id"]
        ts = now_iso()
        is_new = object_id not in known
        old_station = known.get(object_id)
        new_station = plan.get("station")
        status_changed = (not is_new) and (old_station != new_station)

        pending.append(_plan_row(plan, ts))
        if status_changed:
            log_pending.append((object_id, old_station, new_station, ts))
        known[object_id] = new_station

        if len(pending) >= batch_size:
            flush()
        return is_new, status_changed, old_station

    try:
        yield write
    finally:
        flush()


def upsert_plan(conn: sqlite3.Connection, plan: dict):
    """
    Single-row upsert. Returns (is_new, status_changed, old_station).

    Convenient for tests and one-offs; for a full sync use `plans_writer()`,
    which avoids a commit per row.
    """
    row = conn.execute(
        "SELECT station FROM plans WHERE object_id = ?", (plan["object_id"],)
    ).fetchone()
    ts = now_iso()

    conn.execute(_PLAN_SQL, _plan_row(plan, ts))

    if row is None:
        conn.commit()
        return True, False, None

    old_station = row["station"]
    new_station = plan.get("station")
    changed = old_station != new_station
    if changed:
        conn.execute(
            """INSERT INTO plan_status_log
               (object_id, old_station, new_station, detected_at) VALUES (?,?,?,?)""",
            (plan["object_id"], old_station, new_station, ts),
        )
    conn.commit()
    return False, changed, old_station


def plans_covering_point(conn: sqlite3.Connection, lon: float, lat: float,
                         pad_lon: float = 0.0, pad_lat: float = 0.0):
    """
    Candidate plans whose bounding box contains (lon, lat), optionally padded
    per axis. Uses idx_plans_bbox; callers still need an exact geometric test,
    since a bbox hit is necessary but not sufficient.
    """
    return conn.execute(
        """SELECT * FROM plans
           WHERE min_lon IS NOT NULL
             AND min_lon <= ? AND max_lon >= ?
             AND min_lat <= ? AND max_lat >= ?""",
        (lon + pad_lon, lon - pad_lon, lat + pad_lat, lat - pad_lat),
    ).fetchall()


def add_subscription(conn, client_id, gush=None, helka=None, label=None,
                     lat=None, lon=None, radius_m=None) -> int:
    cur = conn.execute(
        """INSERT INTO subscriptions
           (client_id, gush, helka, label, lat, lon, radius_m, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (client_id, gush, helka, label, lat, lon, radius_m, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def create_notification(conn, subscription_id, object_id, message,
                        kind: str = "status_change") -> None:
    conn.execute(
        """INSERT INTO notifications
           (subscription_id, object_id, kind, message, created_at)
           VALUES (?,?,?,?,?)""",
        (subscription_id, object_id, kind, message, now_iso()),
    )


def start_sync_run(conn, mode: str) -> int:
    cur = conn.execute(
        "INSERT INTO sync_runs (started_at, mode) VALUES (?,?)", (now_iso(), mode)
    )
    conn.commit()
    return cur.lastrowid


def finish_sync_run(conn, run_id: int, *, seen=0, new=0, changes=0,
                    notifications=0, ok=True, error=None) -> None:
    conn.execute(
        """UPDATE sync_runs SET finished_at=?, plans_seen=?, plans_new=?,
           status_changes=?, notifications=?, ok=?, error=? WHERE id=?""",
        (now_iso(), seen, new, changes, notifications, 1 if ok else 0,
         error, run_id),
    )
    conn.commit()


def last_successful_sync(conn) -> str | None:
    """`started_at` of the most recent successful run - drives incremental sync."""
    row = conn.execute(
        "SELECT started_at FROM sync_runs WHERE ok=1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["started_at"] if row else None


def stats(conn) -> dict:
    def scalar(sql):
        return conn.execute(sql).fetchone()[0]

    return {
        "plans": scalar("SELECT COUNT(*) FROM plans"),
        # INDEXED BY is load-bearing, not a hint. idx_plans_has_geometry is a
        # partial index on exactly this predicate, but SQLite costs it as if it
        # covered few rows and picks a full scan instead - which on this table
        # means reading 184MB of geometry_json (16s cold, 1.5s warm, vs 0.02s
        # off the index). The index is recreated by _migrate() on every
        # get_conn(), so it is always present for the parser to bind to.
        "with_geometry": scalar(
            "SELECT COUNT(*) FROM plans INDEXED BY idx_plans_has_geometry "
            "WHERE geometry_json IS NOT NULL"),
        "subscriptions": scalar("SELECT COUNT(*) FROM subscriptions WHERE active=1"),
        "matches": scalar("SELECT COUNT(*) FROM subscription_plan_matches"),
        "status_changes": scalar("SELECT COUNT(*) FROM plan_status_log"),
        "notifications_pending": scalar("SELECT COUNT(*) FROM notifications WHERE delivered=0"),
        "last_sync": last_successful_sync(conn),
    }


if __name__ == "__main__":
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    conn = get_conn()
    print(f"DB: {DB_PATH}")
    for key, value in stats(conn).items():
        print(f"  {key:24} {value}")
    conn.close()
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
