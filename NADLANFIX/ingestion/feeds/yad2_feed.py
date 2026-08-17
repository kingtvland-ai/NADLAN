"""Persistent, single-pass harvest of the local Yad2 for-sale connector.

Why this module exists
----------------------
The previous implementation kept the Yad2 stock in a process-local dict and
refilled it from inside ``_live_sale_listings``. That had three defects which
together made the dashboard load Yad2 *forever*:

1. The refill was triggered whenever ``len(rows) < target``, and the target was
   18,000 - a figure the feed reaches only after ~900 pages. So virtually every
   request re-armed the worker, and the UI, which polls every 5s while
   ``loading`` is true, never saw a false.
2. The worker slept 2-12s between single-page fetches, so one honest pass cost
   over an hour. Restarting the process threw the whole pass away, because the
   rows lived only in memory.
3. A feed error raised out of the page loop, was recorded, and then the *next*
   request started the whole thing again from the failed page. A connector that
   was down produced an infinite retry storm rather than one visible failure.

The fix is to treat the harvest as a *job with a terminal state*, and to keep
its output in SQLite next to the rest of PlanWatch:

- A run ends in exactly one of ``complete`` / ``exhausted`` / ``failed`` /
  ``stopped``, is written to ``yad2_harvest_runs``, and is never auto-restarted.
  Refreshing is an explicit act (``POST /api/yad2/harvest``) or a TTL decision,
  never a side effect of rendering a table.
- Rows land in ``yad2_listings`` as they arrive, so a crash or a restart keeps
  every page already paid for. "One full load of everything we pulled" survives
  the process.
- Pages are fetched concurrently in small waves. Measured against the live
  connector, 4 workers do ~1.7s/page against ~5s/page before, which turns a
  900-page pass from >75 minutes into ~25.

The store also gives the lead engine (see ``lead_intel``) what it could not
have before: a price per token per day, which is what "how long has this been
for sale, and did the seller blink" is computed from.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

import db

#: Base URL of the local connector, and the definition name it exposes for the
#: for-sale board. Both are overridable because the connector has been observed
#: on 8099 and 8100 in this workspace, serving the same definition list.
API_BASE = os.environ.get("PLANWATCH_YAD2_API", "http://127.0.0.1:8100").rstrip("/")
API_PATH = os.environ.get("PLANWATCH_YAD2_PATH", "/api/yad2_forsale")

#: The connector returns 20 rows per page. Used only to estimate progress; the
#: harvest itself never assumes a page size.
PAGE_SIZE = 20

#: Hard ceiling on a single pass. 900 pages x 20 rows = the 18,000 the business
#: plan names as the Yad2 import target.
MAX_PAGES = 900

#: Rows fetched per wave. Kept small on purpose: the connector renders each page
#: in a real browser, and past ~6 parallel renders it starts shedding requests.
DEFAULT_WORKERS = 4

#: Waves that yield no *new* token before the pass is declared exhausted. It
#: counts new tokens rather than empty responses because Yad2 answers 200 with a
#: repeat of an earlier page once you run past the end of the board - an
#: emptiness check alone would run all 900 pages over the same 20 ads.
EXHAUSTION_WAVES = 3

#: Consecutive waves in which every page errored before the pass is failed. One
#: bad page is normal (the connector re-renders and Radware occasionally wins);
#: three whole waves means the connector is down, and the caller must be told
#: that rather than watching a spinner.
FAILURE_WAVES = 3

#: How long a completed harvest is considered current. A caller asking for rows
#: after this gets the stored rows *and* an "aged" flag; it never triggers an
#: automatic refetch, because an automatic refetch is what caused the original
#: bug. Refresh is a button.
FRESH_FOR_SECONDS = 6 * 60 * 60

#: A run row still marked "running" this long after it started, with no live
#: worker in this process, is treated as abandoned rather than in flight. Sized
#: well above a full pass (~25 min measured at 4 workers) so a slow but healthy
#: harvest is never mistaken for a dead one.
STALE_RUN_SECONDS = 2 * 60 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS yad2_listings (
    token           TEXT PRIMARY KEY,
    price           INTEGER,
    rooms           REAL,
    sqm             REAL,
    property_type   TEXT,
    city            TEXT,
    area            TEXT,
    neighborhood    TEXT,
    street          TEXT,
    floor           TEXT,
    lat             REAL,
    lon             REAL,
    agency          TEXT,
    image           TEXT,
    ad_type         TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    delisted_at     TEXT,
    /* Date decoded from the image URL - see image_date(). This is what lets
     * time-on-market reach back years instead of starting the day we first
     * scraped, and it is present on every row the board returns. */
    image_date      TEXT,
    /* Newest photo date ever seen for this token. Together with image_date
     * (the earliest) it answers two different questions: how long the ad has
     * been up, and when the seller last refreshed its content. A seller who
     * re-shoots the photos is doing something - that is a signal. */
    image_date_last TEXT,
    /* House number and the full image list, both of which only the real search
     * feed provides - the promoted-ads carousel carries neither. */
    house_number    TEXT,
    images_json     TEXT,
    /* Which source produced this row: "carousel" (the connector's promoted
     * ads) or "feed" (the real search feed, which is the only one carrying
     * private sellers). Kept so coverage per source stays visible. */
    origin          TEXT,
    raw_json        TEXT
);

/* Resume point per region, so a 3,800-page pass that is interrupted does not
 * start over. The store upserts by token, so re-fetching is harmless - it is
 * simply hours of wasted browser time. */
CREATE TABLE IF NOT EXISTS yad2_feed_progress (
    region       INTEGER PRIMARY KEY,
    last_page    INTEGER NOT NULL DEFAULT 0,
    total_pages  INTEGER,
    total_rows   INTEGER,
    updated_at   TEXT NOT NULL
);

/* Every observed price for a token, one row per change. This is the raw
 * material for "did the seller drop the price, and how long did it take" -
 * the single strongest motivation signal the plan asks for (section 5). Only
 * changes are written, so a token seen daily at one price stays one row. */
CREATE TABLE IF NOT EXISTS yad2_price_history (
    token       TEXT NOT NULL,
    price       INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (token, observed_at)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS yad2_harvest_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    pages_failed  INTEGER NOT NULL DEFAULT 0,
    rows_seen     INTEGER NOT NULL DEFAULT 0,
    rows_new      INTEGER NOT NULL DEFAULT 0,
    target        INTEGER,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_yad2_listings_city  ON yad2_listings (city);
CREATE INDEX IF NOT EXISTS idx_yad2_listings_price ON yad2_listings (price);
CREATE INDEX IF NOT EXISTS idx_yad2_listings_seen  ON yad2_listings (first_seen_at);
-- MAX(last_seen_at) is the freshness half of the dashboard's data version, and
-- the page polls it. Unindexed it scanned all 61,478 wide rows for 0.45s a call;
-- with the index the same answer is an O(1) read off the index tail.
CREATE INDEX IF NOT EXISTS idx_yad2_last_seen ON yad2_listings (last_seen_at);
"""

#: Columns of `yad2_listings` filled straight from a connector row.
FEED_COLUMNS = ("token", "price", "rooms", "sqm", "property_type", "city",
                "area", "neighborhood", "street", "floor", "lat", "lon",
                "agency", "image", "ad_type")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # `image_date` was added after rows already existed. Add the column to an
    # older store, then backfill it from the image URLs already held - the data
    # was in the store all along, just not decoded.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(yad2_listings)")}
    if "image_date" not in columns:
        conn.execute("ALTER TABLE yad2_listings ADD COLUMN image_date TEXT")
    if "image_date_last" not in columns:
        conn.execute("ALTER TABLE yad2_listings ADD COLUMN image_date_last TEXT")
    for name in ("house_number", "images_json", "origin"):
        if name not in columns:
            conn.execute(f"ALTER TABLE yad2_listings ADD COLUMN {name} TEXT")
    # Created here rather than in SCHEMA because it references image_date, which
    # the ALTERs above may have only just added. It indexes exactly the rows the
    # backfill below looks for: every read path calls ensure_schema, so without
    # it the store is scanned end to end several times per request (~0.4s over
    # 61k rows) for a migration that usually has nothing left to do. Once the
    # backfill is complete the index holds nothing and the lookup is instant.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_yad2_image_backfill
                   ON yad2_listings (token)
                WHERE image IS NOT NULL
                  AND (image_date IS NULL OR image_date_last IS NULL)""")
    conn.commit()
    backfill_image_dates(conn)


def backfill_image_dates(conn: sqlite3.Connection) -> int:
    """Decode image_date for stored rows that do not have one yet."""
    # Either column missing is worth a decode: image_date_last was added after
    # image_date had already been backfilled, so filtering on image_date alone
    # left every existing row without a "last refreshed" value.
    pending = conn.execute(
        """SELECT token, image FROM yad2_listings
            WHERE image IS NOT NULL
              AND (image_date IS NULL OR image_date_last IS NULL)""").fetchall()
    updates = [(image_date(row["image"]), image_date(row["image"]), row["token"])
               for row in pending]
    updates = [row for row in updates if row[0]]
    if updates:
        conn.executemany(
            """UPDATE yad2_listings
                  SET image_date      = COALESCE(image_date, ?),
                      image_date_last = COALESCE(image_date_last, ?)
                WHERE token = ?""", updates)
        conn.commit()
    return len(updates)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value, cast=float):
    if value in (None, ""):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


#: Yad2 image URLs carry the photo's upload timestamp, twice:
#:
#:   https://img.yad2.co.il/Pic/202604/19/2_5/o/y2_9_zpwmifAcQ3_20260419.jpg
#:                              ^^^^^^ ^^                       ^^^^^^^^
#:                              path             and            filename
#:
#: This is the single most valuable field in the feed and the board does not
#: publish it anywhere else. Measured over the full 14,445-row store: the path
#: form is present on **100%** of rows, the filename form on 100%, and the two
#: agree with each other on **100%** - which is what rules out coincidence.
#: The dates span 2014-2026 (225 ads from 2023, 749 from 2024, 3,762 from
#: 2025), so this is genuine multi-year evidence, not an artefact of when we
#: happened to scrape.
#:
#: What it means, precisely: the earliest date we can prove the listing's photo
#: existed. For most ads that is when the ad went up; for an ad whose photos
#: were later replaced it is more recent than the true posting date. So it is a
#: *lower bound on the age*, recorded with its own basis ("image") rather than
#: passed off as the seller's stated publication date.
_IMAGE_DATE_PATH = re.compile(r"/Pic/(20\d{2})(\d{2})/(\d{2})/")
_IMAGE_DATE_NAME = re.compile(r"_(20\d{2})(\d{2})(\d{2})\d{0,6}\.[a-z]+$", re.I)


def image_date(url: str | None) -> str | None:
    """ISO date encoded in a Yad2 image URL, or None.

    Prefers the path form and falls back to the filename; both are validated as
    a real calendar date, so a stray digit run cannot invent a listing age.
    """
    text = str(url or "")
    for pattern in (_IMAGE_DATE_PATH, _IMAGE_DATE_NAME):
        match = pattern.search(text)
        if not match:
            continue
        year, month, day = (int(part) for part in match.groups()[:3])
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------- run state

#: Live progress of the running pass. The DB row is the durable record; this is
#: what the UI polls, so it can show pages/rows moving without a table scan.
_STATE_LOCK = threading.Lock()
_STATE: dict = {
    "status": "idle",       # idle | running | complete | exhausted | failed | stopped
    "run_id": None,
    "started_at": None,
    "finished_at": None,
    "pages_fetched": 0,
    "pages_failed": 0,
    "rows_seen": 0,
    "rows_new": 0,
    "rows_stored": 0,
    "target": 0,
    "error": "",
}
_STOP = threading.Event()


def _set_state(**fields) -> None:
    with _STATE_LOCK:
        _STATE.update(fields)


def state() -> dict:
    """Snapshot of the harvest, including whether it is worth refreshing.

    ``running`` is the *only* value that should make a client poll again. It is
    deliberately false for ``failed``: a caller that keeps polling a broken
    connector is the bug this module was written to remove.
    """
    with _STATE_LOCK:
        snapshot = dict(_STATE)
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        snapshot["rows_stored"] = conn.execute(
            "SELECT COUNT(*) FROM yad2_listings WHERE delisted_at IS NULL"
        ).fetchone()[0]
        last = conn.execute(
            """SELECT id, started_at, finished_at, status, pages_fetched,
                      pages_failed, rows_new, rows_seen, target, error
                 FROM yad2_harvest_runs ORDER BY id DESC LIMIT 1""").fetchone()
    finally:
        conn.close()
    if last and snapshot["status"] == "idle":
        # A fresh process has no in-memory state; report the durable one so a
        # restart does not present a finished harvest as "never run".
        snapshot.update(status=last["status"], run_id=last["id"],
                        started_at=last["started_at"],
                        finished_at=last["finished_at"],
                        pages_fetched=last["pages_fetched"],
                        pages_failed=last["pages_failed"],
                        rows_new=last["rows_new"], rows_seen=last["rows_seen"],
                        target=last["target"] or 0, error=last["error"] or "")
        # A run row left at "running" by a process that died would be reported
        # as in-flight forever, and a client that polls "running" would poll
        # forever - the precise failure this module exists to remove. A run
        # whose owner is gone and which started longer ago than any honest pass
        # takes is reported as stalled, which is terminal.
        if (last["status"] == "running" and not is_running()
                and (_age_seconds(last["started_at"]) or 0) > STALE_RUN_SECONDS):
            snapshot["status"] = "stalled"
            snapshot["error"] = ("הטעינה הקודמת נקטעה (התהליך שהריץ אותה אינו "
                                 "פעיל). לחץ 'רענן יד 2' כדי להריץ מחדש.")
    snapshot["running"] = snapshot["status"] == "running"
    snapshot["age_seconds"] = _age_seconds(snapshot.get("finished_at"))
    snapshot["stale"] = (snapshot["age_seconds"] is not None
                         and snapshot["age_seconds"] > FRESH_FOR_SECONDS)
    snapshot["source_url"] = f"{API_BASE}{API_PATH}"
    return snapshot


def _age_seconds(finished_at: str | None) -> float | None:
    if not finished_at:
        return None
    try:
        stamp = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


# ----------------------------------------------------------------- fetching

class FeedError(RuntimeError):
    """The connector could not answer for this page."""


def fetch_page(page: int, timeout: float = 60.0) -> list[dict]:
    """One page from the local connector. Raises FeedError, never returns None."""
    url = f"{API_BASE}{API_PATH}?page={int(page)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code} on page {page}") from exc
    except Exception as exc:
        raise FeedError(f"{type(exc).__name__} on page {page}: {exc}") from exc
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise FeedError(f"page {page} carried no data array")
    return [row for row in rows if isinstance(row, dict)]


def _store_rows(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """Upsert a wave. Returns (seen, newly inserted).

    A token already present keeps its ``first_seen_at`` - that date is the only
    evidence PlanWatch owns for how long an ad has been on the board, so it must
    never be overwritten by a later sighting.
    """
    if not rows:
        return 0, 0
    now = _now()
    before = conn.execute("SELECT COUNT(*) FROM yad2_listings").fetchone()[0]
    payload = []
    for row in rows:
        token = str(row.get("token") or row.get("id") or "").strip()
        if not token:
            continue
        payload.append((
            token, _number(row.get("price"), int), _number(row.get("rooms")),
            _number(row.get("sqm")), row.get("property_type"), row.get("city"),
            row.get("area"), row.get("neighborhood"), row.get("street"),
            None if row.get("floor") is None else str(row.get("floor")),
            _number(row.get("lat")), _number(row.get("lon")), row.get("agency"),
            row.get("image"), row.get("ad_type"), now, now,
            image_date(row.get("image")), image_date(row.get("image")),
            json.dumps(row, ensure_ascii=False),
        ))
    if not payload:
        return 0, 0
    conn.executemany(
        f"""INSERT INTO yad2_listings ({", ".join(FEED_COLUMNS)},
                                       first_seen_at, last_seen_at,
                                       image_date, image_date_last, raw_json)
            VALUES ({", ".join("?" for _ in FEED_COLUMNS)}, ?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                price=excluded.price, rooms=excluded.rooms, sqm=excluded.sqm,
                property_type=excluded.property_type, city=excluded.city,
                area=excluded.area, neighborhood=excluded.neighborhood,
                street=excluded.street, floor=excluded.floor,
                lat=excluded.lat, lon=excluded.lon, agency=excluded.agency,
                image=excluded.image, ad_type=excluded.ad_type,
                last_seen_at=excluded.last_seen_at,
                seen_count=yad2_listings.seen_count + 1,
                delisted_at=NULL,
                /* Keep the EARLIEST image date ever seen for this token. A
                 * seller who swaps the photos gets a newer URL, and taking the
                 * new one would reset the listing's apparent age to zero -
                 * turning "on the market 14 months" into "brand new" exactly
                 * when the seller is trying to make it look fresh. SQLite's
                 * MIN(a,b) is NULL if either side is, hence the COALESCE. */
                image_date=COALESCE(
                    MIN(yad2_listings.image_date, excluded.image_date),
                    yad2_listings.image_date, excluded.image_date),
                /* ...and the newest, which moves when the seller re-shoots. */
                image_date_last=COALESCE(
                    MAX(yad2_listings.image_date_last, excluded.image_date_last),
                    excluded.image_date_last, yad2_listings.image_date_last),
                raw_json=excluded.raw_json""", payload)

    # Price history: append only when the price actually moved, so the table
    # stays a change log rather than a copy of every sighting.
    conn.executemany(
        """INSERT OR IGNORE INTO yad2_price_history (token, price, observed_at)
           SELECT ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM yad2_price_history h
                 WHERE h.token = ?
                   AND h.price IS ?
                   AND h.observed_at = (SELECT MAX(observed_at)
                                          FROM yad2_price_history
                                         WHERE token = ?))""",
        [(item[0], item[1], now, item[0], item[1], item[0]) for item in payload])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM yad2_listings").fetchone()[0]
    return len(payload), after - before


def _open_run(conn: sqlite3.Connection, target: int) -> int:
    cursor = conn.execute(
        "INSERT INTO yad2_harvest_runs (started_at, status, target) VALUES (?,?,?)",
        (_now(), "running", target))
    conn.commit()
    return int(cursor.lastrowid)


def _progress(conn: sqlite3.Connection, run_id: int, *, pages: int,
              failed: int, seen: int, new: int) -> None:
    conn.execute(
        """UPDATE yad2_harvest_runs
              SET pages_fetched=?, pages_failed=?, rows_seen=?, rows_new=?
            WHERE id=?""", (pages, failed, seen, new, run_id))
    conn.commit()


def _close_run(conn: sqlite3.Connection, run_id: int, status: str,
               *, pages: int, failed: int, seen: int, new: int,
               error: str = "") -> None:
    conn.execute(
        """UPDATE yad2_harvest_runs
              SET finished_at=?, status=?, pages_fetched=?, pages_failed=?,
                  rows_seen=?, rows_new=?, error=?
            WHERE id=?""",
        (_now(), status, pages, failed, seen, new, error or None, run_id))
    conn.commit()


# ------------------------------------------------------------------ the pass

def harvest(target: int = 18000, workers: int = DEFAULT_WORKERS,
            max_pages: int = MAX_PAGES) -> dict:
    """Run ONE pass over the connector and return its terminal state.

    Terminates on the first of: the target reached, the board exhausted, the
    page ceiling, an explicit stop, or the connector failing for
    ``FAILURE_WAVES`` waves running. It never loops waiting for more.
    """
    target = max(1, min(int(target), 100_000))
    workers = max(1, min(int(workers), 8))
    conn = db.get_conn()
    ensure_schema(conn)
    run_id = _open_run(conn, target)
    _STOP.clear()
    _set_state(status="running", run_id=run_id, started_at=_now(),
               finished_at=None, pages_fetched=0, pages_failed=0, rows_seen=0,
               rows_new=0, target=target, error="")

    pages = failed = seen = new = 0
    barren = 0     # consecutive waves that added no new token
    dead = 0       # consecutive waves in which every page errored
    status, error = "complete", ""
    page = 1
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            while page <= max_pages:
                if _STOP.is_set():
                    status = "stopped"
                    break
                wave = list(range(page, min(page + workers, max_pages + 1)))
                page = wave[-1] + 1

                results, errors = [], []
                for outcome in pool.map(_safe_fetch, wave):
                    if isinstance(outcome, str):
                        errors.append(outcome)
                    else:
                        results.append(outcome)
                pages += len(results)
                failed += len(errors)

                if results:
                    dead = 0
                else:
                    dead += 1
                    if dead >= FAILURE_WAVES:
                        status = "failed"
                        error = errors[-1] if errors else "המחבר המקומי לא מגיב"
                        break
                    continue

                flat = [row for batch in results for row in batch]
                wave_seen, wave_new = _store_rows(conn, flat)
                seen += wave_seen
                new += wave_new
                _set_state(pages_fetched=pages, pages_failed=failed,
                           rows_seen=seen, rows_new=new)
                # Progress also goes to the run row, not just this process's
                # memory. A harvest started from the CLI, or one that outlives a
                # dashboard restart, is otherwise "running" with no numbers -
                # which looks exactly like the hang this module replaced.
                _progress(conn, run_id, pages=pages, failed=failed,
                          seen=seen, new=new)

                # Exhaustion is measured in *new* tokens, not empty pages: past
                # the end of the board Yad2 answers 200 with a repeat.
                barren = 0 if wave_new else barren + 1
                if barren >= EXHAUSTION_WAVES:
                    status = "exhausted"
                    break

                stored = conn.execute(
                    "SELECT COUNT(*) FROM yad2_listings").fetchone()[0]
                if stored >= target:
                    status = "complete"
                    break
            else:
                status = "complete"
    except Exception as exc:                       # pragma: no cover - defensive
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        _close_run(conn, run_id, status, pages=pages, failed=failed,
                   seen=seen, new=new, error=error)
        conn.close()
        _set_state(status=status, finished_at=_now(), pages_fetched=pages,
                   pages_failed=failed, rows_seen=seen, rows_new=new,
                   error=error)
    return state()


# -------------------------------------------------- the real search feed
#
# The carousel harvest above reads the connector's `platinumAds` path. That is
# the *paid promotion* strip: 14,445 rows, every one with an agency, not one
# private seller. The real search feed carries both, and far more of them -
# measured 2026-08-05 across the six regions: 87,672 listings, ~20 private and
# ~23 agency per page. It also carries the house number and the full image
# list, neither of which the carousel has.
#
# It is only reachable from inside a browser holding a live Yad2 session, so
# the Node service owns that part (see backend/src/feed.js) and this harvests
# through it. That is a real dependency: `npm run start` in backend/ must be
# up, and the harvest says so plainly when it is not.

FEED_API = os.environ.get("PLANWATCH_YAD2_FEED", "http://127.0.0.1:4000").rstrip("/")

#: The six regions that together cover the country. Yad2 rejects a feed request
#: with no region (HTTP 400), so there is no "everything" call.
FEED_REGIONS = (1, 2, 3, 5, 6, 7)


def fetch_feed_page(region: int, page: int, timeout: float = 180.0) -> dict:
    """One page of the real feed for one region, via the Node service."""
    url = f"{FEED_API}/api/yad2-feed?region={int(region)}&page={int(page)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code} on region {region} page {page}") from exc
    except Exception as exc:
        raise FeedError(
            f"{type(exc).__name__} on region {region} page {page}: {exc}. "
            f"האם שירות ה-Node רץ? (cd backend && npm run start)") from exc


def _store_feed_rows(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """Upsert feed rows. Same table as the carousel, richer columns."""
    if not rows:
        return 0, 0
    now = _now()
    before = conn.execute("SELECT COUNT(*) FROM yad2_listings").fetchone()[0]
    payload = []
    for row in rows:
        token = str(row.get("token") or "").strip()
        if not token:
            continue
        image = row.get("image")
        payload.append((
            token, _number(row.get("price"), int), _number(row.get("rooms")),
            _number(row.get("sqm")), row.get("property_type"), row.get("city"),
            row.get("area"), row.get("neighborhood"), row.get("street"),
            None if row.get("floor") is None else str(row.get("floor")),
            _number(row.get("lat")), _number(row.get("lon")), row.get("agency"),
            image, row.get("ad_type"), now, now,
            image_date(image), image_date(image),
            None if row.get("house_number") is None else str(row["house_number"]),
            json.dumps(row.get("images") or [], ensure_ascii=False),
            "feed", json.dumps(row, ensure_ascii=False),
        ))
    if not payload:
        return 0, 0
    conn.executemany(
        f"""INSERT INTO yad2_listings ({", ".join(FEED_COLUMNS)},
                first_seen_at, last_seen_at, image_date, image_date_last,
                house_number, images_json, origin, raw_json)
            VALUES ({", ".join("?" for _ in FEED_COLUMNS)}, ?,?,?,?,?,?,?,?)
            ON CONFLICT(token) DO UPDATE SET
                price=excluded.price, rooms=excluded.rooms, sqm=excluded.sqm,
                property_type=excluded.property_type, city=excluded.city,
                area=excluded.area, neighborhood=excluded.neighborhood,
                street=excluded.street, floor=excluded.floor,
                lat=excluded.lat, lon=excluded.lon,
                agency=COALESCE(excluded.agency, yad2_listings.agency),
                image=excluded.image,
                /* ad_type from the feed is Yad2's own word for who is selling,
                 * so it must win over the carousel's blanket "commercial". */
                ad_type=excluded.ad_type,
                last_seen_at=excluded.last_seen_at,
                seen_count=yad2_listings.seen_count + 1,
                delisted_at=NULL,
                image_date=COALESCE(
                    MIN(yad2_listings.image_date, excluded.image_date),
                    yad2_listings.image_date, excluded.image_date),
                image_date_last=COALESCE(
                    MAX(yad2_listings.image_date_last, excluded.image_date_last),
                    excluded.image_date_last, yad2_listings.image_date_last),
                house_number=COALESCE(excluded.house_number,
                                      yad2_listings.house_number),
                images_json=COALESCE(excluded.images_json,
                                     yad2_listings.images_json),
                origin='feed',
                raw_json=excluded.raw_json""", payload)

    conn.executemany(
        """INSERT OR IGNORE INTO yad2_price_history (token, price, observed_at)
           SELECT ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM yad2_price_history h
                 WHERE h.token = ? AND h.price IS ?
                   AND h.observed_at = (SELECT MAX(observed_at)
                                          FROM yad2_price_history
                                         WHERE token = ?))""",
        [(item[0], item[1], now, item[0], item[1], item[0]) for item in payload])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM yad2_listings").fetchone()[0]
    return len(payload), after - before


def harvest_feed(*, regions=FEED_REGIONS, max_pages_per_region: int | None = None,
                 resume: bool = True) -> dict:
    """Harvest the real search feed, region by region, resumably.

    ~3,800 pages at ~2.6s each is roughly two and a half hours for a first full
    pass, so it records its position per region in ``yad2_feed_progress`` and
    picks up from there. Re-fetching a page is harmless (rows upsert by token);
    it just wastes browser time, which is the expensive part here.
    """
    conn = db.get_conn()
    ensure_schema(conn)
    run_id = _open_run(conn, 0)
    _STOP.clear()
    _set_state(status="running", run_id=run_id, started_at=_now(),
               finished_at=None, pages_fetched=0, pages_failed=0, rows_seen=0,
               rows_new=0, target=0, error="")

    pages = failed = seen = new = 0
    status, error = "complete", ""
    try:
        for region in regions:
            if _STOP.is_set():
                status = "stopped"
                break
            row = conn.execute(
                "SELECT last_page, total_pages FROM yad2_feed_progress WHERE region=?",
                (region,)).fetchone()
            start = (row["last_page"] + 1) if (row and resume) else 1
            total_pages = row["total_pages"] if row else None

            page = start
            while True:
                if _STOP.is_set():
                    status = "stopped"
                    break
                if total_pages and page > total_pages:
                    break
                if max_pages_per_region and page - start + 1 > max_pages_per_region:
                    break
                try:
                    payload = fetch_feed_page(region, page)
                except FeedError as exc:
                    failed += 1
                    # One bad page is normal; a wall of them means the browser
                    # session died and continuing would just burn hours.
                    if failed % 25 == 0:
                        status, error = "failed", str(exc)
                        break
                    page += 1
                    continue

                rows = payload.get("rows") or []
                total_pages = payload.get("totalPages") or total_pages
                if not rows:
                    break
                wave_seen, wave_new = _store_feed_rows(conn, rows)
                pages += 1
                seen += wave_seen
                new += wave_new
                conn.execute(
                    """INSERT INTO yad2_feed_progress
                           (region, last_page, total_pages, total_rows, updated_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(region) DO UPDATE SET
                           last_page=excluded.last_page,
                           total_pages=excluded.total_pages,
                           total_rows=excluded.total_rows,
                           updated_at=excluded.updated_at""",
                    (region, page, total_pages, payload.get("total"), _now()))
                conn.commit()
                _set_state(pages_fetched=pages, pages_failed=failed,
                           rows_seen=seen, rows_new=new)
                _progress(conn, run_id, pages=pages, failed=failed,
                          seen=seen, new=new)
                page += 1
            if status in ("failed", "stopped"):
                break
    except Exception as exc:                        # pragma: no cover
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        _close_run(conn, run_id, status, pages=pages, failed=failed,
                   seen=seen, new=new, error=error)
        conn.close()
        _set_state(status=status, finished_at=_now(), pages_fetched=pages,
                   pages_failed=failed, rows_seen=seen, rows_new=new,
                   error=error)
    return state()


def start_feed(**kwargs) -> dict:
    """Run harvest_feed in the background, at most one at a time."""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return state()
        _WORKER = threading.Thread(target=harvest_feed, kwargs=kwargs,
                                   name="yad2-feed-harvest", daemon=True)
        _set_state(status="running", started_at=_now(), finished_at=None,
                   error="")
        _WORKER.start()
    return state()


def _safe_fetch(page: int):
    """Fetch with one retry. Returns rows, or the error text on failure."""
    for attempt in range(2):
        try:
            return fetch_page(page)
        except FeedError as exc:
            if attempt == 0:
                time.sleep(1.5)
                continue
            return str(exc)
    return f"page {page} failed"


_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def start(target: int = 18000, workers: int = DEFAULT_WORKERS) -> dict:
    """Start a pass in the background if one is not already running.

    Idempotent by design: calling it while a pass runs returns the running
    state instead of starting a second one. That single guard is what stops the
    "every request arms another worker" behaviour that produced the endless
    load.
    """
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return state()
        _WORKER = threading.Thread(
            target=harvest, kwargs={"target": target, "workers": workers},
            name="yad2-harvest", daemon=True)
        _set_state(status="running", started_at=_now(), finished_at=None,
                   error="", target=target)
        _WORKER.start()
    return state()


def stop() -> dict:
    """Ask the running pass to finish at the next wave boundary."""
    _STOP.set()
    return state()


def is_running() -> bool:
    with _WORKER_LOCK:
        return _WORKER is not None and _WORKER.is_alive()


# ------------------------------------------------------------------ reading

def rows(limit: int | None = None) -> list[dict]:
    """Every stored Yad2 listing, newest sighting first.

    Reads the store, not the network. This is the whole point: rendering the
    table can never start a scrape.
    """
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        sql = ("""SELECT token, price, rooms, sqm, property_type, city, area,
                         neighborhood, street, floor, lat, lon, agency, image,
                         ad_type, first_seen_at, last_seen_at, seen_count,
                         image_date, image_date_last, images_json, house_number
                    FROM yad2_listings
                   WHERE delisted_at IS NULL
                   ORDER BY first_seen_at DESC""")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(row) for row in conn.execute(sql)]
    finally:
        conn.close()


def counts() -> dict:
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        row = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN agency IS NOT NULL AND trim(agency) <> ''
                               THEN 1 ELSE 0 END) agency_rows,
                      MIN(first_seen_at) first_seen, MAX(last_seen_at) last_seen
                 FROM yad2_listings WHERE delisted_at IS NULL""").fetchone()
        return {"total": row["total"] or 0,
                "with_agency": row["agency_rows"] or 0,
                "first_seen": row["first_seen"], "last_seen": row["last_seen"]}
    finally:
        conn.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Harvest the local Yad2 feed once")
    parser.add_argument("--target", type=int, default=18000)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--status", action="store_true",
                        help="print the stored state and exit")
    args = parser.parse_args()
    if args.status:
        print(json.dumps(state(), ensure_ascii=False, indent=2))
        return 0
    result = harvest(args.target, args.workers, args.max_pages)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("complete", "exhausted", "stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
