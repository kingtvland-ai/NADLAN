"""Advertiser contact details for a stored Yad2 for-sale listing.

What this can and cannot do, measured rather than assumed
--------------------------------------------------------
Two routes were probed against the live services on 2026-08-05:

``yad2_item``
    The ad's own page, ``yad2.co.il/realestate/item/<token>``, which is where
    the advertiser's own number lives. **Six** routes were tried on 2026-08-05
    and every one is blocked:

    1. the local curl2api connector, default settings;
    2. the connector carrying the working board definition's 18 cookies;
    3. the same, with ``networkidle`` + human-mouse + full asset loading;
    4. the same again after forcing a **live cookie refresh** through
       ``POST /api/_cookies/refresh``, seconds before the request;
    5. the Playwright persistent profile in ``backend/``;
    6. ``gw.yad2.co.il/realestate-feed/item/<token>/{,contact-info,phone}``
       from inside that browser, with its own session cookies.

    1-5 return a Radware / ShieldSquare interstitial ("Verifying your
    browser…"); 6 returns 404 behind the same shield. Notably the *board* page
    loads fine with those same cookies - Yad2 protects item pages harder than
    listing pages. Two adjacent sources were checked and are also dead ends:
    the agency directory (``yad2_agencies``) publishes only id/name/logo/link,
    and the sitemap index (42 sub-sitemaps) contains region and city search
    pages, never individual ads.

    The route stays implemented and will start returning numbers the moment the
    profile holds a passing session again (run ``npm run login`` in
    ``backend/``). Until then it reports the block rather than an empty cell.

``google_places``
    The *agency's* published business number, looked up by the agency name that
    every harvested row already carries (317/317 rows in the current store, 233
    distinct agencies). This is a business's own published contact detail from
    an official API, which is a different and far safer thing than harvesting a
    private seller's mobile. It needs ``GOOGLE_MAPS_API_KEY``.

Rules this module keeps
-----------------------
- **On demand only.** There is no bulk sweep and no scheduled job. A number is
  fetched when a user opens one listing and asks, which is what section 10 of
  the business plan ("מידע אישי… יוצג רק כאשר יש בסיס חוקי והרשאה מתאימה")
  requires, and it is also what stops this becoming a scraped phone book.
- **Never invented.** If no route yields a number the answer is
  ``{"available": false, "reason": ...}``. A blank is a fact about our
  coverage; a guessed number is a wrong call to a stranger.
- **Provenance always.** Every stored number keeps its source, the exact query
  that produced it, and when it was retrieved, so the UI can show confidence
  the way every other PlanWatch figure does.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS yad2_listing_contacts (
    token         TEXT PRIMARY KEY,
    agency        TEXT,
    phone         TEXT,
    phone_source  TEXT,
    confidence    TEXT,
    website       TEXT,
    matched_name  TEXT,
    query         TEXT,
    retrieved_at  TEXT NOT NULL,
    unavailable   TEXT,
    raw_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_yad2_contacts_agency
    ON yad2_listing_contacts (agency);
"""

#: Israeli landline and mobile, with the separators people actually type.
PHONE_RE = re.compile(r"\b0(?:5\d|[2-4]|[8-9]|7\d)[-. ]?\d{3}[-. ]?\d{4}\b")

#: How long a resolved contact is reused before a new lookup is allowed. Agency
#: numbers move rarely and every lookup costs a paid API call, so a week is
#: generous without being stale enough to misdirect a call.
CACHE_SECONDS = 7 * 24 * 3600


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_phone(value: str | None) -> str | None:
    """A phone as digits with a leading zero, or None if it is not one."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("972"):
        digits = "0" + digits[3:]
    if not 9 <= len(digits) <= 10 or not digits.startswith("0"):
        return None
    return digits


def _listing(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        """SELECT token, agency, city, ad_type, street, neighborhood
             FROM yad2_listings WHERE token = ?""", (str(token),)).fetchone()
    return dict(row) if row else None


def _cached(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM yad2_listing_contacts WHERE token = ?", (str(token),)
    ).fetchone()
    if not row:
        return None
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(row["retrieved_at"])).total_seconds()
    except (ValueError, TypeError):
        age = 0
    if age > CACHE_SECONDS:
        return None
    return dict(row)


# ------------------------------------------------------------------- routes

def _from_google_places(agency: str, city: str) -> dict:
    """The agency's published business number, via the official Places API."""
    import google_places_client as places

    if not places.enabled():
        return {"available": False, "source": "google_places",
                "reason": "GOOGLE_MAPS_API_KEY לא מוגדר — חיפוש טלפון של סוכנות כבוי"}
    query = " ".join(filter(None, (agency, city, "ישראל")))
    try:
        result = places.search_text(query, page_size=5)
    except Exception as exc:
        return {"available": False, "source": "google_places",
                "reason": f"Google Places נכשל: {exc}", "query": query}

    wanted = _squash(agency)
    for place in result.get("places") or []:
        phone = normalise_phone(place.get("phone"))
        if not phone:
            continue
        name = _squash(place.get("name"))
        # An exact-ish name match is "verified"; a first hit that merely came
        # back for the query is "derived". The plan (section ד) asks for the
        # certainty level to travel with the classification, not be implied.
        exact = wanted and (wanted in name or name in wanted)
        return {"available": True, "source": "google_places", "phone": phone,
                "confidence": "verified" if exact else "derived",
                "matched_name": place.get("name"),
                "website": place.get("website"), "query": query,
                "raw": place}
    return {"available": False, "source": "google_places", "query": query,
            "reason": "לא נמצא מספר עסקי מפורסם לסוכנות הזו"}


def _from_yad2_item(token: str) -> dict:
    """The advertiser's own number from the ad page, through the connector.

    Kept because it is the *right* source when the session is valid; it simply
    reports the block instead of pretending, so the UI can tell the user what
    to fix rather than showing an empty cell forever.
    """
    import urllib.error
    import urllib.request

    import yad2_feed

    url = f"{yad2_feed.API_BASE}/api/yad2_item_contact?token={token}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"available": False, "source": "yad2_item",
                    "reason": ("במחבר המקומי אין הגדרת yad2_item_contact. "
                               "עמוד המודעה חסום כרגע ב-Radware; יש לחדש את "
                               "פרופיל הדפדפן (backend: npm run login).")}
        return {"available": False, "source": "yad2_item",
                "reason": f"המחבר החזיר HTTP {exc.code}"}
    except Exception as exc:
        return {"available": False, "source": "yad2_item",
                "reason": f"{type(exc).__name__}: {exc}"}

    rows = payload.get("data") if isinstance(payload, dict) else payload
    text = json.dumps(rows, ensure_ascii=False)
    if "Radware" in text or "ShieldSquare" in text or "perfdrive" in text:
        return {"available": False, "source": "yad2_item",
                "reason": ("עמוד המודעה חזר כדף אימות בוטים. "
                           "חדש את פרופיל הדפדפן: backend → npm run login.")}
    for candidate in PHONE_RE.findall(text):
        phone = normalise_phone(candidate)
        if phone:
            return {"available": True, "source": "yad2_item", "phone": phone,
                    "confidence": "verified", "query": url}
    return {"available": False, "source": "yad2_item",
            "reason": "לא נמצא מספר בעמוד המודעה"}


def _squash(value) -> str:
    return re.sub(r"[\s\-־'\"״׳,.]+", "", str(value or "")).lower()


#: Resolution order. The advertiser's own number first, because it is the one
#: an agent actually wants; the agency switchboard is the fallback.
ROUTES = ("yad2_item", "google_places")


def resolve(token: str, *, refresh: bool = False) -> dict:
    """Best available contact for ONE listing. Never bulk, never invented."""
    token = str(token or "").strip()
    if not token:
        raise ValueError("token is required")

    conn = db.get_conn()
    try:
        import yad2_feed
        yad2_feed.ensure_schema(conn)
        ensure_schema(conn)

        listing = _listing(conn, token)
        if not listing:
            return {"token": token, "available": False,
                    "reason": "המודעה אינה במאגר שנמשך. הרץ טעינת יד 2 קודם."}

        if not refresh:
            cached = _cached(conn, token)
            if cached:
                return _present(cached, listing, cached=True)

        attempts = []
        resolved = None
        for route in ROUTES:
            if route == "yad2_item":
                outcome = _from_yad2_item(token)
            else:
                outcome = _from_google_places(listing.get("agency") or "",
                                              listing.get("city") or "")
            attempts.append({"source": outcome.get("source", route),
                             "available": outcome.get("available", False),
                             "reason": outcome.get("reason")})
            if outcome.get("available"):
                resolved = outcome
                break

        now = _now()
        conn.execute(
            """INSERT INTO yad2_listing_contacts
                   (token, agency, phone, phone_source, confidence, website,
                    matched_name, query, retrieved_at, unavailable, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(token) DO UPDATE SET
                   agency=excluded.agency, phone=excluded.phone,
                   phone_source=excluded.phone_source,
                   confidence=excluded.confidence, website=excluded.website,
                   matched_name=excluded.matched_name, query=excluded.query,
                   retrieved_at=excluded.retrieved_at,
                   unavailable=excluded.unavailable, raw_json=excluded.raw_json""",
            (token, listing.get("agency"),
             (resolved or {}).get("phone"), (resolved or {}).get("source"),
             (resolved or {}).get("confidence"), (resolved or {}).get("website"),
             (resolved or {}).get("matched_name"), (resolved or {}).get("query"),
             now,
             None if resolved else json.dumps(attempts, ensure_ascii=False),
             json.dumps((resolved or {}).get("raw") or {}, ensure_ascii=False)))
        conn.commit()

        record = {"token": token, "agency": listing.get("agency"),
                  "phone": (resolved or {}).get("phone"),
                  "phone_source": (resolved or {}).get("source"),
                  "confidence": (resolved or {}).get("confidence"),
                  "website": (resolved or {}).get("website"),
                  "matched_name": (resolved or {}).get("matched_name"),
                  "retrieved_at": now}
        out = _present(record, listing, cached=False)
        out["attempts"] = attempts
        return out
    finally:
        conn.close()


def _present(record: dict, listing: dict, *, cached: bool) -> dict:
    """Shape one contact for the UI, always carrying its provenance."""
    phone = record.get("phone")
    attempts = record.get("unavailable")
    if isinstance(attempts, str):
        try:
            attempts = json.loads(attempts)
        except ValueError:
            attempts = None
    return {
        "token": record.get("token") or listing.get("token"),
        "available": bool(phone),
        "phone": phone,
        "phone_source": record.get("phone_source"),
        "confidence": record.get("confidence"),
        "agency": record.get("agency") or listing.get("agency"),
        "matched_name": record.get("matched_name"),
        "website": record.get("website"),
        "ad_url": f"https://www.yad2.co.il/realestate/item/{listing.get('token')}",
        "retrieved_at": record.get("retrieved_at"),
        "from_cache": cached,
        "attempts": attempts,
        "note": ("מספר עסקי מפורסם של הסוכנות, לא של בעל הנכס."
                 if record.get("phone_source") == "google_places" else
                 ("מספר מתוך המודעה עצמה."
                  if phone else
                  "לא הושג מספר. פנייה דרך קישור המודעה.")),
        "policy": ("איתור נעשה לפי מודעה בודדת ולפי בקשה מפורשת בלבד. "
                   "אין איסוף מרוכז של מספרי טלפון."),
    }


def coverage() -> dict:
    """How much of the store has a contact, for the sources/quality screen."""
    conn = db.get_conn()
    try:
        import yad2_feed
        yad2_feed.ensure_schema(conn)
        ensure_schema(conn)
        listings = conn.execute(
            "SELECT COUNT(*) FROM yad2_listings WHERE delisted_at IS NULL"
        ).fetchone()[0]
        row = conn.execute(
            """SELECT COUNT(*) attempted,
                      SUM(CASE WHEN phone IS NOT NULL THEN 1 ELSE 0 END) resolved
                 FROM yad2_listing_contacts""").fetchone()
        import google_places_client as places
        return {"listings": listings, "attempted": row["attempted"] or 0,
                "resolved": row["resolved"] or 0,
                "google_places_enabled": places.enabled(),
                "routes": list(ROUTES)}
    finally:
        conn.close()
