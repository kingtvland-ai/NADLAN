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
