"""Read-only bridge from the Python scraper DB into the main dashboard."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import threading
from collections import defaultdict
from contextlib import closing
from pathlib import Path


_LOCAL_DB = (
    Path(__file__).resolve().parent.parent
    / "real_estate_scraper" / "real_estate_scraper"
    / "data" / "real_estate.db"
)
# The scraper project lives beside this PlanWatch checkout in the normal
# workspace. Keep the old relative location as a fallback, but prefer the
# actual shared scraper DB when it exists so AD, Madlan and Komo are never
# silently replaced by a new feed.
#
# `parents[2]` assumes this file sits at least three directories below some
# ancestor - true on a normal checkout, false in a container where the app
# is copied to /app: parents are just [/app, /] and parents[2] doesn't
# exist. That used to raise IndexError while merely *constructing* the path,
# at import time, which took down every /api/combined-sale-listings request
# (this module is imported inside that handler) before _SHARED_DB.exists()
# ever ran. len() guards the traversal instead.
_HERE_PARENTS = Path(__file__).resolve().parents
_SHARED_DB = (
    _HERE_PARENTS[2] / "PROJECT-CITY"
    / "real_estate_scraper" / "real_estate_scraper"
    / "data" / "real_estate.db"
) if len(_HERE_PARENTS) > 2 else None
DEFAULT_DB = _SHARED_DB if (_SHARED_DB is not None and _SHARED_DB.exists()) else _LOCAL_DB


def _squash(name: str) -> str:
    """Strip spaces and hyphens so locality spellings compare equal."""
    return re.sub(r"[\s\-־]+", "", str(name or "")).strip()


#: Explicit denials of brokerage, stripped before looking for agency markers.
#: "ללא תיווך" *contains* "תיווך", so testing for an agency first classifies an
#: ad that says "no broker" as brokered - exactly inverted.
NO_BROKER_MARKERS = re.compile(r"ללא\s*תיווך|אין\s*תיווך|לא\s*דרך\s*מתווך", re.I)
AGENCY_MARKERS = re.compile(r"מתווך|תיווך|סוכנות|broker|agency|realt", re.I)
PRIVATE_MARKERS = re.compile(r"מבעל\s*הנכס|מפרטי|פרטי\b|מהבעלים", re.I)

#: Display text and certainty per state. Three states, never two: the business
#: plan (section ד) requires "לא ידוע" to be shown separately so a screen
#: cannot imply a private seller where the source said nothing.
BROKERAGE_LABELS = {"agency": "תיווך", "private": "ללא תיווך",
                    "unknown": "לא ידוע"}
BROKERAGE_CONFIDENCE = {"agency": "מאומת — המקור פרסם משרד",
                        "private": "נגזר מהמקור",
                        "unknown": "אין מידע במקור"}


def brokerage_state(raw: str | None, *, agency: str | None = None) -> str:
    """Brokerage state from positive evidence only: agency / private / unknown.

    Single source of truth for the question, shared by the listings table and
    the leads screen - two screens disagreeing about whether the same listing
    has a broker is worse than either being wrong alone.

    It returns "unknown" freely, and that is the point. This used to default to
    "ללא תיווך" whenever no broker keyword appeared, which sounds conservative
    and is not: measured over 400 rows per source, AD and Komo store the
    listing's card HTML and it carries **no** brokerage information in either
    direction. Defaulting therefore labelled ~4,900 listings "no broker" on the
    strength of a field that was never populated - and "ללא תיווך" is a call
    list, so that sends an agent to phone owners who are already represented.
    """
    if (agency or "").strip():
        return "agency"
    text = str(raw or "")
    if not text.strip() or text.strip() in ("{}", "null"):
        return "unknown"
    denied = bool(NO_BROKER_MARKERS.search(text))
    remainder = NO_BROKER_MARKERS.sub(" ", text)
    if AGENCY_MARKERS.search(remainder):
        # A named agency alongside a denial is a contradictory source. Prefer
        # "agency": the costly error is phoning an owner who is represented.
        return "agency"
    if denied or PRIVATE_MARKERS.search(remainder):
        return "private"
    return "unknown"


def _brokerage_from_raw(raw: str | None) -> str:
    """Display label for a legacy row's brokerage state."""
    return BROKERAGE_LABELS[brokerage_state(raw)]


def _tally(rows, key) -> dict:
    """Count non-empty values of `key` across rows, for the filter facets."""
    counts: dict = defaultdict(int)
    for row in rows:
        value = (row.get(key) or "").strip()
        if value:
            counts[value] += 1
    return counts


def _path() -> Path:
    return Path(os.environ.get(
        "PLANWATCH_SCRAPER_DB", str(DEFAULT_DB))).resolve()


def _connect() -> sqlite3.Connection:
    path = _path()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


SAFE_SALE_WHERE = """
    is_active=1 AND listing_type='sale'
    AND price IS NOT NULL AND price >= 100000
    AND street IS NOT NULL AND trim(street) <> ''
    AND (listing_url IS NULL OR (
        lower(listing_url) NOT LIKE '%/rent%'
        AND lower(listing_url) NOT LIKE '%rent=%'))
"""


#: Prefix map for image URLs extracted from each source's stored HTML.
#: AD stores protocol-relative URLs ("//img4.ad.co.il/..."); Komo stores
#: root-relative paths ("/api/modaot/..."). Madlan-authorized stores a JSON
#: payload with no image at all, so it contributes None.
IMAGE_PREFIXES = {
    "ad": "https:",
    "komo": "https://www.komo.co.il",
}


def extract_listing_image(raw: str | None, source: str | None) -> str | None:
    """First image URL from a source's stored card HTML, or None.

    Both AD and Komo save the listing card's HTML fragment in `raw_json`.
    The useful field is the <img src=...>: AD's is protocol-relative on
    img4.ad.co.il, Komo's is root-relative on www.komo.co.il. Everything
    else (madlan-authorized, madlan-homepage) stores structured JSON with
    no image reference, so it stays None and the UI falls back to the
    existing placeholder rendering.
    """
    if not raw or not source:
        return None
    prefix = IMAGE_PREFIXES.get(source.lower())
    if not prefix:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw, re.I)
    if not match:
        return None
    src = match.group(1).replace("&amp;", "&")
    if src.startswith("//"):
        return f"{prefix}{src}"
    if src.startswith("/"):
        return f"{prefix}{src}"
    return src


def status() -> dict:
    path = _path()
    if not path.exists():
        return {"available": False, "path": str(path), "records": 0}
    with closing(_connect()) as conn:
        count = conn.execute(
            f"SELECT COUNT(*) FROM listings WHERE {SAFE_SALE_WHERE}").fetchone()[0]
    return {"available": True, "path": str(path), "records": count,
            "mode": "read_only"}


def _as_day(value: str) -> str:
    """First 10 chars of an ISO timestamp - its calendar day.

    `first_seen` is stored as a full ISO timestamp
    ("2026-07-30T22:18:33.224229") while the UI sends a plain date
    ("2026-07-30"). Comparing the two as strings makes an inclusive upper
    bound behave exclusively, because "2026-07-30T22:.." sorts *after*
    "2026-07-30". Both sides are reduced to the day instead.
    """
    return str(value or "").strip()[:10]


#: Plausibility envelope for a single home for sale, mirroring what
#: SINGLE_HOME_PRICE_CEILING does on the transactions side.
#:
#: The feeds carry placeholder and typo rows that are not homes: a 42m² flat
#: in נהריה listed at ₪790,000,000, repeated ₪111,111,111 placeholders, a
#: "200,000 m²" roof apartment, and - most damaging - rows whose area field
#: arrived as 1.0, which turns price_per_sqm into the whole price. Measured on
#: the live feed, 1-2% of rows breach each bound.
#:
#: These matter far beyond a silly row at the top of a sorted list: every
#: score in this view is a comparison against the *median* ₪/m² of the
#: listing's neighbourhood, so a single ₪100,000,000/m² row drags that median
#: and quietly restates the discount of every genuine listing near it. They
#: are therefore excluded from the benchmark sample and flagged in the output,
#: never silently deleted - `exclude_suspect=1` is how a caller drops them.
PLAUSIBLE = {
    "price":   (100_000, 50_000_000),
    "area_sqm": (15, 1_000),
    # Tel Aviv prime genuinely reaches ~100k/m², so the ceiling sits well
    # above it; the floor catches rows priced as if the area were a typo.
    "price_per_sqm": (2_000, 250_000),
}


def _implausible(row) -> str | None:
    """Why this row cannot be a single home for sale, or None if it can."""
    for field, (low, high) in PLAUSIBLE.items():
        value = row.get(field)
        if value is None:
            continue
        if value < low:
            return f"{field} נמוך מדי ({value:g})"
        if value > high:
            return f"{field} גבוה מדי ({value:g})"
    return None


#: Orderings the listings view offers, as (row key, descending). "score" is
#: the default and the only one that is a judgement; the rest are plain facts
#: an agent sorts by when they already know what they are looking for.
SORTS = {
    "score":     ("rank_score", True),
    "discount":  ("discount_pct", True),
    "price_asc": ("price", False),
    "price_desc": ("price", True),
    "ppm_asc":   ("price_per_m2", False),
    "ppm_desc":  ("price_per_m2", True),
    "area_desc": ("area_m2", True),
    "rooms_desc": ("rooms", True),
    "newest":    ("first_seen", True),
}


def _sort_scored(scored: list, sort: str) -> list:
    """Return `scored` ordered by `sort`, with missing values last.

    Rows that have no value for the key are held out and appended rather than
    given a stand-in: a listing with no area must not sort as if it were 0 m²
    and head an ascending list. Ties fall back to the score.
    """
    key, desc = SORTS.get(sort or "score", SORTS["score"])
    known = [row for row in scored if row.get(key) is not None]
    missing = [row for row in scored if row.get(key) is None]
    known.sort(key=lambda row: row["rank_score"], reverse=True)
    known.sort(key=lambda row: row[key], reverse=desc)
    missing.sort(key=lambda row: row["rank_score"], reverse=True)
    return known + missing


#: Cached scoring input, keyed on the scraper DB's identity and mtime.
#: Rebuilding it per request meant every filter change, sort change and page
#: turn re-read and re-scored the whole table; the benchmarks it holds depend
#: only on the data, never on the filters, so they survive until the scraper
#: writes again. Guarded by a lock because ThreadingHTTPServer serves requests
#: concurrently and two first-hits would otherwise both build it.
_SCORE_CACHE: dict = {}
_SCORE_LOCK = threading.Lock()


def _scoring_snapshot():
    """Return (all_rows, sources, city_values, neighborhood_values), cached.

    The cache key is (path, mtime, size). SQLite updates the file's mtime on
    commit, so an import by the scraper invalidates this without any explicit
    signal between the two processes.
    """
    path = _path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None

    cached = _SCORE_CACHE.get("entry")
    if key is not None and cached and cached[0] == key:
        return cached[1]

    if not path.exists():
        # This source is optional: real_estate.db lives in a sibling
        # checkout outside this repo (see DEFAULT_DB above) and simply does
        # not exist in environments that never had it, e.g. this container -
        # sqlite3.connect(..., mode=ro) cannot open a file that is not
        # there, and used to raise OperationalError straight out of
        # `_connect()` with nothing catching it, which took down the whole
        # merged /api/combined-sale-listings response (Yad2 + ONMAP
        # included) over one missing, genuinely-optional input. An absent
        # file here is an expected deployment state, not a fault.
        return [], [], defaultdict(list), defaultdict(list)

    with _SCORE_LOCK:
        cached = _SCORE_CACHE.get("entry")
        if key is not None and cached and cached[0] == key:
            return cached[1]

        with closing(_connect()) as conn:
            all_rows = [dict(row) for row in conn.execute(
                """SELECT source, listing_id, city, neighborhood, street, price,
                          rooms, floor, area_sqm, price_per_sqm, listing_url,
                          first_seen, last_seen, listed_at, raw_json
                    FROM listings
                    WHERE """ + SAFE_SALE_WHERE)]
            sources = [dict(row) for row in conn.execute(
                """SELECT source, COUNT(*) listings, MAX(last_seen) last_fetched_at
                     FROM listings WHERE """ + SAFE_SALE_WHERE + """
                    GROUP BY source ORDER BY listings DESC""")]

        for row in all_rows:
            raw_html = row.get("raw_json")
            row["brokerage"] = _brokerage_from_raw(raw_html)
            # Image URL is derived from the stored card HTML while it is still
            # in hand; `raw_json` is dropped right after so scored rows do not
            # ship multi-KB HTML fragments to the browser.
            row["image"] = extract_listing_image(raw_html, row.get("source"))
            row.pop("raw_json", None)

        # Benchmarks are built only from rows that could be a real home. An
        # implausible row is still returned and scored - it is just not used
        # as a comparable for its neighbours. See PLAUSIBLE.
        city_values = defaultdict(list)
        neighborhood_values = defaultdict(list)
        for row in all_rows:
            row["_suspect"] = _implausible(row)
            if row.get("price_per_sqm") and not row["_suspect"]:
                city_values[row["city"]].append(float(row["price_per_sqm"]))
                neighborhood_values[
                    (row["city"], row.get("neighborhood"))].append(
                        float(row["price_per_sqm"]))

        snapshot = (all_rows, sources, city_values, neighborhood_values)
        if key is not None:
            _SCORE_CACHE["entry"] = (key, snapshot)
        return snapshot


def listings(*, locality="", neighborhood="", street="", source="",
             min_price=None, max_price=None, min_area=None, max_area=None,
             min_rooms=None, max_rooms=None, min_ppm=None, max_ppm=None,
             date_from="", date_to="", min_discount_pct=None,
             max_discount_pct=None, has_url=None, exclude_suspect=False,
             sort="score", limit=80, offset=0) -> dict:
    """Scored for-sale listings from the read-only scraper DB.

    Filters are applied in Python rather than SQL on purpose: the ₪/m²
    benchmark each row is scored against is the median of its neighbourhood
    (or city), and that median has to be computed over *every* listing. Doing
    the filtering in SQL would compute each benchmark from the surviving rows
    only, so narrowing the price range would move the benchmark and silently
    change every discount figure.
    """
    all_rows, sources, city_values, neighborhood_values = _scoring_snapshot()

    scored = []
    for row in all_rows:
        if locality and _squash(locality) not in _squash(row.get("city")):
            continue
        if neighborhood and _squash(neighborhood) not in _squash(
                row.get("neighborhood")):
            continue
        if street and _squash(street) not in _squash(row.get("street")):
            continue
        # Exact match, not substring. The source picker sends the stored name
        # verbatim, and "ad" is a substring of "madlan-authorized" - so asking
        # for אדס (2,698 listings) returned 4,137, silently folding מדל״ן into
        # it. Nothing here is a search box; every value comes from the facet
        # list, which is exactly the set of stored names.
        if source and source.strip().lower() != (row.get("source") or "").lower():
            continue
        if min_price is not None and row["price"] < float(min_price):
            continue
        if max_price is not None and row["price"] > float(max_price):
            continue
        # Area, rooms and floor are not published for every listing (area is
        # missing on ~12% of sale rows, floor on ~63%). A row with no value
        # cannot satisfy a bound on that field, so it drops out - the same
        # rule the original min_area used.
        if min_area is not None and (
                row.get("area_sqm") is None or
                row["area_sqm"] < float(min_area)):
            continue
        if max_area is not None and (
                row.get("area_sqm") is None or
                row["area_sqm"] > float(max_area)):
            continue
        if min_rooms is not None and (
                row.get("rooms") is None or row["rooms"] < float(min_rooms)):
            continue
        if max_rooms is not None and (
                row.get("rooms") is None or row["rooms"] > float(max_rooms)):
            continue
        # ₪/m² is the number a professional actually compares on, so it is
        # filterable in its own right and not only via the price/area pair.
        if min_ppm is not None and (
                row.get("price_per_sqm") is None or
                row["price_per_sqm"] < float(min_ppm)):
            continue
        if max_ppm is not None and (
                row.get("price_per_sqm") is None or
                row["price_per_sqm"] > float(max_ppm)):
            continue
        # ~18% of rows carry no listing_url. An agent chasing a deal needs the
        # ad itself, so let them exclude the ones they cannot open.
        if has_url and not (row.get("listing_url") or "").strip():
            continue
        # Dated on first_seen - when the scraper first caught the listing.
        # `listed_at` is the seller's own publication date and would be the
        # better field, but the feeds fill it on only ~18% of sale rows, so
        # filtering on it would silently hide most of the stock.
        seen_day = _as_day(row.get("first_seen"))
        if date_from and seen_day < _as_day(date_from):
            continue
        if date_to and seen_day > _as_day(date_to):
            continue
        comparable = neighborhood_values[
            (row["city"], row.get("neighborhood"))]
        scope = "שכונה"
        if len(comparable) < 5:
            comparable = city_values[row["city"]]
            scope = "עיר"
        benchmark = statistics.median(comparable) if comparable else None
        discount = (
            (benchmark - row["price_per_sqm"]) / benchmark * 100
            if benchmark and row.get("price_per_sqm") else None)
        if min_discount_pct is not None and (
                discount is None or discount < float(min_discount_pct)):
            continue
        # An upper bound on the gap is what separates "underpriced" from
        # "too good to be true" - the 50%+ rows are usually a wrong area or a
        # part-share sale, not a bargain.
        if max_discount_pct is not None and (
                discount is None or discount > float(max_discount_pct)):
            continue
        evidence = min(len(comparable), 20) / 2
        score = min(100, max(0, discount or 0) * 2 + evidence)
        quality = "תקין"
        if row.get("_suspect"):
            # A row outside the plausibility envelope is a broken record, not
            # a bargain: it is shown so nothing is hidden, but it never earns
            # a rank that would float it to the top of the default view.
            quality = f"נתון חשוד — {row['_suspect']}"
            score = 0.0
        elif discount is not None and discount > 50:
            quality = "חריג — לאמת מחיר ופרטי מודעה"
            score = min(score, 60)
        if exclude_suspect and quality != "תקין":
            continue
        scored.append({
            "rank_score": round(score, 1),
            "confidence": "high" if len(comparable) >= 10 else "partial",
            "locality": row["city"],
            "address": ", ".join(filter(None, (
                row.get("neighborhood"), row.get("street")))),
            "rooms": row.get("rooms"),
            "area_m2": row.get("area_sqm"),
            "year_built": None,
            "price": row.get("price"),
            "price_per_m2": row.get("price_per_sqm"),
            "benchmark_m2": round(benchmark) if benchmark else None,
            "benchmark_scope": scope,
            "comparable_count": len(comparable),
            "discount_pct": round(discount, 1) if discount is not None else None,
            "plans_approved": None,
            # Madlan-authorized rows currently carry synthetic/incorrect ad
            # URLs.  Keep the listing and its scoring data, but never expose
            # a broken link in the dashboard.
            "url": (None if "madlan" in (row.get("source") or "").lower()
                    else row.get("listing_url")),
            "source": row.get("source"),
            "image": row.get("image"),
            "brokerage": row.get("brokerage", "ללא תיווך"),
            "first_seen": row.get("first_seen"),
            "last_seen": row.get("last_seen"),
            "listed_at": row.get("listed_at"),
            "data_quality": quality,
        })
    scored = _sort_scored(scored, sort)
    total = len(scored)
    seen_days = sorted(_as_day(r.get("first_seen")) for r in all_rows
                       if r.get("first_seen"))
    return {
        "has_listings": bool(all_rows),
        "total": total,
        "rows": scored[offset:offset + limit],
        "sources": sources,
        # Facets for the filter UI, computed over the whole table rather than
        # the filtered page so the dropdowns do not shrink as you narrow.
        "localities": [dict(r) for r in sorted(
            ({"locality": c, "listings": n} for c, n in
             _tally(all_rows, "city").items()),
            key=lambda r: -r["listings"])],
        "date_range": {"from": seen_days[0], "to": seen_days[-1]}
                      if seen_days else None,
        "filters_applied": {k: v for k, v in {
            "locality": locality, "neighborhood": neighborhood,
            "street": street, "source": source, "min_price": min_price,
            "max_price": max_price, "min_area": min_area,
            "max_area": max_area, "min_rooms": min_rooms,
            "max_rooms": max_rooms, "min_ppm": min_ppm, "max_ppm": max_ppm,
            "date_from": date_from, "date_to": date_to,
            "min_discount_pct": min_discount_pct,
            "max_discount_pct": max_discount_pct,
            "has_url": has_url, "exclude_suspect": exclude_suspect,
        }.items() if v not in (None, "", False)},
        "sort": sort if sort in SORTS else "score",
        "sorts": list(SORTS),
        # Data health, so the UI can say how much of the feed it distrusts
        # instead of leaving the user to discover it by sorting.
        "suspect_held": sum(1 for r in all_rows if r.get("_suspect")),
        "listings_held": len(all_rows),
        "read_only": True,
        "imported_into_planwatch": False,
        "note": (
            "מודעות פעילות ממסד סורקי Python חיצוני, בתצוגת קריאה בלבד. "
            "הציון משווה מחיר מבוקש למ״ר לחציון בשכונה או בעיר; "
            "מחיר מבוקש אינו מחיר עסקה."
        ),
        "weights": {"discount": 2, "evidence": 0.5},
    }


#: Asset types that are a home changing hands. The official feed mixes these
#: with land, combination and commercial deals worth orders of magnitude more
#: (measured: apartments ₪4.2M average vs combination ₪271M, top row ₪316bn),
#: so any average that does not filter on these is meaningless.
#: Must stay in sync with nadlan_ckan_scraper.RESIDENTIAL_TYPES - the importer
#: and this reader have to agree on what counts as a home, or the dashboard
#: reports a different count than the scraper imported. Enumerated from the
#: live feed: an earlier shorter list dropped ~6,800 real sales ("דירה" alone
#: is 5,521 rows since 2015).
RESIDENTIAL_TYPES = (
    "דירה בבית קומות", "דירה", "דירת גן", "דירת גג",
    "קוטג' דו משפחתי", "קוטג' חד משפחתי", "קוטג' טורי",
    "בית בודד", "מגורים", "חד משפחתי (וילה)", "דופלקס", "מיני פנטהאוז",
)


def transactions_for_parcel(gush, helka, *, limit=50) -> dict:
    """Concluded sales on ONE parcel - the strongest evidence of its value.

    gush/helka live inside `raw_json` (the transactions table has no dedicated
    columns), so this matches on the JSON payload directly. The stored form is
    ``"gush": "12794", "helka": "16"`` - matching on that exact substring is
    what keeps 6941/23 from also matching 6941/230.
    """
    path = _path()
    if not path.exists():
        return {"available": False, "total": 0, "rows": []}
    gush, helka = str(gush).strip(), str(helka).strip()
    if not gush.isdigit() or not helka.isdigit():
        return {"available": False, "total": 0, "rows": [],
                "note": "גוש וחלקה חייבים להיות מספריים."}
    needle = f'"gush": "{gush}", "helka": "{helka}"'

    with closing(_connect()) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone():
            return {"available": False, "total": 0, "rows": []}
        rows = [dict(row) for row in conn.execute(
            """SELECT city, street, house_number, deal_date, price, floor,
                      year_built, asset_type
                 FROM transactions
                WHERE instr(raw_json, ?) > 0
                ORDER BY deal_date DESC LIMIT ?""", (needle, int(limit)))]
    return {
        "available": True, "total": len(rows), "rows": rows,
        "gush": gush, "helka": helka, "parcel_key": f"{gush}/{helka}",
        "read_only": True,
        "source": "מידע לעם — מאגר עסקאות הנדל״ן (רשות המסים)",
        "license": "CC-BY",
    }


def transactions_status() -> dict:
    """Report how many concluded transactions the scraper DB holds.

    Returns both figures on purpose. `records` is every row in the table;
    `records_residential` applies the same residential-type and price-ceiling
    filter the transactions view defaults to. They differ (285,535 vs
    285,444 on the current data), so a caller that shows one number next to a
    table rendering the other is showing the user a contradiction - see
    market_client.counts, which reports the filtered figure because that is
    what the UI renders.
    """
    path = _path()
    if not path.exists():
        return {"available": False, "path": str(path), "records": 0,
                "records_residential": 0}
    with closing(_connect()) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()
        if not exists:
            return {"available": False, "path": str(path), "records": 0,
                    "records_residential": 0, "mode": "read_only"}
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        marks = ",".join("?" for _ in RESIDENTIAL_TYPES)
        residential = conn.execute(
            f"""SELECT COUNT(*) FROM transactions
                 WHERE price IS NOT NULL AND price > 0
                   AND asset_type IN ({marks}) AND price <= ?""",
            (*RESIDENTIAL_TYPES, SINGLE_HOME_PRICE_CEILING)).fetchone()[0]
    return {"available": True, "path": str(path), "records": count,
            "records_residential": residential, "mode": "read_only"}


#: Ceiling for a single-home sale. Even inside residential asset types the
#: feed carries whole-building and whole-project deals - a "מגורים" row at
#: ₪316bn and "דירה בבית קומות" rows at ₪2bn are real, just not a flat. 36
#: rows sit above this line out of ~148k; leaving them in dragged every
#: average and put nonsense at the top of a price-sorted list.
SINGLE_HOME_PRICE_CEILING = 100_000_000


def transactions(*, locality="", street="", date_from="", date_to="",
                 min_price=None, max_price=None, residential_only=True,
                 limit=80, offset=0, include_facets=True) -> dict:
    """Concluded sale transactions from the official CC-BY dataset.

    This is the *other* side of the market from ``listings``: what buyers
    actually paid, not what sellers are asking. The dataset publishes no area
    or room count (NULL across all 357,593 source rows), so this view reports
    price levels and deal counts only - never a price-per-m2, which would
    require an area the source does not have.
    """
    path = _path()
    if not path.exists():
        return {"available": False, "total": 0, "rows": [], "cities": [],
                "note": "מסד הסורק לא נמצא."}

    clauses, params = ["price IS NOT NULL", "price > 0"], []
    if residential_only:
        marks = ",".join("?" for _ in RESIDENTIAL_TYPES)
        clauses.append(f"asset_type IN ({marks})")
        params.extend(RESIDENTIAL_TYPES)
        # Asset type alone is not enough: whole-building sales are filed under
        # residential types too. See SINGLE_HOME_PRICE_CEILING.
        clauses.append("price <= ?")
        params.append(SINGLE_HOME_PRICE_CEILING)
    if locality:
        # Hyphen and space placement differs between sources: PlanWatch says
        # "תל אביב-יפו" while the official dataset says "תל אביב -יפו", and a
        # plain LIKE silently matched nothing for the country's largest city.
        # Compare on a punctuation-free form so both spellings resolve.
        clauses.append(
            "REPLACE(REPLACE(REPLACE(city, ' ', ''), '-', ''), '־', '') "
            "LIKE ?")
        params.append(f"%{_squash(locality)}%")
    if street:
        clauses.append("street LIKE ?")
        params.append(f"%{street}%")
    if date_from:
        clauses.append("deal_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("deal_date <= ?")
        params.append(date_to)
    if min_price is not None:
        clauses.append("price >= ?")
        params.append(float(min_price))
    if max_price is not None:
        clauses.append("price <= ?")
        params.append(float(max_price))
    where = " AND ".join(clauses)

    with closing(_connect()) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone():
            return {"available": False, "total": 0, "rows": [], "cities": [],
                    "note": "טרם יובאו עסקאות. הרץ nadlan_ckan_scraper.py."}
        # `transactions` carries no usable index for these filters - the
        # residential asset types alone are ~88% of the rows, so every query
        # below is a sequential scan of 285k raw_json-bearing rows and costs
        # seconds. The row page is the only one that has to run per request;
        # the total and the per-city summary depend solely on the filters, so
        # paging re-ran two full scans to recompute numbers that had not
        # changed. `include_facets=False` skips them and the caller reuses
        # what it already has. See dashboard.html's DL_TXFACETS.
        rows = [dict(row) for row in conn.execute(
            f"""SELECT city, street, house_number, deal_date, price, floor,
                       year_built, asset_type, raw_json
                  FROM transactions WHERE {where}
                 ORDER BY deal_date DESC, price DESC
                 LIMIT ? OFFSET ?""", params + [int(limit), int(offset)])]
        total = cities = None
        if include_facets:
            total = conn.execute(
                f"SELECT COUNT(*) FROM transactions WHERE {where}", params
            ).fetchone()[0]
            cities = [dict(row) for row in conn.execute(
                f"""SELECT city, COUNT(*) deals,
                           CAST(AVG(price) AS INTEGER) avg_price,
                           MIN(deal_date) first_deal, MAX(deal_date) last_deal
                      FROM transactions WHERE {where}
                     GROUP BY city ORDER BY COUNT(*) DESC""", params)]

    out = []
    for row in rows:
        gush = helka = None
        try:
            raw = json.loads(row.pop("raw_json") or "{}")
            gush, helka = raw.get("gush"), raw.get("helka")
        except (ValueError, TypeError):
            pass
        row["gush"], row["helka"] = gush, helka
        row["parcel_key"] = f"{gush}/{helka}" if gush and helka else None
        out.append(row)

    prices = [row["price"] for row in out if row.get("price")]
    return {
        "available": True,
        "total": total,
        "rows": out,
        "cities": cities,
        "median_price": statistics.median(prices) if prices else None,
        "residential_only": residential_only,
        "read_only": True,
        "source": "מידע לעם — מאגר עסקאות הנדל״ן (רשות המסים)",
        "source_url": "https://www.odata.org.il/dataset/nadlan",
        "license": "CC-BY",
        "note": (
            "עסקאות מכר שבוצעו בפועל — מחיר ששולם, לא מחיר מבוקש. "
            "המקור אינו מפרסם שטח או מספר חדרים, ולכן אין ₪/מ״ר." +
            ("" if residential_only else
             " שים לב: הסינון למגורים כבוי — התוצאות כוללות קרקע, "
             "קומבינציה ומסחרי בסדרי גודל שונים לחלוטין.")
        ),
    }
