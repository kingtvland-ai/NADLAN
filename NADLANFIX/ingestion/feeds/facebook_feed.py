"""Facebook Marketplace for-sale listings harvester.

Why this module exists
----------------------
Facebook Marketplace publishes Israeli real-estate ads that do not appear on
Yad2 or ONMAP. The scraper uses Playwright (Python) with a persistent browser
profile to bypass the login wall and extract listing cards from search result
pages. The harvest is treated as a *job with a terminal state* (complete /
exhausted / failed / stopped), and rows are persisted to SQLite as they arrive
so a crash or restart does not lose already-fetched pages.

Architecture
------------
- One browser context is reused across the whole pass.
- City search pages are fetched concurrently in small waves.
- Each card is extracted via DOM snapshot (one round-trip per page).
- Detail enrichment (description, seller) is best-effort for newly inserted
  listings only.
- Price history is recorded on every change.
- Deduplication is by listing_id (Facebook's internal item number).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import db
from ingestion.normalization.facebook_metadata import infer_metadata
from ingestion.normalization.facebook_comparables import comparable_anchor, title_similarity, MIN_COMPARABLE_SIMILARITY
from ingestion.normalization.facebook_valuation import valuation_profile
from ingestion.normalization.facebook_scoring import score_listing

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Path to the Playwright storage-state file (cookies + localStorage). Created
#: by `fb-market session login` in the FacebookMScrap project; copied or
#: symlinked here for reuse.
STORAGE_STATE_PATH = Path(
    os.environ.get("PLANWATCH_FB_STORAGE_STATE",
                   Path(__file__).parent / "data" / "facebook_storage_state.json")
)

#: Default search query - real-estate for-sale in Hebrew.
DEFAULT_QUERY = os.environ.get("PLANWATCH_FB_QUERY", "מכירות בתים")

#: Minimum price filter (ILS).
DEFAULT_MIN_PRICE = int(os.environ.get("PLANWATCH_FB_MIN_PRICE", "400000"))

#: How many listings one harvest pass aims to collect.
HARVEST_TARGET = int(os.environ.get("PLANWATCH_FB_TARGET", "500"))

#: Pages fetched per wave. Kept small to avoid shedding requests.
DEFAULT_WORKERS = 3

#: Waves that yield no new listing_id before the pass is declared exhausted.
EXHAUSTION_WAVES = 3

#: Consecutive waves in which every page errored before the pass is failed.
FAILURE_WAVES = 3

#: How long a completed harvest is considered current.
FRESH_FOR_SECONDS = 6 * 60 * 60

#: A run row still marked "running" this long after it started, with no live
#: worker in this process, is treated as abandoned.
STALE_RUN_SECONDS = 2 * 60 * 60

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_facebook_city       ON facebook_listings (city_name);
CREATE INDEX IF NOT EXISTS idx_facebook_price      ON facebook_listings (price_value);
CREATE INDEX IF NOT EXISTS idx_facebook_last_seen  ON facebook_listings (last_seen_at);
CREATE INDEX IF NOT EXISTS idx_facebook_deal_score ON facebook_listings (deal_score DESC);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value, cast=float):
    if value in (None, ""):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(facebook_listings)")}
    for name, kind in (
        ("deal_score", "REAL NOT NULL DEFAULT 0"),
        ("score_confidence", "REAL NOT NULL DEFAULT 0"),
        ("classification_source", "TEXT NOT NULL DEFAULT 'heuristic'"),
        ("classification_confidence", "REAL NOT NULL DEFAULT 0"),
        ("restricted", "INTEGER NOT NULL DEFAULT 0"),
        ("score_reasons", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE facebook_listings ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError:
                pass
    conn.executescript(_fb_cities_schema())
    conn.commit()


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------

_STATE: dict = {}
_STATE_LOCK = threading.Lock()


def state() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def counts(conn: sqlite3.Connection | None = None) -> dict:
    """Row counts and freshness for the Facebook store."""
    own = conn is None
    if own:
        conn = db.get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM facebook_listings").fetchone()[0]
        last_seen = conn.execute(
            "SELECT MAX(last_seen_at) FROM facebook_listings"
        ).fetchone()[0]
        new_today = 0
        if last_seen:
            today = datetime.now(timezone.utc).date().isoformat()
            new_today = conn.execute(
                "SELECT COUNT(*) FROM facebook_listings WHERE first_seen_at >= ?",
                (today,)
            ).fetchone()[0]
        return {"total": total, "last_seen": last_seen, "new_today": new_today}
    finally:
        if own:
            conn.close()


def rows(conn: sqlite3.Connection | None = None, limit: int = 0) -> list[dict]:
    """All stored Facebook listings, optionally limited."""
    own = conn is None
    if own:
        conn = db.get_conn()
    try:
        sql = "SELECT *, listing_id AS token FROM facebook_listings ORDER BY last_seen_at DESC"
        if limit > 0:
            sql += f" LIMIT {limit}"
        return [dict(row) for row in conn.execute(sql).fetchall()]
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def _build_search_url(city_id: str, query: str, min_price: int | None = None) -> str:
    params = []
    if min_price is not None:
        params.append(f"minPrice={min_price}")
    params.append(f"query={quote_plus(query)}")
    params.append("exact=false")
    return f"https://www.facebook.com/marketplace/{city_id}/search?" + "&".join(params)


def _extract_cards(page, max_items: int) -> list[dict]:
    """Snapshot all marketplace item cards from the current page."""
    try:
        page.locator("a[href*='/marketplace/item/']").first.wait_for(
            state="attached", timeout=12_000
        )
    except Exception:
        return []

    for _ in range(12):
        current = page.locator("a[href*='/marketplace/item/']").count()
        if current >= max_items:
            break
        page.mouse.wheel(0, 2400)
        time.sleep(0.45)

    links = page.locator("a[href*='/marketplace/item/']")
    limit = max_items * 3
    return links.evaluate_all(
        """(nodes, limit) => nodes.slice(0, limit).map(anchor => {
            const image = anchor.querySelector('img');
            return {
                href: anchor.getAttribute('href'),
                text: anchor.innerText || '',
                aria_label: anchor.getAttribute('aria-label'),
                image_url: image ? image.getAttribute('src') : null,
                image_alt: image ? image.getAttribute('alt') : null
            };
        })""",
        limit,
    )


def _normalize_card(record: dict, query: str) -> dict | None:
    """Convert a raw card snapshot into a normalized listing dict."""
    href = str(record.get("href") or "")
    if "/marketplace/item/" not in href:
        return None

    listing_id_match = re.search(r"/marketplace/item/(\d+)", href)
    listing_id = listing_id_match.group(1) if listing_id_match else href

    text = str(record.get("text") or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    price_text = None
    price_value = None
    currency = "ILS"
    for line in lines:
        if "₪" in line or "$" in line or "ILS" in line:
            price_text = line
            price_match = re.search(r"[\d,]+(?:\.\d{1,2})?", line.replace(",", ""))
            if price_match:
                try:
                    price_value = float(price_match.group().replace(",", ""))
                except ValueError:
                    pass
            if "₪" in line:
                currency = "ILS"
            elif "$" in line:
                currency = "USD"
            break

    title = None
    for line in lines:
        if line != price_text and not re.fullmatch(r"[\d,]+(?:\.\d{1,2})?", line.replace(",", "")):
            title = line
            break
    if not title:
        title = str(record.get("aria_label") or record.get("image_alt") or "Untitled")

    location = None
    if len(lines) >= 3:
        candidates = [line for line in lines if line not in {title, price_text}]
        if candidates:
            location = candidates[-1]

    image_url = str(record.get("image_url") or "").strip() or None
    absolute_url = f"https://www.facebook.com{href}" if href.startswith("/") else href

    normalized_title = " ".join(title.casefold().split())
    fingerprint_source = f"{normalized_title}|{(location or '').casefold().strip()}"
    fingerprint = hashlib.sha256(fingerprint_source.encode()).hexdigest()[:24]

    metadata = infer_metadata(title=title, body="")

    return {
        "listing_id": listing_id,
        "title": title,
        "normalized_title": normalized_title,
        "fingerprint": fingerprint,
        "url": absolute_url,
        "price_text": price_text,
        "price_value": price_value,
        "currency": currency,
        "location": location,
        "image_url": image_url,
        "seller_name": None,
        "description": None,
        "category": metadata["category"],
        "condition": metadata["condition"],
        "classification_source": metadata["source"],
        "classification_confidence": metadata["confidence"],
        "restricted": metadata["restricted"],
        "source_query": query,
        "first_seen_at": _now(),
        "last_seen_at": _now(),
        "seen_count": 1,
        "delisted_at": None,
        "raw_json": json.dumps(record, ensure_ascii=False),
    }


def _upsert_listing(conn: sqlite3.Connection, listing: dict) -> tuple[bool, bool]:
    """Insert or update a listing. Returns (inserted, price_changed)."""
    existing = conn.execute(
        "SELECT price_value, seen_count FROM facebook_listings WHERE listing_id = ?",
        (listing["listing_id"],)
    ).fetchone()

    now = _now()
    price_changed = False

    if existing is None:
        conn.execute(
            """INSERT INTO facebook_listings
               (listing_id, title, normalized_title, fingerprint, url, price_text,
                price_value, currency, location, image_url, seller_name, description,
                category, condition, classification_source, classification_confidence,
                restricted, source_query, first_seen_at, last_seen_at,
                seen_count, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                listing["listing_id"], listing["title"], listing["normalized_title"],
                listing["fingerprint"], listing["url"], listing["price_text"],
                listing["price_value"], listing["currency"], listing["location"],
                listing["image_url"], listing["seller_name"], listing["description"],
                listing["category"], listing["condition"],
                listing["classification_source"], listing["classification_confidence"],
                listing["restricted"], listing["source_query"],
                listing["first_seen_at"], now, 1, listing["raw_json"],
            )
        )
        if listing["price_value"] is not None:
            conn.execute(
                """INSERT INTO facebook_price_history
                   (listing_id, price_text, price_value, currency, observed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (listing["listing_id"], listing["price_text"], listing["price_value"],
                 listing["currency"], now)
            )
        return True, True
    else:
        old_price = existing["price_value"]
        new_count = existing["seen_count"] + 1
        conn.execute(
            """UPDATE facebook_listings
               SET last_seen_at = ?, seen_count = ?, price_text = ?, price_value = ?,
                   currency = ?, raw_json = ?
               WHERE listing_id = ?""",
            (now, new_count, listing["price_text"], listing["price_value"],
             listing["currency"], listing["raw_json"], listing["listing_id"])
        )
        if listing["price_value"] is not None and old_price != listing["price_value"]:
            conn.execute(
                """INSERT INTO facebook_price_history
                   (listing_id, price_text, price_value, currency, observed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (listing["listing_id"], listing["price_text"], listing["price_value"],
                 listing["currency"], now)
            )
            price_changed = True
        return False, price_changed


def _compute_comparable_stats(conn: sqlite3.Connection, listing: dict, limit: int = 20) -> dict:
    """Find comparable listings and compute price statistics."""
    anchor = comparable_anchor(listing.get("normalized_title", ""))
    if not anchor:
        return {"sample_size": 0, "median_price": None, "min_price": None, "max_price": None, "previous_price": None}

    candidates = conn.execute(
        """SELECT price_value, normalized_title FROM facebook_listings
           WHERE listing_id != ? AND price_value IS NOT NULL AND price_value > 0
           AND normalized_title LIKE ?
           ORDER BY last_seen_at DESC LIMIT ?""",
        (listing["listing_id"], f"%{anchor}%", limit * 2)
    ).fetchall()

    if not candidates:
        return {"sample_size": 0, "median_price": None, "min_price": None, "max_price": None, "previous_price": None}

    similar = []
    for row in candidates:
        sim = title_similarity(listing.get("normalized_title", ""), row["normalized_title"])
        if sim >= MIN_COMPARABLE_SIMILARITY:
            similar.append((sim, row["price_value"]))

    if not similar:
        return {"sample_size": 0, "median_price": None, "min_price": None, "max_price": None, "previous_price": None}

    similar.sort(key=lambda x: x[0], reverse=True)
    prices = [p for _, p in similar[:limit]]
    prices.sort()

    median_price = float(prices[len(prices) // 2]) if prices else None
    return {
        "sample_size": len(prices),
        "median_price": median_price,
        "min_price": float(prices[0]) if prices else None,
        "max_price": float(prices[-1]) if prices else None,
        "previous_price": None,
    }


def _score_and_update(conn: sqlite3.Connection, listing: dict) -> None:
    """Compute deal score for a listing and update it in the database."""
    metadata = infer_metadata(
        title=listing.get("title", ""),
        body=listing.get("description") or "",
        category_hint=listing.get("category"),
        condition_hint=listing.get("condition"),
    )

    category = metadata["category"]
    condition = metadata["condition"]
    classification_confidence = metadata["confidence"]
    classification_source = metadata["source"]
    restricted = metadata["restricted"]

    stats = _compute_comparable_stats(conn, listing)
    score_result = score_listing(
        {
            "price_value": listing.get("price_value"),
            "category": category,
            "condition": condition,
            "restricted": restricted,
        },
        stats,
    )

    deal_score = score_result.get("deal_score", 0.0)
    score_confidence = score_result.get("confidence", 0.0)
    score_reasons = json.dumps(score_result.get("reasons", []), ensure_ascii=False)

    conn.execute(
        """UPDATE facebook_listings
           SET category = ?, condition = ?, classification_source = ?,
               classification_confidence = ?, restricted = ?,
               deal_score = ?, score_confidence = ?, score_reasons = ?
           WHERE listing_id = ?""",
        (category, condition, classification_source, classification_confidence,
         restricted, deal_score, score_confidence, score_reasons,
         listing["listing_id"])
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    _ensure_schema(conn)


def start(target: int = HARVEST_TARGET) -> dict:
    """Start a new Facebook harvest pass in a background thread.

    Returns the initial run state dict.
    """
    with _STATE_LOCK:
        if _STATE.get("running"):
            return dict(_STATE)

    run_id = _new_run(target)
    with _STATE_LOCK:
        _STATE.update({
            "run_id": run_id,
            "status": "running",
            "started_at": _now(),
            "pages_fetched": 0,
            "pages_failed": 0,
            "rows_seen": 0,
            "rows_new": 0,
            "target": target,
            "error": "",
        })

    thread = threading.Thread(target=_harvest_worker, args=(run_id, target), daemon=True)
    thread.start()
    return state()


def stop() -> dict:
    """Request a running harvest to stop at the next page boundary."""
    with _STATE_LOCK:
        _STATE["stop_requested"] = True
    return state()


def harvest(target: int = HARVEST_TARGET, workers: int = DEFAULT_WORKERS) -> dict:
    """Synchronous harvest for the scheduler. Runs in-process."""
    run_id = _new_run(target)
    with _STATE_LOCK:
        _STATE.update({
            "run_id": run_id,
            "status": "running",
            "started_at": _now(),
            "pages_fetched": 0,
            "pages_failed": 0,
            "rows_seen": 0,
            "rows_new": 0,
            "target": target,
            "error": "",
        })

    try:
        _harvest_worker(run_id, target)
    except Exception as exc:
        _complete_run(run_id, "failed", str(exc))
        with _STATE_LOCK:
            _STATE.update({"status": "failed", "error": str(exc)})

    return state()


def _new_run(target: int) -> int:
    conn = db.get_conn()
    try:
        cursor = conn.execute(
            """INSERT INTO facebook_harvest_runs
               (started_at, status, target)
               VALUES (?, 'running', ?)""",
            (_now(), target)
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _complete_run(run_id: int, status: str, error: str = "") -> None:
    conn = db.get_conn()
    try:
        conn.execute(
            """UPDATE facebook_harvest_runs
               SET finished_at = ?, status = ?, error = ?
               WHERE id = ?""",
            (_now(), status, error, run_id)
        )
        conn.commit()
    finally:
        conn.close()


def _harvest_worker(run_id: int, target: int) -> None:
    """Background worker: fetch Facebook Marketplace search pages."""
    from playwright.sync_api import sync_playwright

    conn = db.get_conn()
    try:
        _ensure_schema(conn)

        cities = _load_cities(conn)
        if not cities:
            _complete_run(run_id, "failed", "No cities configured")
            with _STATE_LOCK:
                _STATE.update({"status": "failed", "error": "No cities configured"})
            return

        seen_ids: set[str] = set()
        rows_new = 0
        pages_fetched = 0
        pages_failed = 0
        empty_waves = 0
        error_waves = 0

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context_options = {"locale": "he-IL", "viewport": {"width": 1440, "height": 1000}}
            if STORAGE_STATE_PATH.exists():
                context_options["storage_state"] = str(STORAGE_STATE_PATH)
            context = browser.new_context(**context_options)

            try:
                for city_name, city_id in cities:
                    if rows_new >= target:
                        break
                    with _STATE_LOCK:
                        if _STATE.get("stop_requested"):
                            break

                    url = _build_search_url(city_id, DEFAULT_QUERY, DEFAULT_MIN_PRICE)
                    page = context.new_page()

                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_timeout(2000)

                        for wave in range(EXHAUSTION_WAVES):
                            if rows_new >= target:
                                break

                            cards = _extract_cards(page, 50)
                            if not cards:
                                empty_waves += 1
                                if empty_waves >= EXHAUSTION_WAVES:
                                    break
                                continue

                            empty_waves = 0
                            new_in_wave = 0
                            new_listings = []

                            for card in cards:
                                listing = _normalize_card(card, DEFAULT_QUERY)
                                if listing is None:
                                    continue

                                if listing["listing_id"] in seen_ids:
                                    continue
                                seen_ids.add(listing["listing_id"])

                                inserted, price_changed = _upsert_listing(conn, listing)
                                if inserted:
                                    rows_new += 1
                                    new_in_wave += 1
                                    new_listings.append(listing)

                            if new_listings:
                                for new_listing in new_listings:
                                    try:
                                        _score_and_update(conn, new_listing)
                                    except Exception as exc:
                                        print(f"scoring error for {new_listing.get('listing_id')}: {exc}")

                            conn.commit()
                            pages_fetched += 1

                            with _STATE_LOCK:
                                _STATE.update({
                                    "pages_fetched": pages_fetched,
                                    "rows_new": rows_new,
                                })

                            if new_in_wave == 0:
                                error_waves += 1
                                if error_waves >= FAILURE_WAVES:
                                    break
                            else:
                                error_waves = 0

                            page.wait_for_timeout(1000)

                    except Exception as exc:
                        pages_failed += 1
                        with _STATE_LOCK:
                            _STATE["pages_failed"] = pages_failed
                    finally:
                        page.close()

            finally:
                context.close()
                browser.close()

        status = "complete" if rows_new >= target else "exhausted"
        with _STATE_LOCK:
            if _STATE.get("stop_requested"):
                status = "stopped"
        _complete_run(run_id, status)

        with _STATE_LOCK:
            _STATE.update({
                "status": status,
                "finished_at": _now(),
                "rows_seen": len(seen_ids),
                "rows_new": rows_new,
                "pages_fetched": pages_fetched,
                "pages_failed": pages_failed,
            })

    except Exception as exc:
        _complete_run(run_id, "failed", str(exc))
        with _STATE_LOCK:
            _STATE.update({"status": "failed", "error": str(exc)})
    finally:
        conn.close()


def _load_cities(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Load configured cities from the DB, falling back to defaults."""
    try:
        rows = conn.execute(
            "SELECT city_name, city_id FROM facebook_cities WHERE enabled = 1"
        ).fetchall()
        if rows:
            return [(row["city_name"], row["city_id"]) for row in rows]
    except sqlite3.OperationalError:
        pass

    defaults = [
        ("תל אביב-יפו", "telaviv"),
        ("ירושלים", "jerusalem"),
        ("חיפה", "110619208966868"),
        ("ראשון לציון", "109394745753748"),
        ("פתח תקווה", "104047936296918"),
        ("אשדוד", "105695102798184"),
        ("נתניה", "112017418824019"),
        ("באר שבע", "110944518930851"),
        ("חולון", "114283675255321"),
        ("רמת גן", "112604772085346"),
    ]
    now = _now()
    for name, city_id in defaults:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO facebook_cities (city_name, city_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, city_id, now, now)
            )
        except sqlite3.OperationalError:
            pass
    conn.commit()
    return defaults


def _fb_cities_schema() -> str:
    now = _now()
    return f"""
    CREATE TABLE IF NOT EXISTS facebook_cities (
        city_name   TEXT PRIMARY KEY,
        city_id     TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    INSERT OR IGNORE INTO facebook_cities (city_name, city_id, created_at, updated_at)
    VALUES ('תל אביב-יפו', 'telaviv', '{now}', '{now}');
    """
