"""
PlanWatch - market & price client (מחירים ושוק)
================================================
Price and market signals, so a plan can be judged and not only listed.

Four live sources, all probed 2026-07-28:

* **CBS apartment price index** (`api.cbs.gov.il`) - the official index.
  National series back to 1994 (quarterly to 1987), six districts monthly
  since 2017-10, plus a new-apartments series. `robots.txt` = 200/open, and
  this is a documented public API.
* **מחיר למשתכן / דירה בהנחה** (data.gov.il, 2,352 rows) - `PriceForMeter`
  per project, with locality, neighbourhood, units, and *demand*
  (subscribers vs units). 112 localities.
* **מלאי תכנוני למגורים** (רמ"י, 1,112 rows) - joins onto `plans.pl_number`;
  carries the planning stage and units-potential-for-marketing.
* **עלויות פיתוח בבניה העירונית** (רמ"י, 1,427 rows) - development levies
  per project; median ≈ 139,474 ₪ per unit.

Sources deliberately NOT used
-----------------------------
* **yad2.co.il** - `robots.txt` says `Disallow: /api/`, and the new-site rules
  disallow the filtered `/realestate/forsale?...` URLs one by one. The listings
  API on `gw.yad2.co.il` is the same product behind a host with no robots.txt
  of its own. Off limits.
* **madlan.co.il** - `robots.txt` names and disallows *every* data endpoint
  individually: `/api/`, `/api2/`, `/search/`, `/homes/`, `/property/`,
  `/sold`, `/feed`, `getPropertyInfo`, `getMarkerRecord`, `getHeatmapValue`.
  There is no ambiguity to interpret. Off limits.
* **nadlan.gov.il** (רשות המסים deals) - `robots.txt` *allows* crawling, but
  every `Nadlan.REST/Main/*` path returns the SPA shell: the REST API is
  reCAPTCHA-gated (the site key sits in a comment in the served HTML).
  Re-verified here for the third time; not circumvented.
* **"דירות למכירה ללא הגרלה"** (data.gov.il resource
  `ea93b3c9-…`) - published but EMPTY: `total=0`, one `_id` field. Nothing to
  import. Left in the notes so nobody re-discovers it as a lead.

For actual for-sale listings use `listings` (below): an empty adapter table
fed from a feed you are licensed for. See `import_listings_csv`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from legacy.tools.http_client import build_session

CKAN = "https://data.gov.il/api/3/action/datastore_search"
CBS = "https://api.cbs.gov.il/index/data/price"

RES_LOTTERY = "7c8255d0-49ef-49db-8904-4cf917586031"
RES_INVENTORY = "99aad98f-2b54-4eea-834d-650b56389bf3"
RES_DEVCOST = "bf164a03-55c7-4bea-8740-66ce60a51a2c"
#: Published but empty (total=0). Kept for the record - do not import.
RES_FORSALE_EMPTY = "ea93b3c9-15e2-4b74-a632-097ee53737e4"

PAGE = 1000

#: CBS series -> (label, scope). `district` values match `plans.district_name`
#: exactly, which is what makes the join work without a lookup table.
CBS_SERIES = {
    40010: ("מחירי דירות - כללי", "national"),
    70000: ("מדד מחירי דירות חדשות", "national_new"),
    60000: ("ירושלים", "district"),
    60100: ("צפון", "district"),
    60200: ("חיפה", "district"),
    60300: ("מרכז", "district"),
    60400: ("תל-אביב", "district"),
    60500: ("דרום", "district"),
    # The two indices that make prices from different years comparable. The
    # eight above measure what homes cost; these measure what money and
    # building cost, and without them "prices rose 8%" cannot be told apart
    # from "everything rose 8%". `construction_input` is also the only series
    # here about the *supply* side - when it climbs faster than the price
    # index, new building stops penciling and the existing stock is what is
    # left to buy.
    200010: ("מדד תשומה בבנייה למגורים", "construction_input"),
    130010: ("מדד המחירים לצרכן", "cpi"),
}
#: plans.district_name -> CBS series id. Verified against the 7 distinct values
#: actually present in `plans` (the 7th, "מטה", has no district index).
DISTRICT_TO_CBS = {
    "ירושלים": 60000, "צפון": 60100, "חיפה": 60200,
    "מרכז": 60300, "תל-אביב": 60400, "דרום": 60500,
}

TABLES = """
CREATE TABLE IF NOT EXISTS cbs_price_index (
    series_id   INTEGER NOT NULL,
    series_name TEXT,
    scope       TEXT,          -- national | national_new | district
    period_type TEXT NOT NULL, -- month | quarter
    period      TEXT NOT NULL, -- YYYY-MM (month) or YYYY-Qn (quarter)
    year        INTEGER,
    value       REAL,          -- index level on `base_desc`
    pct_change  REAL,          -- vs previous period
    pct_year    REAL,          -- vs same period last year
    base_desc   TEXT,
    PRIMARY KEY (series_id, period_type, period)
);

CREATE TABLE IF NOT EXISTS ml_projects (
    lottery_id     TEXT PRIMARY KEY,
    project_id     TEXT,
    project_name   TEXT,
    locality       TEXT,
    locality_code  TEXT,
    neighborhood   TEXT,
    price_per_m2   REAL,       -- NULL when the source had 0 / '-'
    units          INTEGER,
    subscribers    INTEGER,
    winners        INTEGER,
    status         TEXT,
    permit_status  TEXT,
    provider       TEXT,
    lottery_date   TEXT,
    marketing      TEXT
);

CREATE TABLE IF NOT EXISTS rami_inventory (
    plan_number     TEXT,
    plan_name       TEXT,
    stage           TEXT,
    promoter        TEXT,
    locality        TEXT,
    locality_code   TEXT,
    units_potential INTEGER,
    threshold_date  TEXT,
    deposit_date    TEXT,
    approve_date    TEXT,
    rami_url        TEXT,
    mavat_url       TEXT,
    poly_key        TEXT
);

CREATE TABLE IF NOT EXISTS dev_costs (
    project_id       TEXT,
    project_name     TEXT,
    district         TEXT,
    locality         TEXT,
    locality_code    TEXT,
    site_name        TEXT,
    units            INTEGER,
    status           TEXT,
    tender_index     TEXT,
    develop_pay      REAL,
    old_by_new_cost  REAL,     -- meaning unconfirmed at source; raw only
    mosdot_dev_pay   REAL,
    tender_dev_pay   REAL
);

/*
 * For-sale listings. EMPTY BY DESIGN.
 *
 * Yad2 and Madlan both forbid automated access in robots.txt (see the module
 * docstring), so PlanWatch does not fetch them. This table is the seam: point
 * `import_listings_csv` at an export from a feed you are licensed for - a Yad2
 * or Madlan partner feed, a broker's MLS dump, your own CRM - and every
 * scoring query in opportunity.py starts using it with no other change.
 *
 * `price_per_m2` is stored, not computed on read, so a feed that publishes it
 * directly is trusted over price/area arithmetic on a rounded area.
 */
CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,        -- e.g. 'yad2-partner-feed', 'broker-csv'
    external_id   TEXT,
    url           TEXT,
    gush          TEXT,
    helka         TEXT,
    lat           REAL,
    lon           REAL,
    address       TEXT,
    locality      TEXT,
    neighborhood  TEXT,
    property_type TEXT,
    rooms         REAL,
    floor         INTEGER,
    total_floors  INTEGER,
    area_m2       REAL,
    year_built    INTEGER,
    price         REAL,
    price_per_m2  REAL,
    listed_at     TEXT,
    fetched_at    TEXT,
    raw_json      TEXT,
    UNIQUE (source, external_id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_cbs_scope     ON cbs_price_index (scope, period_type, period);
CREATE INDEX IF NOT EXISTS idx_ml_locality   ON ml_projects (locality);
CREATE INDEX IF NOT EXISTS idx_rami_plan     ON rami_inventory (plan_number);
CREATE INDEX IF NOT EXISTS idx_rami_locality ON rami_inventory (locality);
CREATE INDEX IF NOT EXISTS idx_dev_locality  ON dev_costs (locality);
CREATE INDEX IF NOT EXISTS idx_listings_loc  ON listings (locality);
CREATE INDEX IF NOT EXISTS idx_listings_gh   ON listings (gush, helka);
CREATE INDEX IF NOT EXISTS idx_listings_bbox ON listings (lat, lon);
"""

#: CBS answers HTTP 500 `{"Message":"Error: Price Data"}` when a request asks
#: for more periods than the series actually holds. Series 70000 starts
#: 2017-10 (~105 months), so full-history and `last=120` both fail while
#: `last=60` succeeds - it is a deterministic "window too big", not a blip.
#: Walk this ladder and keep the first window that answers.
_CBS_WINDOWS = (None, 240, 96, 60, 36, 12)

_session = None
_cbs_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def _cbs_sess():
    """
    CBS gets its own session with almost no retrying. The shared session
    retries 5x with backoff 1.5 on 500, which turns each expected-and-handled
    CBS 500 into ~45 s of pointless sleeping while walking _CBS_WINDOWS.
    """
    global _cbs_session
    if _cbs_session is None:
        _cbs_session = build_session(total_retries=1, backoff_factor=0.5)
    return _cbs_session


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


# ------------------------------------------------------------------ parsing

def _num(value):
    """
    Source numbers arrive as '9,242.00', '-', '' or a real float.
    Returns None for anything that is not a usable number, and for 0 in the
    price case - a 0 ₪/m² project is missing data, not a free apartment.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int(value):
    num = _num(value)
    return int(num) if num is not None else None


def _txt(value):
    """The RAMI/מחיר-למשתכן feeds pad with spaces AND non-breaking spaces."""
    if value is None:
        return None
    text = " ".join(str(value).replace("\xa0", " ").split())
    return text or None


_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _date(value):
    """'19/12/2017' -> '2017-12-19'. Already-ISO and '-' pass through/vanish."""
    text = _txt(value)
    if not text or text == "-":
        return None
    match = _DMY.match(text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return text[:10]


def _ckan_all(resource_id, timeout=(20, 120), should_stop=None):
    """Page a CKAN datastore resource dry."""
    offset = 0
    while True:
        if should_stop and should_stop():
            return
        payload = _sess().get(CKAN, params={
            "resource_id": resource_id, "limit": PAGE, "offset": offset,
        }, timeout=timeout).json()
        if not payload.get("success"):
            raise RuntimeError(f"CKAN search failed for {resource_id} at {offset}")
        records = payload["result"]["records"]
        if not records:
            return
        yield from records
        offset += len(records)
        if len(records) < PAGE:
            return


def _cbs_pages(series_id, timeout):
    """
    Fetch every page of one CBS series.

    Returns `(pages, window)`, or `(None, None)` when no window in _CBS_WINDOWS
    works - a series that is simply unavailable today must not abort the import
    of the other seven.
    """
    for window in _CBS_WINDOWS:
        params = {"id": series_id, "format": "json", "download": "false"}
        if window:
            # `last` and `Page`/`PageSize` do not mix on this API: sending both
            # 500s even for a window the server accepts on its own.
            params["last"] = window
        else:
            params.update(Page=1, PageSize=100)

        first = _cbs_sess().get(CBS, params=params, timeout=timeout)
        if first.status_code == 500:
            continue
        first.raise_for_status()
        payload = first.json()
        pages = [payload]
        last_page = (payload.get("paging") or {}).get("last_page") or 1
        for page in range(2, last_page + 1):
            resp = _cbs_sess().get(CBS, params=dict(params, Page=page),
                                   timeout=timeout)
            resp.raise_for_status()
            pages.append(resp.json())
        return pages, window
    return None, None


# ------------------------------------------------------------------- imports

def import_cbs(conn, progress=None, should_stop=None, timeout=(20, 120)) -> int:
    """
    Pull every CBS series in CBS_SERIES, monthly and quarterly.

    Upsert rather than staged-swap: CBS revises recent periods in place, and
    the series are small (a few thousand rows total), so there is nothing to
    gain from a swap and a partial failure should not empty the table.
    """
    ensure_schema(conn)
    total = 0
    for series_id, (label, scope) in CBS_SERIES.items():
        if should_stop and should_stop():
            break
        pages, used_window = _cbs_pages(series_id, timeout)
        if pages is None:
            if progress:
                progress(f"cbs:{series_id} unavailable", total)
            continue
        rows = []
        for payload in pages:
            for period_type in ("month", "quarter"):
                for block in (payload.get(period_type) or []):
                    for entry in block.get("date") or []:
                        year = entry.get("year")
                        if period_type == "month":
                            period = f"{year}-{int(entry['month']):02d}"
                        else:
                            period = f"{year}-Q{entry.get('quarter') or entry.get('month')}"
                        base = entry.get("currBase") or {}
                        rows.append((
                            series_id, label, scope, period_type, period, year,
                            _num(base.get("value")), _num(entry.get("percent")),
                            _num(entry.get("percentYear")), base.get("baseDesc"),
                        ))

        conn.executemany(
            """INSERT INTO cbs_price_index
                 (series_id, series_name, scope, period_type, period, year,
                  value, pct_change, pct_year, base_desc)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (series_id, period_type, period) DO UPDATE SET
                 value=excluded.value, pct_change=excluded.pct_change,
                 pct_year=excluded.pct_year, base_desc=excluded.base_desc""",
            rows)
        conn.commit()
        total += len(rows)
        if progress:
            progress("cbs", total)
    return total


def _swap_import(conn, table, columns, rows, progress_key, progress):
    """Write to a staging table and swap, so a mid-import failure changes nothing."""
    staging = f"{table}_new"
    conn.execute(f"DROP TABLE IF EXISTS {staging}")
    conn.execute(f"CREATE TABLE {staging} AS SELECT * FROM {table} WHERE 0")
    marks = ",".join("?" for _ in columns)
    conn.executemany(
        f"INSERT INTO {staging} ({', '.join(columns)}) VALUES ({marks})", rows)
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {staging} RENAME TO {table}")
    conn.executescript(TABLES)      # recreate the dropped table def if needed
    conn.executescript(INDEXES)     # and its indexes
    conn.commit()
    if progress:
        progress(progress_key, len(rows))
    return len(rows)


def import_lotteries(conn, progress=None, should_stop=None, timeout=(20, 120)) -> int:
    """מחיר למשתכן / דירה בהנחה - the ₪/m² benchmark and the demand signal."""
    ensure_schema(conn)
    cols = ("lottery_id", "project_id", "project_name", "locality",
            "locality_code", "neighborhood", "price_per_m2", "units",
            "subscribers", "winners", "status", "permit_status", "provider",
            "lottery_date", "marketing")
    rows, seen = [], set()
    for rec in _ckan_all(RES_LOTTERY, timeout, should_stop):
        key = _txt(rec.get("LotteryId")) or f"_{len(rows)}"
        if key in seen:          # the feed repeats a handful of lottery ids
            continue
        seen.add(key)
        price = _num(rec.get("PriceForMeter"))
        rows.append((
            key, _txt(rec.get("ProjectId")), _txt(rec.get("ProjectName")),
            _txt(rec.get("LamasName")), _txt(rec.get("LamasCode")),
            _txt(rec.get("Neighborhood")),
            price if price else None,           # 0 ₪/m² = missing, not free
            _int(rec.get("LotteryHousingUnits")),
            _int(rec.get("Subscribers")), _int(rec.get("Winners")),
            _txt(rec.get("ProjectStatus")), _txt(rec.get("ConstructionPermitName")),
            _txt(rec.get("ProviderName")), _date(rec.get("LotteryExecutionDate")),
            _txt(rec.get("MarketingMethodDesc")),
        ))
    if should_stop and should_stop():
        return 0
    return _swap_import(conn, "ml_projects", cols, rows, "lotteries", progress)


def import_inventory(conn, progress=None, should_stop=None, timeout=(20, 120)) -> int:
    """מלאי תכנוני למגורים (רמ"י) - joins onto plans.pl_number."""
    ensure_schema(conn)
    cols = ("plan_number", "plan_name", "stage", "promoter", "locality",
            "locality_code", "units_potential", "threshold_date",
            "deposit_date", "approve_date", "rami_url", "mavat_url", "poly_key")
    rows = [(
        _txt(r.get("מספר תוכנית")), _txt(r.get("שם תוכנית")),
        _txt(r.get("שלב תכנוני")), _txt(r.get("יזם תכנון")),
        _txt(r.get("יישוב")), _txt(r.get("סמל יישוב")),
        _int(r.get("יחד פוטנציאל לשיווק")),
        _date(r.get("תאריך קיום תנאי סף")),
        _date(r.get("תאריך פרסום להפקדה ברשומות")),
        _date(r.get("תאריך פרסום לאישור ברשומות")),
        _txt(r.get("קישור לאתר רשות מקרקעי ישראל")),
        _txt(r.get("קישור לאתר מנהל תכנון")),
        _txt(r.get("מפתח לפוליגון תכנית")),
    ) for r in _ckan_all(RES_INVENTORY, timeout, should_stop)]
    if should_stop and should_stop():
        return 0
    return _swap_import(conn, "rami_inventory", cols, rows, "inventory", progress)


def import_dev_costs(conn, progress=None, should_stop=None, timeout=(20, 120)) -> int:
    """עלויות פיתוח - development levies per project."""
    ensure_schema(conn)
    cols = ("project_id", "project_name", "district", "locality",
            "locality_code", "site_name", "units", "status", "tender_index",
            "develop_pay", "old_by_new_cost", "mosdot_dev_pay", "tender_dev_pay")
    rows = [(
        _txt(r.get("ProjectID")), _txt(r.get("ProjectName")),
        _txt(r.get("MahozName")), _txt(r.get("LamasName")),
        _txt(r.get("LamasCode")), _txt(r.get("AtarName")),
        _int(r.get("LivingUnits")), _txt(r.get("StatusDescription")),
        _txt(r.get("TenderIndexDate")), _num(r.get("DevelopPay")),
        _num(r.get("OldByNewCost")), _num(r.get("MosdotDevPay")),
        _num(r.get("TenderDevPay")),
    ) for r in _ckan_all(RES_DEVCOST, timeout, should_stop)]
    if should_stop and should_stop():
        return 0
    return _swap_import(conn, "dev_costs", cols, rows, "devcost", progress)


IMPORTERS = {
    "cbs": import_cbs,
    "lotteries": import_lotteries,
    "inventory": import_inventory,
    "devcost": import_dev_costs,
}


def import_all(conn, progress=None, should_stop=None, timeout=(20, 120)) -> int:
    total = 0
    for key, fn in IMPORTERS.items():
        if should_stop and should_stop():
            break
        total += fn(conn, progress=progress, should_stop=should_stop,
                    timeout=timeout)
    return total


# ------------------------------------------------- licensed listings adapter

LISTING_FIELDS = (
    "source", "external_id", "url", "gush", "helka", "lat", "lon", "address",
    "locality", "neighborhood", "property_type", "rooms", "floor",
    "total_floors", "area_m2", "year_built", "price", "price_per_m2",
    "listed_at",
)
_LISTING_NUM = {"lat", "lon", "rooms", "area_m2", "price", "price_per_m2"}
_LISTING_INT = {"floor", "total_floors", "year_built"}
LISTING_BATCH = 2_000


def _listing_external_id(clean: dict, raw: dict) -> str:
    """Return a stable feed key even when a licensed export lacks one.

    SQLite permits multiple NULLs in a unique key.  Leaving ``external_id``
    empty therefore made re-importing a broker CSV silently duplicate every
    row.  Prefer the provider's ID; otherwise hash a canonical representation
    of the row and use that deterministic value as the adapter key.
    """
    value = _txt(clean.get("external_id"))
    if value:
        return value
    identity = {
        "url": clean.get("url"), "gush": clean.get("gush"),
        "helka": clean.get("helka"), "address": clean.get("address"),
        "locality": clean.get("locality"), "price": clean.get("price"),
        "area_m2": clean.get("area_m2"), "listed_at": clean.get("listed_at"),
        "raw": raw,
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return "derived:" + hashlib.sha256(encoded).hexdigest()


def import_listings_records(conn, records, source=None) -> int:
    """Upsert authorised listing dictionaries from a CRM/MLS/feed payload.

    ``source`` is authoritative when supplied.  In particular, an inbound
    partner API request must not be able to split its rows across arbitrary
    source namespaces by including a ``source`` member in individual rows.
    """
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    cols = LISTING_FIELDS + ("fetched_at", "raw_json")
    marks = ",".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in
                        ("source", "external_id"))
    sql = f"""INSERT INTO listings ({', '.join(cols)}) VALUES ({marks})
               ON CONFLICT (source, external_id) DO UPDATE SET {updates}"""
    total, batch = 0, []
    for rec in records:
        if not isinstance(rec, dict):
            raise ValueError("each listing must be a JSON object")
        clean = {}
        for key, value in rec.items():
            field = (key or "").strip()
            if field in LISTING_FIELDS:
                if field in _LISTING_NUM:
                    clean[field] = _num(value)
                elif field in _LISTING_INT:
                    clean[field] = _int(value)
                else:
                    clean[field] = _txt(value)
        clean["source"] = source or clean.get("source") or "feed"
        clean["external_id"] = _listing_external_id(clean, rec)
        if clean.get("price_per_m2") is None and clean.get("price") and clean.get("area_m2"):
            clean["price_per_m2"] = round(clean["price"] / clean["area_m2"], 1)
        batch.append(tuple(clean.get(f) for f in LISTING_FIELDS)
                     + (now, json.dumps(rec, ensure_ascii=False, default=str)))
        if len(batch) >= LISTING_BATCH:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    return total


def import_listings_csv(conn, path, source=None, encoding="utf-8-sig") -> int:
    """
    Load for-sale listings from a CSV you are licensed to hold.

    Header names are matched against LISTING_FIELDS; unknown columns are kept
    verbatim in `raw_json` rather than dropped, because a feed's extra columns
    are usually the interesting ones. `price_per_m2` is derived from
    price/area_m2 only when the feed did not supply it.

    Re-importing the same (source, external_id) updates in place.
    """
    with open(path, newline="", encoding=encoding) as handle:
        return import_listings_records(conn, csv.DictReader(handle), source or "csv")


def import_listings_json(conn, path, source=None, encoding="utf-8") -> int:
    """Load a licensed CRM/API JSON export without a supplier-specific adapter.

    The file may be a bare array of listing objects or an inbound-API-shaped
    object, ``{"source": "…", "listings": [...]}``.  A CLI-supplied source
    always takes precedence over the file metadata.
    """
    with open(path, encoding=encoding) as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        records, payload_source = payload, None
    elif isinstance(payload, dict):
        records, payload_source = payload.get("listings"), payload.get("source")
    else:
        raise ValueError("listing JSON must be an array or an object with listings")
    if not isinstance(records, list):
        raise ValueError("listing JSON field 'listings' must be an array")
    return import_listings_records(conn, records, source or _txt(payload_source) or "json")


# ------------------------------------------------------------------ readers

def counts(conn) -> dict:
    out = {}
    for table in ("cbs_price_index", "ml_projects", "rami_inventory",
                  "dev_costs", "listings"):
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        out[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] if exists else 0

    # `listings` here is the local licensed-feed table, but /api/deals serves
    # the read-only scraper bridge whenever that DB has rows. Mirror that same
    # routing rule rather than "fall back only if local is empty": the two
    # differ the moment a licensed CSV feed is loaded alongside a populated
    # scraper DB - which the listings empty-state card explicitly invites -
    # and the KPI would then count one source while the table under it renders
    # the other. Count what the UI actually shows.
    try:
        from ingestion.feeds import external_listings_view as external
        state = external.status()
        if state.get("available") and state.get("records"):
            out["listings"] = state["records"]
    except Exception:
        pass

    # Concluded transactions live only in the scraper DB (official CC-BY
    # dataset); surface them so the UI can show both sides of the market.
    # The residential figure is the one reported, because the transactions
    # view defaults to residential_only=1 - showing the raw table count here
    # put 285,535 in the KPI above a table that said 285,444.
    try:
        from ingestion.feeds import external_listings_view as external
        status = external.transactions_status()
        out["transactions"] = (status.get("records_residential")
                               or status.get("records", 0) or 0)
    except Exception:
        out["transactions"] = 0
    return out


def district_trend(conn, district) -> dict | None:
    """Latest CBS index level and 12-month change for one district."""
    series_id = DISTRICT_TO_CBS.get((district or "").strip())
    if series_id is None:
        return None
    row = conn.execute(
        """SELECT period, value, pct_change, pct_year, base_desc
           FROM cbs_price_index
           WHERE series_id=? AND period_type='month' AND value IS NOT NULL
           ORDER BY period DESC LIMIT 1""", (series_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["district"] = district
    out["series_id"] = series_id
    # pct_year is often NULL on the district series; derive it when it is.
    # Chained from the monthly changes, not divided out of two levels - CBS
    # rebases these series and a ratio across a rebase measures the base
    # change. See `chained_ratio`.
    out["pct_year"] = _year_change(conn, series_id, out["period"], out["pct_year"])
    return out


def national_trend(conn) -> dict | None:
    row = conn.execute(
        """SELECT period, value, pct_change, pct_year, base_desc
           FROM cbs_price_index
           WHERE series_id=40010 AND period_type='month' AND value IS NOT NULL
           ORDER BY period DESC LIMIT 1""").fetchone()
    return dict(row) if row else None


#: The two cost series, by the scope they were imported under.
COST_SERIES = {"construction_input": 200010, "cpi": 130010}


def chained_ratio(conn, series_id, from_period: str, to_period: str | None = None):
    """Cumulative price ratio between two months, immune to rebasing.

    **Levels from this table cannot be divided.** CBS rebases: series 130010
    carries 17 different `base_desc` values, and 200010 nine, each one a fresh
    100. Dividing a 2026 level on the "2024 ממוצע" base by a 2024 level on the
    "2022 ממוצע" base measured the base change, not inflation - it reported the
    last two years of Israeli CPI as a 3.7% *fall*.

    The monthly `pct_change` is base-independent, present on every row but the
    first of each series, and chains correctly straight through a rebase. So
    the ratio is the product of the monthly changes strictly after
    `from_period`, which is what a rebased index is designed to be read as.

    Returns None when any month in the span is missing, rather than skipping
    it: a gap silently dropped understates the total by exactly that month.
    """
    if not from_period:
        return None
    start = str(from_period)[:7]
    end = str(to_period)[:7] if to_period else None
    if end is None:
        row = conn.execute(
            "SELECT MAX(period) p FROM cbs_price_index WHERE series_id=? "
            "AND period_type='month' AND value IS NOT NULL",
            (series_id,)).fetchone()
        end = row["p"] if row else None
    if not end or end <= start:
        return 1.0 if end == start else None

    rows = conn.execute(
        "SELECT period, pct_change FROM cbs_price_index WHERE series_id=? "
        "AND period_type='month' AND period > ? AND period <= ? ORDER BY period",
        (series_id, start, end)).fetchall()
    # Months between the two ends, inclusive of the far end only.
    y0, m0 = (int(x) for x in start.split("-"))
    y1, m1 = (int(x) for x in end.split("-"))
    expected = (y1 - y0) * 12 + (m1 - m0)
    if len(rows) != expected:
        return None
    ratio = 1.0
    for row in rows:
        if row["pct_change"] is None:
            return None
        ratio *= 1 + float(row["pct_change"]) / 100.0
    return ratio


def _year_change(conn, series_id, period, published):
    """12-month change: the published figure, else chained from the monthlies."""
    if published is not None:
        return published
    ratio = chained_ratio(conn, series_id,
                          f"{int(str(period)[:4]) - 1}-{str(period)[5:7]}", period)
    return None if ratio is None else round((ratio - 1) * 100, 1)


def _series_trend(conn, series_id) -> dict | None:
    """Latest level plus the 12-month change, derived rather than trusted.

    CBS fills `pct_year` on some series and leaves it NULL on others. Where it
    is empty the change is *chained from the monthly figures* rather than taken
    as a ratio of two levels - see `chained_ratio` for the rebasing that makes
    the ratio wrong.
    """
    row = conn.execute(
        """SELECT period, value, pct_change, pct_year, base_desc
             FROM cbs_price_index
            WHERE series_id=? AND period_type='month' AND value IS NOT NULL
            ORDER BY period DESC LIMIT 1""", (series_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["series_id"] = series_id
    if out["period"]:
        out["pct_year"] = _year_change(conn, series_id, out["period"],
                                       out["pct_year"])
    return out


def cost_trends(conn) -> dict:
    """Building costs and consumer prices, next to home prices.

    `real_pct_year` is the part that is actually new information: home prices
    up 5.4% while consumer prices are up 1.6% is a 3.8% real move, and the same
    5.4% against 5.4% inflation is no move at all. The nominal figure alone has
    been read as growth in every one of those cases.

    `cost_gap_pct_year` compares home prices against the cost of *building*
    them. Homes running ahead of construction costs widens the developer's
    margin and pulls new supply in; construction costs running ahead closes it
    and the pipeline thins, which is a statement about supply two years out.
    """
    out = {}
    for label, series_id in COST_SERIES.items():
        trend = _series_trend(conn, series_id)
        if trend:
            out[label] = trend
    homes = national_trend(conn)
    if homes:
        out["homes"] = homes
        home_yr = homes.get("pct_year")
        cpi_yr = (out.get("cpi") or {}).get("pct_year")
        build_yr = (out.get("construction_input") or {}).get("pct_year")
        if home_yr is not None and cpi_yr is not None:
            out["real_pct_year"] = round(home_yr - cpi_yr, 1)
        if home_yr is not None and build_yr is not None:
            out["cost_gap_pct_year"] = round(home_yr - build_yr, 1)
    return out


def deflator(conn, from_period: str, scope: str = "cpi") -> float | None:
    """Multiplier that restates a `YYYY-MM` figure in today's money.

    Used for price history: a 2,300,000 ₪ asking price from two years ago is
    not comparable to today's 2,300,000 ₪, and treating the pair as "no change"
    is how a real 3% cut gets recorded as a seller holding firm.

    Returns None rather than 1.0 when the span cannot be measured - an
    unavailable adjustment must not be indistinguishable from a zero one.
    """
    series_id = COST_SERIES.get(scope)
    if series_id is None:
        return None
    ratio = chained_ratio(conn, series_id, from_period)
    return None if ratio is None else round(ratio, 4)


def locality_price(conn, locality) -> dict | None:
    """
    ₪/m² benchmark for a locality, from מחיר למשתכן projects.

    CAVEAT, and it matters: these are *subsidised* prices (דירה בהנחה), so the
    level sits systematically BELOW the free market. Use it as a floor and as a
    relative ranking between localities - never as a market valuation.
    """
    name = (locality or "").strip()
    if not name:
        return None
    row = conn.execute(
        """SELECT COUNT(*) n, AVG(price_per_m2) avg_m2,
                  MIN(price_per_m2) min_m2, MAX(price_per_m2) max_m2,
                  SUM(units) units, SUM(subscribers) subscribers
           FROM ml_projects
           WHERE price_per_m2 IS NOT NULL AND locality LIKE ?""",
        (f"%{name}%",)).fetchone()
    if not row or not row["n"]:
        return None
    out = dict(row)
    out["locality"] = name
    out["avg_m2"] = round(out["avg_m2"], 0) if out["avg_m2"] else None
    # median: more honest than the mean on 1-3 projects with an outlier
    values = [r[0] for r in conn.execute(
        """SELECT price_per_m2 FROM ml_projects
           WHERE price_per_m2 IS NOT NULL AND locality LIKE ?
           ORDER BY price_per_m2""", (f"%{name}%",))]
    mid = len(values) // 2
    out["median_m2"] = round(
        values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2, 0)
    # demand pressure: subscribers per offered unit
    if out["units"]:
        out["demand_ratio"] = round((out["subscribers"] or 0) / out["units"], 1)
    else:
        out["demand_ratio"] = None
    out["subsidised"] = True     # never let a caller forget
    return out


def national_price_percentiles(conn) -> dict:
    """Locality ₪/m² spread, so 'cheap' and 'dear' have national reference points."""
    values = [r[0] for r in conn.execute(
        """SELECT AVG(price_per_m2) FROM ml_projects
           WHERE price_per_m2 IS NOT NULL AND locality IS NOT NULL
           GROUP BY locality ORDER BY 1""")]
    if not values:
        return {}

    def pct(fraction):
        return round(values[min(len(values) - 1, int(len(values) * fraction))], 0)

    return {"localities": len(values), "p10": pct(0.10), "p25": pct(0.25),
            "p50": pct(0.50), "p75": pct(0.75), "p90": pct(0.90)}


def dev_cost_per_unit(conn, locality) -> dict | None:
    """Median development levy per housing unit in a locality."""
    name = (locality or "").strip()
    if not name:
        return None
    values = [r[0] for r in conn.execute(
        """SELECT develop_pay * 1.0 / units FROM dev_costs
           WHERE units > 0 AND develop_pay > 0 AND locality LIKE ?
           ORDER BY 1""", (f"%{name}%",))]
    if not values:
        return None
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return {"locality": name, "projects": len(values),
            "median_per_unit": round(median, 0),
            "min_per_unit": round(values[0], 0),
            "max_per_unit": round(values[-1], 0)}


def inventory_for_plan(conn, plan_number) -> list[dict]:
    if not plan_number:
        return []
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rami_inventory'"
    ).fetchone()
    if not exists:
        return []
    # Normalised comparison on both sides: `plans` spaces its slashes
    # ("תמל/ 1064") and this feed does not. Exact matching lost 47 of 1,112.
    import db as _db
    key = _db.squash_plan_number(plan_number)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM rami_inventory WHERE"
        " replace(replace(replace(plan_number,' ',''),char(9),''),char(160),'')"
        " = ?", (key,))]


if __name__ == "__main__":
    import argparse
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="PlanWatch market-data importer.")
    parser.add_argument("--listings-csv", help="licensed property-feed CSV to import")
    parser.add_argument("--listings-json", help="licensed CRM/API JSON export to import")
    parser.add_argument("--listings-source", help="feed/provider label, e.g. broker-mls")
    parser.add_argument("--db", help="optional PlanWatch SQLite path")
    args = parser.parse_args()
    conn = db.get_conn(args.db)
    if args.listings_csv and args.listings_json:
        parser.error("choose only one of --listings-csv or --listings-json")
    if args.listings_csv or args.listings_json:
        total = (import_listings_csv(conn, args.listings_csv, source=args.listings_source)
                 if args.listings_csv else
                 import_listings_json(conn, args.listings_json, source=args.listings_source))
        print(f"imported {total:,} licensed listings")
        print(json.dumps(counts(conn), ensure_ascii=False, indent=1))
        conn.close()
        raise SystemExit(0)

    total = import_all(conn, progress=lambda k, n: print(f"  {k:11} {n:,}"))
    print(f"\nimported {total:,} market rows")
    print(json.dumps(counts(conn), ensure_ascii=False, indent=1))
    print("national:", json.dumps(national_trend(conn), ensure_ascii=False))
    print("spread:  ", json.dumps(national_price_percentiles(conn), ensure_ascii=False))
    for city in ("תל אביב", "חריש", "באר שבע"):
        print(f"{city}:", json.dumps(locality_price(conn, city), ensure_ascii=False))
    conn.close()
