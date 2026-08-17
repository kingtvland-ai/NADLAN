"""Harvest for-sale listings from ONMAP (onmap.co.il).

Why this is a separate source
-----------------------------
ONMAP publishes things Yad2 does not, and each one closes a gap this project
had to work around:

- ``created_at`` is a real publication timestamp. Yad2 gives none at all, which
  is why ``yad2_feed.image_date`` had to infer age from photo URLs. ONMAP rows
  need no inference: ``age_basis`` for them is ``published``.
- ``images`` arrives as a list of up to ~18 photos, each in three resolutions
  (thumbnail / gallery / full). The lightbox uses ``full``.
- ``additional_info`` carries area, rooms, floor, parking and bathrooms.

The depth cap, and how this works around it
-------------------------------------------
The API pages with ``$skip`` and ``$limit`` but stops returning rows past
roughly 3,000 - measured: ``$skip=1000`` still answers 24 rows, ``$skip=3000``
answers 0 with ``hasNextPage: false``. Raising ``$limit`` is rejected (HTTP
400). So a single ordering can only ever expose the first ~3,000 listings.

The workaround is to walk the same result set under several orderings. Newest
first and oldest first reach opposite ends; cheapest and dearest reach two
more. Rows are keyed on ``id``, so the passes union rather than duplicate, and
each ordering contributes whatever it alone can reach.

This does not claim to fetch every ONMAP listing. It fetches every listing the
public API is willing to page to, and records how many that was.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import db

#: The scraper definition holds the endpoint, the query defaults and the
#: cookies that make the API answer. It lives with the other connector
#: definitions rather than being duplicated here, so refreshing cookies in one
#: place fixes both.
DEFINITION = Path(os.environ.get(
    "PLANWATCH_ONMAP_DEFINITION",
    r"F:\AI-STUDIO-BUILDER-APP\PROJECT-CITY\מאתר-API\curl2api\scrapers\onmap.json"))

#: Orderings walked in turn. Each reaches a different ~3,000-row window of the
#: same result set; together they cover far more than any one of them.
SORTS = ("-search_date", "search_date", "price", "-price")

#: Where the API stops answering. Measured, not guessed: $skip=1000 returns
#: rows, $skip=3000 returns none.
MAX_SKIP = 3000

PAGE_SIZE = 24

SCHEMA = """
CREATE TABLE IF NOT EXISTS onmap_listings (
    id            TEXT PRIMARY KEY,
    price         INTEGER,
    currency      TEXT,
    property_type TEXT,
    city          TEXT,
    neighborhood  TEXT,
    street        TEXT,
    address_text  TEXT,
    rooms         REAL,
    area_sqm      REAL,
    floor         TEXT,
    bathrooms     REAL,
    parking       REAL,
    lat           REAL,
    lon           REAL,
    /* ONMAP's own publication timestamp. Unlike Yad2 this needs no inference,
     * so these rows carry a `published` age basis in the lead engine. */
    created_at    TEXT,
    search_date   TEXT,
    slug          TEXT,
    url           TEXT,
    image         TEXT,
    images_json   TEXT,
    is_promoted   INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    delisted_at   TEXT,
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS onmap_price_history (
    id          TEXT NOT NULL,
    price       INTEGER,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (id, observed_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_onmap_city    ON onmap_listings (city);
CREATE INDEX IF NOT EXISTS idx_onmap_created ON onmap_listings (created_at);
"""


def _add_column(conn: sqlite3.Connection, name: str, kind: str = "TEXT") -> None:
    """ALTER ... ADD COLUMN, tolerant of losing the race.

    SQLite has no `ADD COLUMN IF NOT EXISTS`, and the column list has to be read
    before the ALTER - so the contact sweep running in its own thread and an
    HTTP request arriving mid-flight can both read "missing" and both try to
    add it. The loser used to take down the request with
    `duplicate column name: description`. The failure is the success case.
    """
    try:
        conn.execute(f"ALTER TABLE onmap_listings ADD COLUMN {name} {kind}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(onmap_listings)")}
    # `phone` was added after rows already existed. The number was in `raw_json`
    # all along - the harvest simply never lifted it out.
    for name in ("phone", "house_number", "contact_name", "contact_checked_at",
                 "description", "motivation_flags"):
        if name not in columns:
            _add_column(conn, name)
    if "owner_property_count" not in columns:
        _add_column(conn, "owner_property_count", "INTEGER")
    conn.commit()
    repair_urls(conn)
    backfill_phones(conn)
    backfill_house_numbers(conn)


def backfill_house_numbers(conn: sqlite3.Connection) -> int:
    """Lift `address.he.house_number` out of stored `raw_json`.

    Measured: 1,534 of 2,839 rows carry one. A rewrite of what is already
    stored, not a new fetch - and the difference between an ONMAP listing that
    can be compared to its neighbours and one that cannot.
    """
    pending = conn.execute(
        """SELECT id, raw_json FROM onmap_listings
            WHERE house_number IS NULL AND raw_json IS NOT NULL""").fetchall()
    updates = []
    for row in pending:
        try:
            address = (json.loads(row["raw_json"]) or {}).get("address") or {}
        except (ValueError, TypeError):
            continue
        number = ((address.get("he") or {}).get("house_number")
                  or (address.get("en") or {}).get("house_number"))
        if number not in (None, ""):
            updates.append((str(number).strip(), row["id"]))
    if updates:
        conn.executemany(
            "UPDATE onmap_listings SET house_number = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def backfill_phones(conn: sqlite3.Connection) -> int:
    """Lift `phone_number` out of stored `raw_json` for rows harvested before
    the column existed. A rewrite of what we already hold, not a new fetch."""
    pending = conn.execute(
        """SELECT id, raw_json FROM onmap_listings
            WHERE phone IS NULL AND raw_json IS NOT NULL
              AND instr(raw_json, 'phone_number') > 0""").fetchall()
    updates = []
    for row in pending:
        try:
            payload = json.loads(row["raw_json"]) or {}
            phone = (
                normalise_phone(payload.get("phone_number"))
                or normalise_phone(((payload.get("contacts") or {})
                                    .get("primary") or {}).get("phone"))
                or normalise_phone(((payload.get("contact") or {})
                                    .get("primary") or {}).get("phone"))
            )
        except (ValueError, TypeError):
            continue
        if phone:
            updates.append((phone, row["id"]))
    if updates:
        conn.executemany("UPDATE onmap_listings SET phone = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def normalise_phone(value) -> str | None:
    """An Israeli number in the local form people actually dial.

    ONMAP publishes E.164 (`+972544269547`); a person calling from Israel needs
    `0544269547`. Anything that is not a plausible Israeli number is dropped
    rather than reformatted into something dialable but wrong.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    if len(digits) in (9, 10) and digits.startswith("0"):
        return digits
    return None


def repair_urls(conn: sqlite3.Connection) -> int:
    """Rewrite ad URLs stored in the dead ``/en/properties/`` form.

    Every row harvested before 2026-08-09 holds a link ONMAP answers with its
    own "page not found" screen. The slug and coordinates needed to build a
    working link were already stored, so this is a rewrite, not a re-harvest.
    Rows already in the current form are untouched, which makes it a no-op on
    every run after the first.
    """
    pending = conn.execute(
        """SELECT id, slug, lat, lon FROM onmap_listings
            WHERE slug IS NOT NULL
              AND (url IS NULL OR url NOT LIKE '%/search/homes/%')""").fetchall()
    updates = [(listing_url(row["slug"], row["lat"], row["lon"]), row["id"])
               for row in pending]
    updates = [pair for pair in updates if pair[0]]
    if updates:
        conn.executemany("UPDATE onmap_listings SET url = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OnmapError(RuntimeError):
    """The ONMAP API could not be reached or did not answer usefully."""


def _definition() -> dict:
    if not DEFINITION.exists():
        raise OnmapError(
            f"הגדרת ONMAP לא נמצאה: {DEFINITION}. "
            "הגדר PLANWATCH_ONMAP_DEFINITION לנתיב הנכון.")
    return json.loads(DEFINITION.read_text(encoding="utf-8"))


def _headers(definition: dict) -> dict:
    cookies = definition.get("cookies") or {}
    return {
        "accept": "application/json, text/plain, */*",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/151.0.0.0 Safari/537.36"),
        "referer": "https://www.onmap.co.il/",
        "accept-language": "he-IL",
        "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }


def fetch_page(skip: int, sort: str, *, timeout: float = 60.0) -> dict:
    definition = _definition()
    params = dict(definition.get("params") or {})
    params.update({"$skip": str(int(skip)), "$sort": sort,
                   "$limit": str(PAGE_SIZE)})
    url = f"{definition['url']}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=_headers(definition))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OnmapError(f"HTTP {exc.code} at skip={skip} sort={sort}") from exc
    except Exception as exc:
        raise OnmapError(f"{type(exc).__name__} at skip={skip}: {exc}") from exc


#: The per-property endpoint. The search feed carries a phone on 36 of 2,839
#: rows; this one returned a number on **20 of 20** sampled listings, at
#: `contacts.primary.phone`. It also returns `properties_by_owner`, which is
#: ONMAP telling us directly what else the same advertiser is selling - the
#: cross-reference `listing_seller` could not build for Yad2 for want of any
#: identifier at all.
DETAIL_URL = "https://phoenix.onmap.co.il/v1/properties/{id}"

#: Gap between contact lookups. The sweep is ~2,800 requests against someone
#: else's API to fill a field they publish on every ad page; pacing it is the
#: least it deserves.
CONTACT_DELAY_SECONDS = 0.4


def fetch_detail(listing_id: str, *, timeout: float = 30.0) -> dict:
    """The full record for one property, including the advertiser's number."""
    definition = _definition()
    url = DETAIL_URL.format(id=urllib.parse.quote(str(listing_id)))
    request = urllib.request.Request(url, headers=_headers(definition))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OnmapError(f"HTTP {exc.code} for {listing_id}") from exc
    except Exception as exc:
        raise OnmapError(f"{type(exc).__name__} for {listing_id}: {exc}") from exc


#: What a seller writes in their own ad, and what it means. Every other
#: motivation signal in this system is inferred - days on the board, a price
#: cut, several flats in one building. This one is the seller *saying so*.
#:
#: Ordered by how much it is worth. "כונס נכסים" is a court-appointed receiver
#: selling under an obligation to realise the asset; "הזדמנות" is a word every
#: agent types, which is why it is not in this list at all.
MOTIVATION_PATTERNS = (
    ("כינוס נכסים", r"כונס\s*נכסים|כינוס\s*נכסים|הליכי\s*כינוס"),
    ("עיזבון או ירושה", r"עיזבון|עזבון|ירוש[הת]\b|יורשים"),
    ("מחיר גמיש", r"גמיש(?:ות)?\s*במחיר|מחיר\s*גמיש|גמישות\s*במחיר|במחיר\s*גמיש"),
    ("דחוף", r"\bדחוף\b|בדחיפות|למכירה\s*מהיר[הת]"),
    ("מחיר ירד", r"מחיר\s*ירד|ירידת\s*מחיר|הופחת"),
    ("רילוקיישן או מעבר", r"רילוקיישן|רילוקשיין|עקב\s*מעבר|עוזבים\s*את\s*הארץ"),
    ("פינוי מיידי", r"פינוי\s*מייד[יב]|כניס[הת]\s*מייד"),
    # Weakest of the set, and separated from "מחיר גמיש" on purpose. Measured
    # over 648 ad texts: 53 say the *move-out date* is flexible and only 2 say
    # the *price* is. Folding them together - the obvious thing to do, since
    # both are the word "גמיש" - would have labelled 53 ordinary sellers as
    # open to a lower offer, which is precisely the false lead a call list
    # cannot afford.
    ("פינוי גמיש", r"פינוי[:\s]*(?:עד\s*\S+\s*)?\(?\s*גמיש|כניסה\s*גמיש|"
                   r"תארי[ךכ]\s*כניסה\s*[-–]?\s*גמיש|פנוי\s*גמיש|ו?גמיש\s*$"),
)


def motivation_of(text: str | None) -> list[str]:
    """Motivation the seller stated in words, not motivation we inferred."""
    blob = str(text or "")
    if not blob.strip():
        return []
    return [label for label, pattern in MOTIVATION_PATTERNS
            if re.search(pattern, blob, re.I)]


def contact_of(detail: dict) -> dict:
    """Phone, agency and the owner's other listings, out of a detail payload."""
    contacts = (detail.get("contacts") or {})
    primary = contacts.get("primary") or {}
    others = detail.get("properties_by_owner") or []
    return {
        "phone": normalise_phone(primary.get("phone")),
        "contact_name": primary.get("name") or primary.get("full_name"),
        "agency_id": detail.get("agency_id"),
        # ONMAP's own answer to "what else is this person selling".
        "owner_property_count": len(others) or None,
        "owner_property_ids": [o.get("id") for o in others if o.get("id")] or None,
        # The same request already carries the ad's own text; reading it here
        # costs nothing and is the only place a seller states their own reason.
        "description": (detail.get("description") or "").strip() or None,
        "motivation_flags": motivation_of(detail.get("description")),
    }


def fetch_contact(listing_id: str) -> dict:
    """One listing's contact, stored on the row so it is fetched only once."""
    detail = fetch_detail(listing_id)
    found = contact_of(detail)
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        conn.execute(
            """UPDATE onmap_listings
                  SET phone = COALESCE(?, phone),
                      contact_name = COALESCE(?, contact_name),
                      owner_property_count = COALESCE(?, owner_property_count),
                      description = COALESCE(?, description),
                      motivation_flags = ?,
                      contact_checked_at = ?
                WHERE id = ?""",
            (found["phone"], found["contact_name"], found["owner_property_count"],
             found["description"],
             json.dumps(found["motivation_flags"], ensure_ascii=False)
             if found["motivation_flags"] else None,
             _now(), str(listing_id)))
        conn.commit()
    finally:
        conn.close()
    return found


def rescore_motivation() -> int:
    """Re-read stored ad text with the current patterns. No network.

    The patterns are tuned against real ad language and will keep being tuned;
    re-fetching 2,839 pages to apply a regex change would be absurd when the
    text is already here.
    """
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT id, description FROM onmap_listings "
            "WHERE description IS NOT NULL").fetchall()
        updates = []
        for row in rows:
            flags = motivation_of(row["description"])
            updates.append((json.dumps(flags, ensure_ascii=False) if flags else None,
                            row["id"]))
        conn.executemany(
            "UPDATE onmap_listings SET motivation_flags = ? WHERE id = ?", updates)
        conn.commit()
        return sum(1 for u in updates if u[0])
    finally:
        conn.close()


def harvest_contacts(*, limit: int | None = None) -> dict:
    """Fill in the advertiser's number for stored listings that lack one.

    Bounded and paced. Rows already checked are skipped on the next run, so
    this is resumable and a second pass costs nothing.
    """
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        # `description IS NULL` as well as never-checked: rows swept before the
        # ad text was stored are re-read once, and only once.
        sql = ("""SELECT id FROM onmap_listings
                   WHERE delisted_at IS NULL
                     AND (contact_checked_at IS NULL OR description IS NULL)
                   ORDER BY created_at DESC""")
        if limit:
            sql += f" LIMIT {int(limit)}"
        ids = [row["id"] for row in conn.execute(sql)]
    finally:
        conn.close()

    found = failed = 0
    for index, listing_id in enumerate(ids):
        try:
            if fetch_contact(listing_id).get("phone"):
                found += 1
        except Exception:
            failed += 1
        _CONTACT_STATE.update(status="running", checked=index + 1,
                              total=len(ids), found=found, failed=failed)
        if index + 1 < len(ids):
            time.sleep(CONTACT_DELAY_SECONDS)
    _CONTACT_STATE.update(status="complete", checked=len(ids), total=len(ids),
                          found=found, failed=failed, finished_at=_now())
    return dict(_CONTACT_STATE)


_CONTACT_STATE: dict = {"status": "idle", "checked": 0, "total": 0,
                        "found": 0, "failed": 0, "finished_at": None}
_CONTACT_WORKER: threading.Thread | None = None


def contact_state() -> dict:
    snapshot = dict(_CONTACT_STATE)
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        row = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN phone IS NOT NULL THEN 1 ELSE 0 END) with_phone,
                      SUM(CASE WHEN contact_checked_at IS NOT NULL THEN 1 ELSE 0 END) checked
                 FROM onmap_listings WHERE delisted_at IS NULL""").fetchone()
        snapshot.update(stored=row["total"], with_phone=row["with_phone"] or 0,
                        already_checked=row["checked"] or 0)
    finally:
        conn.close()
    snapshot["running"] = snapshot["status"] == "running"
    return snapshot


def start_contacts(**kwargs) -> dict:
    """Run the contact sweep in the background, one at a time."""
    global _CONTACT_WORKER
    with _LOCK:
        if _CONTACT_WORKER and _CONTACT_WORKER.is_alive():
            return contact_state()
        _CONTACT_STATE.update(status="running", checked=0, found=0, failed=0,
                              finished_at=None)
        _CONTACT_WORKER = threading.Thread(
            target=harvest_contacts, kwargs=kwargs, daemon=True)
        _CONTACT_WORKER.start()
    return contact_state()


def _floor_text(value) -> str | None:
    """ONMAP stores the floor as {on_the, out_of}; render it as "1/8"."""
    if isinstance(value, dict):
        on, out = value.get("on_the"), value.get("out_of")
        if on is None:
            return None
        return f"{on}/{out}" if out is not None else str(on)
    return None if value is None else str(value)


def _parking_count(value) -> float | None:
    """Parking arrives as {aboveground, underground}, each a count or "none"."""
    if isinstance(value, dict):
        total = 0.0
        for slot in value.values():
            try:
                total += float(str(slot))
            except (TypeError, ValueError):
                continue
        return total or None
    return _number(value)


def _number(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: ONMAP localises the address into he/en/ru/fr blocks, each with
#: `city_name` / `neighborhood` / `street_name` / `house_number`. Hebrew is
#: preferred so localities join against the rest of PlanWatch, which is Hebrew
#: throughout; English is the fallback for rows where `he` is null.
def _part(address: dict, *names):
    """One component of the address, Hebrew first, English second."""
    for lang in ("he", "en"):
        block = address.get(lang)
        if not isinstance(block, dict):
            continue
        for name in names:
            value = block.get(name)
            if value not in (None, ""):
                return str(value).strip()
    return None


def _address_text(address: dict) -> str:
    """Readable address line: street, house number, neighbourhood, city."""
    street = _part(address, "street_name")
    house = _part(address, "house_number")
    parts = [" ".join(p for p in (street, house) if p),
             _part(address, "neighborhood", "neighbourhood"),
             _part(address, "city_name")]
    return ", ".join(p for p in parts if p)


def listing_url(slug: str | None, lat=None, lon=None) -> str | None:
    """The URL that actually opens an ONMAP ad.

    ONMAP has no per-listing page. The ad is a panel over the map search, and
    the slug alone does not open it: ``/search/homes/buy?property=<slug>`` just
    lands on the nationwide search. The panel appears only when the map is
    already looking at the listing, so the viewport goes in the path -
    ``/c_<centre>/t_<corner>/z_16`` - and the slug selects the ad within it.

    This was previously written as ``/en/properties/<slug>``, a route that does
    not exist on the site: every link answered ONMAP's own "אופס! הדף לא נמצא"
    page. Verified 2026-08-09 by navigating the live site and opening a stored
    slug at its stored coordinates.

    A row with no coordinates cannot open its panel, so it gets the search page
    with the slug attached - the wrong ad is never shown, and the link still
    lands somewhere real.
    """
    if not slug:
        return None
    base = "https://www.onmap.co.il/search/homes/buy"
    if lat is None or lon is None:
        return f"{base}?property={slug}"
    # `t_` is the viewport's far corner; a small offset gives a tight box
    # around the listing at z_16, which is close enough for the pin to be in
    # frame and for the panel to open.
    return (f"{base}/c_{float(lat):.6f},{float(lon):.6f}"
            f"/t_{float(lat) + 0.02:.6f},{float(lon) + 0.02:.6f}/z_16"
            f"?property={slug}")


def normalise(row: dict) -> dict | None:
    listing_id = row.get("id")
    if not listing_id:
        return None
    address = row.get("address") or {}
    info = row.get("additional_info") or {}
    location = address.get("location") or {}
    lat = lon = None
    if isinstance(location, dict):
        lat = _number(location.get("lat"))
        lon = _number(location.get("lon") or location.get("lng"))

    images = [img.get("full") or img.get("gallery") or img.get("thumbnail")
              for img in (row.get("images") or []) if isinstance(img, dict)]
    images = [u for u in images if u]
    slug = row.get("slug")
    return {
        "id": str(listing_id),
        "price": int(row["price"]) if str(row.get("price") or "").strip().isdigit()
                 else (int(_number(row.get("price")) or 0) or None),
        "currency": row.get("currency"),
        "property_type": row.get("property_type"),
        "city": _part(address, "city_name"),
        "neighborhood": _part(address, "neighborhood", "neighbourhood"),
        "street": _part(address, "street_name"),
        # Required to place the listing in a building at all - see
        # listing_seller.address_key.
        "house_number": _part(address, "house_number"),
        "address_text": _address_text(address),
        "rooms": _number(info.get("rooms")),
        # `area` and `floor` are objects, not numbers: area is
        # {base, garden, field} and floor is {on_the, out_of}. Reading them as
        # scalars is how these came back empty on the first pass.
        "area_sqm": _number((info.get("area") or {}).get("base")
                            if isinstance(info.get("area"), dict)
                            else info.get("area")),
        "floor": _floor_text(info.get("floor")),
        "bathrooms": _number(info.get("bathrooms")),
        "parking": _parking_count(info.get("parking")),
        "lat": lat, "lon": lon,
        "created_at": row.get("created_at"),
        "search_date": row.get("search_date"),
        "slug": slug,
        "url": listing_url(slug, lat, lon),
        "image": row.get("thumbnail") or (images[0] if images else None),
        "images": images,
        # ONMAP publishes the advertiser's number on the listing itself, on the
        # minority of ads whose owner chose to show it. It is the only source we
        # hold that gives a number without a second request, and it was being
        # thrown away with the rest of the raw payload.
        "phone": normalise_phone(row.get("phone_number")),
        "is_promoted": bool(row.get("is_promoted") or row.get("is_top_promoted")),
        "raw": row,
    }


COLUMNS = ("id", "price", "currency", "property_type", "city", "neighborhood",
           "street", "house_number", "address_text", "rooms", "area_sqm",
           "floor", "bathrooms", "parking", "lat", "lon", "created_at",
           "search_date", "slug", "url", "image", "phone")


def store(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    """Upsert normalised rows. Returns (seen, newly inserted)."""
    if not rows:
        return 0, 0
    now = _now()
    before = conn.execute("SELECT COUNT(*) FROM onmap_listings").fetchone()[0]
    payload = [(*(row[c] for c in COLUMNS),
                json.dumps(row["images"], ensure_ascii=False),
                1 if row["is_promoted"] else 0, now, now,
                json.dumps(row["raw"], ensure_ascii=False))
               for row in rows]
    conn.executemany(
        f"""INSERT INTO onmap_listings ({", ".join(COLUMNS)}, images_json,
                is_promoted, first_seen_at, last_seen_at, raw_json)
            VALUES ({", ".join("?" for _ in COLUMNS)}, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                {", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "id")},
                images_json=excluded.images_json,
                is_promoted=excluded.is_promoted,
                last_seen_at=excluded.last_seen_at,
                seen_count=onmap_listings.seen_count + 1,
                delisted_at=NULL,
                raw_json=excluded.raw_json""", payload)
    conn.executemany(
        """INSERT OR IGNORE INTO onmap_price_history (id, price, observed_at)
           SELECT ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM onmap_price_history h
                 WHERE h.id = ? AND h.price IS ?
                   AND h.observed_at = (SELECT MAX(observed_at)
                                          FROM onmap_price_history WHERE id = ?))""",
        [(r["id"], r["price"], now, r["id"], r["price"], r["id"]) for r in rows])
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM onmap_listings").fetchone()[0]
    return len(payload), after - before


_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_STATE: dict = {"status": "idle", "pages": 0, "rows_seen": 0, "rows_new": 0,
                "sort": None, "error": ""}


def state() -> dict:
    snapshot = dict(_STATE)
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        row = conn.execute(
            """SELECT COUNT(*) n, MIN(created_at) mn, MAX(created_at) mx,
                      COUNT(images_json) imgs
                 FROM onmap_listings WHERE delisted_at IS NULL""").fetchone()
        snapshot.update(stored=row["n"], oldest=row["mn"], newest=row["mx"],
                        with_images=row["imgs"])
    finally:
        conn.close()
    snapshot["running"] = snapshot["status"] == "running"
    return snapshot


def harvest(*, sorts=SORTS, max_skip: int = MAX_SKIP) -> dict:
    """Walk every ordering to its depth limit, unioning the results by id."""
    conn = db.get_conn()
    ensure_schema(conn)
    _STATE.update(status="running", pages=0, rows_seen=0, rows_new=0, error="")
    pages = seen = new = 0
    status, error = "complete", ""
    try:
        for sort in sorts:
            _STATE["sort"] = sort
            skip = 0
            while skip < max_skip:
                try:
                    payload = fetch_page(skip, sort)
                except OnmapError as exc:
                    error = str(exc)
                    break
                rows = payload.get("data")
                if not isinstance(rows, list) or not rows:
                    break
                normalised = [n for n in (normalise(r) for r in rows) if n]
                wave_seen, wave_new = store(conn, normalised)
                pages += 1
                seen += wave_seen
                new += wave_new
                _STATE.update(pages=pages, rows_seen=seen, rows_new=new)
                if not (payload.get("meta") or {}).get("hasNextPage", True):
                    break
                skip += PAGE_SIZE
    except Exception as exc:                        # pragma: no cover
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
        _STATE.update(status=status, error=error)
    return state()


def start(**kwargs) -> dict:
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return state()
        _WORKER = threading.Thread(target=harvest, kwargs=kwargs,
                                   name="onmap-harvest", daemon=True)
        _STATE.update(status="running", error="")
        _WORKER.start()
    return state()


def rows(limit: int | None = None) -> list[dict]:
    """Stored ONMAP listings, newest publication first."""
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        sql = ("""SELECT id, price, property_type, city, neighborhood, street,
                         house_number, address_text, rooms, area_sqm, floor,
                         lat, lon, created_at, url, image, images_json, phone,
                         contact_name, owner_property_count, motivation_flags,
                         first_seen_at, last_seen_at
                    FROM onmap_listings
                   WHERE delisted_at IS NULL
                   ORDER BY created_at DESC""")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def counts() -> dict:
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        # `enriched` and `last_contact` exist for one reason: the dashboard keys
        # its whole-corpus annotation cache on this dict. The contact sweep adds
        # phones, names and descriptions to *existing* rows, so it moves neither
        # the row count nor `created_at` - and the cache therefore never
        # invalidated. 2,510 harvested phones sat in the table while the table on
        # screen kept serving the annotation built before the sweep started, and
        # no amount of reloading brought them out. Any column the sweep writes
        # has to be visible in the fingerprint or the work stays invisible.
        row = conn.execute(
            """SELECT COUNT(*) total, MIN(created_at) first, MAX(created_at) last,
                      COUNT(NULLIF(TRIM(phone), '')) enriched, MAX(contact_checked_at) last_contact
                 FROM onmap_listings WHERE delisted_at IS NULL""").fetchone()
        return {"total": row["total"] or 0, "first_created": row["first"],
                "last_created": row["last"], "enriched": row["enriched"] or 0,
                "last_contact": row["last_contact"]}
    finally:
        conn.close()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Harvest ONMAP for-sale listings")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--max-skip", type=int, default=MAX_SKIP)
    args = parser.parse_args()
    if args.status:
        print(json.dumps(state(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(harvest(max_skip=args.max_skip), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
