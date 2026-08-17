"""Time-on-market maths and lead characterisation for for-sale listings.

The question this answers
------------------------
"Which of these sellers actually wants to sell?" A board tells you what is for
sale; it does not tell you who is tired of being on it. That difference is the
whole product argument in section 5 of the business plan ("גרף חיים של מודעה…
יכול לזהות נכס שמתקרב לפשרה לפני שהשוק כולו מבחין בו"), and it is computable
from data PlanWatch already owns, with no new integration.

The three inputs
----------------
1. **Days on market.** How long the ad has been observable. Every figure here
   carries an ``age_basis`` and an ``age_confidence`` because the honest answer
   differs per source and the difference matters enormously:

   - ``published`` - the seller's own publication date (``listed_at``). True
     time on market. Madlan only, ~1,400 rows.
   - ``image``     - the date encoded in the listing's photo URL (see
     ``yad2_feed.image_date``). Yad2 publishes no posting date anywhere, but
     every image URL carries its upload timestamp, in two independent places
     that agree on 100% of 14,445 rows, spanning 2014-2026. It proves the ad
     existed by then, so it is a lower bound - and a far better one than our
     own scraping window.
   - ``observed``  - PlanWatch's own ``first_seen``. The weakest floor; it says
     more about when we started scraping than about the ad. AD and Komo only,
     whose stored payloads carry no date at all (checked: 0 of 300 rows each).

   Together these date ~82% of the population with real evidence instead of
   7%. A floor is never presented as if it were exact: a listing first seen
   yesterday is "at least 1 day", not "1 day" - reading the floor as the truth
   would rank a long-stale ad as brand new and send an agent in with exactly
   the wrong opening.

2. **Price movement.** Every observed change, from ``yad2_price_history`` for
   Yad2 rows and ``listing_price_history`` for the legacy feed. A cut is the
   single strongest published signal that a seller has moved; the *size* and
   the *number* of cuts separate a nudge from a capitulation.

3. **Market position.** The ₪/m² gap from the neighbourhood (or city) median,
   which the listings view already computes. Distance from the local benchmark
   is what says whether a long-standing ad is stubborn or simply overpriced.

Why a score AND a type
----------------------
A score alone tells an agent who to call first but not what to say. The
``lead_type`` names the situation, and ``signals`` lists the facts that produced
it, because the plan is explicit that a score without its reason must not be
shown (section 5: "אין להציג ציון ללא הסבר"). Every row therefore carries the
evidence that moved it, in the user's language.

This is a ranking aid built on asking prices and our own observation window. It
is not a valuation, and the confidence fields exist so nobody mistakes it for
one.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
from collections import defaultdict
from datetime import datetime, timezone

import db

SCHEMA = """
/* One row per listing per day it was observed with a given price. This is the
 * "graph of an ad's life" the plan asks for: the union of what we harvest and
 * what we can carry forward, in PlanWatch's own database so it keeps growing
 * even when an upstream feed forgets. */
CREATE TABLE IF NOT EXISTS listing_lead_notes (
    listing_key TEXT PRIMARY KEY,
    stage       TEXT,
    owner       TEXT,
    note        TEXT,
    next_action TEXT,
    due_date    TEXT,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lead_notes_stage ON listing_lead_notes (stage);
"""

#: CRM stages, in the order the plan lists them (section 3א). Held here so the
#: API can reject a typo instead of silently storing a stage nothing filters on.
STAGES = ("חדש", "נבדק", "פנייה לבעלים", "פגישה", "הצעה", "נסגר", "לא רלוונטי")

#: Day thresholds for time on market. Chosen against Israeli residential norms:
#: a listing under a month is still enjoying its first wave of interest, three
#: months is where sellers typically start negotiating, and past six months the
#: ad is not selling at its current price.
FRESH_DAYS = 30
SEASONED_DAYS = 90
STALE_DAYS = 180

#: A price cut this deep is a decision rather than a rounding tweak.
MEANINGFUL_CUT_PCT = 3.0

#: Gap from the local median past which a listing is flagged for verification
#: rather than celebrated. Matches the same rule in external_listings_view, so
#: the listings table and the lead ranking never disagree about one listing.
EXTREME_GAP_PCT = 50.0

#: Comparable listings required before a ₪/m² median is treated as a benchmark.
#: Under this the "median" is the row itself and reports a 0% gap, which reads
#: as "priced at market" when it means "nothing to compare with".
MIN_COMPARABLES = 3

#: Plausibility envelope for a home for sale, matching
#: ``external_listings_view.PLAUSIBLE`` so both views call the same rows broken.
#:
#: This is not fussiness. Measured on the current store, rows arrive at ₪10/m²
#: and ₪67/m² - a price recorded in thousands, or an area field that arrived as
#: 1.0. Every one of them scores a 99.9% gap from its neighbourhood median, and
#: they took the entire top of a discount-sorted list: the first five results an
#: agent would see were all garbage. They are still shown and still counted -
#: hiding a broken record is how you stop noticing the feed is broken - but they
#: are labelled and they never earn a rank.
PLAUSIBLE = {
    "price": (100_000, 50_000_000),
    "area_m2": (15, 1_000),
    "price_per_m2": (2_000, 250_000),
}


def implausible(row) -> str | None:
    """Why this row cannot be a home for sale, or None if it can."""
    for field, (low, high) in PLAUSIBLE.items():
        value = row.get(field)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value < low:
            return f"{field} נמוך מדי ({value:g})"
        if value > high:
            return f"{field} גבוה מדי ({value:g})"
    return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    """Parse the several timestamp shapes the feeds use, or return None."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for candidate in (text, text[:19], text[:10]):
        try:
            stamp = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return None


def _days_since(value) -> int | None:
    stamp = _parse(value)
    if stamp is None:
        return None
    return max(0, (_now() - stamp).days)


# ------------------------------------------------------------ time on market

def market_age(*, listed_at=None, image_date=None, first_seen=None) -> dict:
    """Days on market, always paired with the basis it rests on.

    Three sources, in descending order of authority:

    ``listed_at``   the seller's own publication date. A true age.
    ``image_date``  the date encoded in the listing's photo URL (see
                    ``yad2_feed.image_date``). Proves the ad's photo existed by
                    then, so the ad is *at least* that old. Present on 100% of
                    Yad2 rows and reaching back to 2014, which is the only
                    reason this product can talk about time on market at all -
                    our own observation window is days old.
    ``first_seen``  when PlanWatch first saw it. The weakest floor: it says
                    more about when we started scraping than about the ad.

    Whichever is used, ``age_is_minimum`` says whether the number is exact or a
    lower bound, and the label says so in words. That distinction is the whole
    point: reading a floor as the truth ranks a long-stale ad as brand new and
    sends an agent in with precisely the wrong opening line.
    """
    published = _days_since(listed_at)
    if published is not None:
        return {"days_on_market": published, "age_basis": "published",
                "age_confidence": "high", "age_is_minimum": False,
                "age_label": f"{published} ימים בשוק"}
    from_image = _days_since(image_date)
    if from_image is not None:
        return {"days_on_market": from_image, "age_basis": "image",
                "age_confidence": "medium", "age_is_minimum": True,
                "age_label": f"לפחות {from_image} ימים — לפי תאריך התמונה"}
    observed = _days_since(first_seen)
    if observed is not None:
        return {"days_on_market": observed, "age_basis": "observed",
                "age_confidence": "partial", "age_is_minimum": True,
                "age_label": f"לפחות {observed} ימים במעקב"}
    return {"days_on_market": None, "age_basis": "unknown",
            "age_confidence": "none", "age_is_minimum": False,
            "age_label": "אין תאריך"}


def age_band(days: int | None) -> str:
    if days is None:
        return "לא ידוע"
    if days < FRESH_DAYS:
        return "טרי"
    if days < SEASONED_DAYS:
        return "מתבשל"
    if days < STALE_DAYS:
        return "תקוע"
    return "תקוע מאוד"


# --------------------------------------------------------------- price moves

def price_movement(history: list[tuple]) -> dict:
    """Summarise an ordered [(price, observed_at), ...] log for one listing."""
    points = [(int(price), _parse(when))
              for price, when in history if price is not None]
    points = [(price, when) for price, when in points if when is not None]
    points.sort(key=lambda item: item[1])
    if len(points) < 2:
        return {"price_changes": 0, "total_change_pct": None,
                "last_change_pct": None, "days_since_change": None,
                "largest_cut_pct": None, "direction": "ללא שינוי נצפה",
                "observations": len(points)}

    first_price, last_price = points[0][0], points[-1][0]
    total_pct = (last_price - first_price) / first_price * 100 if first_price else None
    prev_price, prev_when = points[-2]
    last_pct = (last_price - prev_price) / prev_price * 100 if prev_price else None
    cuts = [(points[i][0] - points[i - 1][0]) / points[i - 1][0] * 100
            for i in range(1, len(points)) if points[i - 1][0]]
    largest_cut = min(cuts) if cuts else None
    direction = ("ירידת מחיר" if total_pct and total_pct < 0
                 else "עליית מחיר" if total_pct and total_pct > 0
                 else "ללא שינוי")
    return {
        "price_changes": len(points) - 1,
        "total_change_pct": round(total_pct, 1) if total_pct is not None else None,
        "last_change_pct": round(last_pct, 1) if last_pct is not None else None,
        "days_since_change": max(0, (_now() - points[-1][1]).days),
        "largest_cut_pct": round(largest_cut, 1) if largest_cut is not None else None,
        "direction": direction,
        "observations": len(points),
    }


# ------------------------------------------------------------------- scoring

#: Points a signal contributes to the motivation score. The weights are a
#: starting hypothesis, exposed in the API response so a pilot can argue with
#: them rather than reverse-engineer them.
WEIGHTS = {
    "price_cut": 30,        # a published cut - the strongest single signal
    "repeat_cuts": 15,      # more than one cut: a pattern, not a test
    "time_on_market": 25,   # how long it has failed to sell
    "below_market": 15,     # already priced under the local benchmark
    "private_seller": 10,   # no agency between you and the decision maker
    "large_asset": 5,       # upsize/downsize pressure tends to be real
    "parcel_match": 8,      # precise parcel cross-link
    "appraisal_activity": 6, # legal/valuation activity on the parcel
    "tender_history": 6,    # state land sale signal
    "pre_1980": 5,          # TAMA 38 eligibility
    "owner_density": 4,     # multiple rights-holder traces on the parcel
    "building_cluster": 6,  # multiple same-building listings
    "private_cluster": 6,   # multiple private sellers at same address
    "phone_available": 2,   # published contact exists
    "paid_vs_asking": 8,    # asking vs concluded-sale disagreement
    "motivation_text": 5,   # seller states urgency in the ad text
    "fresh_listing": 3,     # new ad worth calling early
}


def score_lead(*, age: dict, movement: dict, discount_pct=None,
               is_private=False, rooms=None) -> dict:
    """Motivation score 0-100 with the signals that produced it.

    Every contribution is recorded in ``signals``. A caller can therefore show
    the reason next to the number, which is a hard requirement of the plan and
    also the only way an agent trusts the ranking enough to act on it.
    """
    score = 0.0
    signals: list[dict] = []

    cut = movement.get("total_change_pct")
    if cut is not None and cut <= -MEANINGFUL_CUT_PCT:
        weight = WEIGHTS["price_cut"] * min(1.0, abs(cut) / 10.0)
        score += weight
        signals.append({"signal": "price_cut", "points": round(weight, 1),
                        "text": f"המחיר ירד ב-{abs(cut):.1f}% מאז שהתחלנו לעקוב"})
    if (movement.get("price_changes") or 0) >= 2:
        score += WEIGHTS["repeat_cuts"]
        signals.append({"signal": "repeat_cuts",
                        "points": WEIGHTS["repeat_cuts"],
                        "text": f"{movement['price_changes']} שינויי מחיר — המוכר מנסה למצוא מחיר"})

    days = age.get("days_on_market")
    if days is not None:
        # Saturates at STALE_DAYS: past six months, longer stops being more
        # informative - the ad is simply not selling at this price.
        weight = WEIGHTS["time_on_market"] * min(1.0, days / STALE_DAYS)
        if days >= FRESH_DAYS:
            score += weight
            prefix = "לפחות " if age.get("age_is_minimum") else ""
            signals.append({"signal": "time_on_market", "points": round(weight, 1),
                            "text": f"{prefix}{days} ימים בשוק ({age_band(days)})"})

    if discount_pct is not None and discount_pct > 5:
        weight = WEIGHTS["below_market"] * min(1.0, discount_pct / 20.0)
        score += weight
        signals.append({"signal": "below_market", "points": round(weight, 1),
                        "text": f"מתומחר {discount_pct:.1f}% מתחת לחציון באזור"})

    if is_private:
        score += WEIGHTS["private_seller"]
        signals.append({"signal": "private_seller",
                        "points": WEIGHTS["private_seller"],
                        "text": "מודעה ללא תיווך — פנייה ישירה לבעלים"})

    if rooms is not None and rooms >= 5:
        score += WEIGHTS["large_asset"]
        signals.append({"signal": "large_asset", "points": WEIGHTS["large_asset"],
                        "text": f"{rooms:g} חדרים — לרוב מהלך של החלפת דירה"})

    return {"lead_score": round(min(100.0, score), 1), "signals": signals}


def extra_signals(row: dict, age: dict, movement: dict, discount_pct=None,
                  paid: dict | None = None) -> tuple[float, list[dict]]:
    """Cross-source lead formulas that reward real joins, not guesswork."""
    bonus = 0.0
    signals: list[dict] = []

    if row.get("gush") and row.get("helka"):
        bonus += WEIGHTS["parcel_match"]
        signals.append({"signal": "parcel_match", "points": WEIGHTS["parcel_match"],
                        "text": f"גוש {row['gush']} חלקה {row['helka']} — זיהוי חלקה מלא"})
    if row.get("appraisal_count"):
        pts = min(WEIGHTS["appraisal_activity"], 2 + int(row["appraisal_count"]) / 2)
        bonus += pts
        signals.append({"signal": "appraisal_activity", "points": round(pts, 1),
                        "text": f"{row['appraisal_count']} הכרעות שמאות על החלקה"})
    if row.get("tender_winning_price"):
        bonus += WEIGHTS["tender_history"]
        signals.append({"signal": "tender_history", "points": WEIGHTS["tender_history"],
                        "text": f"רמ\"י מכרה כאן ב-{int(row['tender_winning_price']):,} ₪"})
    if row.get("building_pre_1980"):
        bonus += WEIGHTS["pre_1980"]
        signals.append({"signal": "pre_1980", "points": WEIGHTS["pre_1980"],
                        "text": "בניין לפני 1980 — מתאים לבדיקה לתמ\"א 38"})
    if row.get("owner_count"):
        pts = min(WEIGHTS["owner_density"], row["owner_count"])
        bonus += pts
        signals.append({"signal": "owner_density", "points": round(pts, 1),
                        "text": f"{row['owner_count']} רמזי בעלים על החלקה"})
    if (row.get("address_listings") or 0) >= 2:
        bonus += WEIGHTS["building_cluster"]
        signals.append({"signal": "building_cluster", "points": WEIGHTS["building_cluster"],
                        "text": f"{row['address_listings']} מודעות באותה כתובת"})
    if (row.get("address_private_listings") or 0) >= 2:
        bonus += WEIGHTS["private_cluster"]
        signals.append({"signal": "private_cluster", "points": WEIGHTS["private_cluster"],
                        "text": f"{row['address_private_listings']} מוכרים פרטיים באותו בניין"})
    if row.get("phone"):
        bonus += WEIGHTS["phone_available"]
        signals.append({"signal": "phone_available", "points": WEIGHTS["phone_available"],
                        "text": "מספר טלפון מפורסם וזמין"})
    if row.get("motivation_flags"):
        pts = min(WEIGHTS["motivation_text"], 1.5 * len(row["motivation_flags"]))
        bonus += pts
        signals.append({"signal": "motivation_text", "points": round(pts, 1),
                        "text": "המודעה עצמה מציינת סיבה למכירה"})
    if age.get("days_on_market") is not None and age.get("days_on_market") < FRESH_DAYS:
        bonus += WEIGHTS["fresh_listing"]
        signals.append({"signal": "fresh_listing", "points": WEIGHTS["fresh_listing"],
                        "text": "מודעה טרייה — חלון תגובה מוקדם"})
    if paid and paid.get("available"):
        if paid.get("gap_vs_paid_pct") is not None:
            gap = abs(float(paid["gap_vs_paid_pct"]))
            pts = min(WEIGHTS["paid_vs_asking"], gap / 8)
            bonus += pts
            signals.append({"signal": "paid_vs_asking", "points": round(pts, 1),
                            "text": f"פער מול עסקאות שבוצעו: {paid['gap_vs_paid_pct']:+.0f}%"})

    return bonus, signals


def classify_lead(*, age: dict, movement: dict, discount_pct=None,
                  is_private=False, rooms=None, price=None) -> dict:
    """Name the situation behind the score, and the opening it implies.

    The three goals named in the brief map onto three distinct patterns:

    - **מוכר כדי לקנות** - a large home, on the market a while, whose owner has
      moved on price. That combination is an upsize/downsize chain, and the
      seller's own deadline is the lever.
    - **הזדמנות השקעה** - priced under the local benchmark with time on market
      behind it. The gap is the return; the age is why it is still available.
    - **ליד טרי** - new to the board and untested. Worth a call precisely
      because nobody else has made it yet.
    """
    days = age.get("days_on_market") or 0
    cut = movement.get("total_change_pct")
    has_cut = cut is not None and cut <= -MEANINGFUL_CUT_PCT
    big_home = rooms is not None and rooms >= 4.5

    if has_cut and days >= SEASONED_DAYS:
        return {"lead_type": "לחוץ למכור",
                "lead_reason": "הוריד מחיר אחרי תקופה ארוכה בשוק",
                "suggested_action": "פנייה ישירה עם הצעה מנומקת — המוכר כבר זז במחיר"}
    if has_cut and big_home:
        return {"lead_type": "מוכר כדי לקנות",
                "lead_reason": "דירה גדולה שירדה במחיר — לרוב שרשרת החלפת דירה",
                "suggested_action": "לברר לוח זמנים ולהציע גם נכס חלופי לרכישה"}
    if discount_pct is not None and discount_pct >= 10 and days >= FRESH_DAYS:
        return {"lead_type": "הזדמנות השקעה",
                "lead_reason": f"מתחת לחציון האזור ב-{discount_pct:.0f}% ועדיין לא נמכר",
                "suggested_action": "לאמת שטח, מצב פיזי וזכויות לפני שמניחים שזו מציאה"}
    if days >= STALE_DAYS:
        return {"lead_type": "תקוע בשוק",
                "lead_reason": "זמן רב בשוק ללא שינוי מחיר",
                "suggested_action": "להציע בדיקת תמחור מחדש — זו פתיחה לשיחת בלעדיות"}
    if is_private and days >= FRESH_DAYS:
        return {"lead_type": "בעלים פרטי",
                "lead_reason": "מפרסם עצמאית מעל חודש",
                "suggested_action": "פנייה לבעלים — עדיין לא נמצא קונה לבד"}
    if days < FRESH_DAYS:
        return {"lead_type": "ליד טרי",
                "lead_reason": "חדש בשוק",
                "suggested_action": "לפנות מוקדם, לפני שהמודעה נשחקת"}
    return {"lead_type": "למעקב",
            "lead_reason": "אין עדיין אות מובהק",
            "suggested_action": "להשאיר במעקב ולבדוק שוב אחרי שינוי מחיר"}


# ------------------------------------------------------------- data assembly

def _yad2_rows(conn: sqlite3.Connection) -> list[dict]:
    from ingestion.feeds import yad2_feed
    yad2_feed.ensure_schema(conn)

    history: dict[str, list[tuple]] = defaultdict(list)
    for row in conn.execute(
            "SELECT token, price, observed_at FROM yad2_price_history"):
        history[row["token"]].append((row["price"], row["observed_at"]))

    rows = []
    for row in conn.execute(
            """SELECT token, price, rooms, sqm, city, neighborhood, street,
                      agency, ad_type, property_type, first_seen_at,
                      last_seen_at, seen_count, image, image_date,
                      house_number, images_json
                 FROM yad2_listings WHERE delisted_at IS NULL"""):
        item = dict(row)
        ppm = (float(item["price"]) / float(item["sqm"])
               if item.get("price") and item.get("sqm") else None)
        rows.append({
            "listing_key": f"yad2:{item['token']}",
            "source": "Yad2", "external_id": item["token"],
            "url": f"https://www.yad2.co.il/realestate/item/{item['token']}",
            "locality": item.get("city"), "neighborhood": item.get("neighborhood"),
            "street": item.get("street"), "price": item.get("price"),
            "rooms": item.get("rooms"), "area_m2": item.get("sqm"),
            "price_per_m2": round(ppm) if ppm else None,
            "agency": item.get("agency"),
            "property_type": item.get("property_type"),
            "house_number": item.get("house_number"),
            # Parsed here rather than shipped as a JSON string: the UI opens a
            # gallery from it, and making every caller re-parse invites one of
            # them to forget and render "[object Object]".
            "images": _json_list(item.get("images_json")),
            # `ad_type` is Yad2's OWN word for who is selling, so both answers
            # here are stated fact rather than inference. It only became
            # available once the harvest moved to the real search feed: the
            # promoted-ads carousel labelled all 14,445 of its rows
            # "commercial", which is why this used to be unable to produce a
            # single private lead.
            "brokerage": ("private" if item.get("ad_type") == "private"
                          else "agency" if (item.get("agency") or "").strip()
                          or item.get("ad_type") == "commercial"
                          else "unknown"),
            # Yad2 publishes ad_type itself, so both answers derived from it
            # are the source's own statement - not our inference from a blank
            # field. The legacy feeds have no such field and stay "derived".
            "brokerage_verified": item.get("ad_type") in ("private", "commercial"),
            # Yad2 publishes no seller-supplied date. What it does publish,
            # inadvertently, is the photo's upload date inside the image URL -
            # which reaches back to 2014 and covers every row. See
            # yad2_feed.image_date.
            "listed_at": None,
            "image_date": item.get("image_date"),
            "image": item.get("image"),
            "first_seen": item.get("first_seen_at"),
            "last_seen": item.get("last_seen_at"),
            "history": history.get(item["token"], []),
        })
    return rows


def _json_list(value):
    """A stored JSON array as a Python list, or None. Never raises."""
    if not value:
        return None
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _brokerage_tables():
    """The shared brokerage vocabulary, imported lazily to avoid a cycle.

    The classifier itself lives in ``external_listings_view`` - the lower-level
    module that both the listings table and this one already depend on - so the
    two screens can never disagree about whether a given listing has a broker.
    """
    from ingestion.feeds import external_listings_view as external
    return external.BROKERAGE_LABELS, external.BROKERAGE_CONFIDENCE


def _legacy_brokerage(raw_json) -> str:
    """Brokerage state for a legacy row. Delegates to the shared classifier."""
    from ingestion.feeds import external_listings_view as external
    return external.brokerage_state(raw_json)


def _onmap_rows(conn: sqlite3.Connection) -> list[dict]:
    """ONMAP listings, which are the only source with a true publication date.

    Yad2 publishes none (hence the image-URL inference) and AD/Komo store no
    date at all. ONMAP's ``created_at`` is the seller's own timestamp, so these
    rows get ``age_basis="published"`` - an exact age rather than a floor.
    """
    from ingestion.feeds import onmap_feed

    onmap_feed.ensure_schema(conn)
    history: dict[str, list[tuple]] = defaultdict(list)
    for row in conn.execute(
            "SELECT id, price, observed_at FROM onmap_price_history"):
        history[row["id"]].append((row["price"], row["observed_at"]))

    rows = []
    for row in conn.execute(
            """SELECT id, price, property_type, city, neighborhood, street,
                      address_text, rooms, area_sqm, floor, url, image,
                      images_json, created_at, first_seen_at, last_seen_at
                 FROM onmap_listings WHERE delisted_at IS NULL"""):
        item = dict(row)
        ppm = (float(item["price"]) / float(item["area_sqm"])
               if item.get("price") and item.get("area_sqm") else None)
        rows.append({
            "listing_key": f"onmap:{item['id']}",
            "source": "onmap", "external_id": item["id"],
            "url": item.get("url"),
            "locality": item.get("city"),
            "neighborhood": item.get("neighborhood"),
            "street": item.get("street"),
            "price": item.get("price"), "rooms": item.get("rooms"),
            "area_m2": item.get("area_sqm"),
            "price_per_m2": round(ppm) if ppm else None,
            "property_type": item.get("property_type"),
            "agency": None,
            # ONMAP does not publish who is selling, so this stays honestly
            # unknown rather than being guessed either way.
            "brokerage": "unknown", "brokerage_verified": False,
            "listed_at": item.get("created_at"),
            "image_date": None, "image": item.get("image"),
            "images": _json_list(item.get("images_json")),
            "house_number": None,
            "first_seen": item.get("first_seen_at"),
            "last_seen": item.get("last_seen_at"),
            "history": history.get(item["id"]) or
                       [(item["price"], item.get("created_at"))],
        })
    return rows


def _legacy_rows() -> list[dict]:
    """AD / Madlan / Komo rows from the read-only scraper DB."""
    from ingestion.feeds import external_listings_view as external

    path = external._path()
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        history: dict[tuple, list[tuple]] = defaultdict(list)
        conn.execute("PRAGMA query_only=ON")
        try:
            for row in conn.execute(
                    """SELECT listing_id, source, price, recorded_at
                         FROM listing_price_history"""):
                history[(row["source"], row["listing_id"])].append(
                    (row["price"], row["recorded_at"]))
        except sqlite3.Error:
            pass                      # older scraper DBs have no history table

        rows = []
        for row in conn.execute(
                """SELECT source, listing_id, city, neighborhood, street, price,
                          rooms, area_sqm, price_per_sqm, listing_url,
                          first_seen, last_seen, listed_at, raw_json
                     FROM listings WHERE """ + external.SAFE_SALE_WHERE):
            item = dict(row)
            key = (item["source"], item["listing_id"])
            # A single stored price is still one point on the curve; pairing it
            # with first_seen lets a later harvest detect the first move.
            log = history.get(key) or [(item["price"], item["first_seen"])]
            rows.append({
                "listing_key": f"{item['source']}:{item['listing_id']}",
                "source": item["source"], "external_id": item["listing_id"],
                "url": (None if "madlan" in (item["source"] or "").lower()
                        else item.get("listing_url")),
                "locality": item.get("city"),
                "neighborhood": item.get("neighborhood"),
                "street": item.get("street"), "price": item.get("price"),
                "rooms": item.get("rooms"), "area_m2": item.get("area_sqm"),
                # Stored as a float by the scraper, so it reached the table as
                # "25,700.93". Agony over agorot per square metre is noise:
                # round to the shekel like every other ₪/m² in the product.
                "price_per_m2": (round(item["price_per_sqm"])
                                 if item.get("price_per_sqm") else None),
                "agency": None,
                "brokerage": _legacy_brokerage(item.get("raw_json")),
                "listed_at": item.get("listed_at"),
                # These feeds publish no photo URL we can date and no property
                # type; the fields are carried so every row has the same shape.
                "image_date": None, "image": None, "property_type": None,
                "house_number": None, "images": None,
                "brokerage_verified": False,
                "first_seen": item.get("first_seen"),
                "last_seen": item.get("last_seen"),
                "history": log,
            })
    return rows


def _benchmarks(rows: list[dict]) -> tuple[dict, dict]:
    """Median ₪/m² per neighbourhood and per city, over ALL rows.

    Built before any filter is applied, for the same reason the listings view
    does it: a benchmark computed from the surviving rows would move whenever
    the user narrowed the price range, silently restating every gap on screen.
    """
    city_values: dict = defaultdict(list)
    hood_values: dict = defaultdict(list)
    for row in rows:
        ppm = row.get("price_per_m2")
        # The same plausibility envelope the listings view enforces; a row whose
        # area arrived as 1.0 would otherwise drag its neighbourhood's median.
        if ppm and 2_000 <= float(ppm) <= 250_000:
            city_values[row.get("locality")].append(float(ppm))
            if row.get("neighborhood"):
                hood_values[(row.get("locality"), row.get("neighborhood"))].append(
                    float(ppm))
    return city_values, hood_values


#: Orderings the leads view offers.
SORTS = {
    "lead_score": ("lead_score", True),
    "days_desc": ("days_on_market", True),
    "days_asc": ("days_on_market", False),
    "cut_desc": ("total_change_pct", False),   # most negative first
    "discount": ("discount_pct", True),
    "price_asc": ("price", False),
    "price_desc": ("price", True),
}


#: Cached scoring output, keyed on the identity of everything that feeds it.
#: Scoring is not filter-dependent - benchmarks are computed over the whole
#: population by design - so the expensive part survives every filter change,
#: sort change and page turn. Rebuilding it per request measured 2.8s at 15,650
#: rows and would have grown past 4s at the full 18,000, on a screen whose whole
#: purpose is repeated filtering. Mirrors external_listings_view._SCORE_CACHE.
_LEAD_CACHE: dict = {}
_LEAD_LOCK = threading.Lock()


def _cache_key(conn: sqlite3.Connection) -> tuple:
    """Identity of the inputs: the two stores plus the CRM notes.

    Any harvest, any scraper import and any note edit changes one of these, so
    the cache invalidates itself without an explicit signal between processes.
    """
    from ingestion.feeds import external_listings_view as external

    # MIN(image_date) is in the key so a backfill that decodes dates for rows
    # already stored invalidates the cache. Without it the row count and
    # timestamps are unchanged and every listing keeps its old, undated age.
    yad2 = conn.execute(
        """SELECT COUNT(*), MAX(last_seen_at), MAX(first_seen_at),
                  MIN(image_date), COUNT(image_date)
             FROM yad2_listings""").fetchone()
    notes = conn.execute(
        "SELECT COUNT(*), MAX(updated_at) FROM listing_lead_notes").fetchone()
    try:
        onmap = conn.execute(
            "SELECT COUNT(*), MAX(last_seen_at) FROM onmap_listings").fetchone()
    except sqlite3.Error:
        onmap = (0, None)
    try:
        stat = external._path().stat()
        scraper = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        scraper = None
    return (tuple(yad2), tuple(notes), tuple(onmap), scraper)


def _enriched_rows():
    """Every listing, scored and classified. Cached on the inputs' identity."""
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        key = _cache_key(conn)
        cached = _LEAD_CACHE.get("entry")
        if cached and cached[0] == key:
            return cached[1]

        with _LEAD_LOCK:
            cached = _LEAD_CACHE.get("entry")
            if cached and cached[0] == key:
                return cached[1]
            notes = {row["listing_key"]: dict(row) for row in
                     conn.execute("SELECT * FROM listing_lead_notes")}
            rows = _yad2_rows(conn) + _onmap_rows(conn) + _legacy_rows()
            enriched = _score_all(rows, notes)
            _LEAD_CACHE["entry"] = (key, enriched)
            return enriched
    finally:
        conn.close()


def _score_all(rows: list[dict], notes: dict) -> list[dict]:
    """Score and classify every row against population-wide benchmarks."""
    from services.analytics import valuation

    city_values, hood_values = _benchmarks(rows)
    labels, confidence = _brokerage_tables()
    # Fetched once and passed down: assess() validates its cache with a
    # filesystem stat(), and paying that per row cost 17s for a full page.
    market = valuation._snapshot()

    enriched = []
    for row in rows:
        age = market_age(listed_at=row.get("listed_at"),
                         image_date=row.get("image_date"),
                         first_seen=row.get("first_seen"))
        movement = price_movement(row.get("history") or [])

        comparable = hood_values.get((row.get("locality"),
                                      row.get("neighborhood")), [])
        scope = "שכונה"
        if len(comparable) < 5:
            comparable = city_values.get(row.get("locality"), [])
            scope = "עיר"
        # A "median" over one or two rows is the row itself, which reports a
        # 0% gap and reads as "priced exactly at market" when the truth is that
        # there is nothing to compare against. Below MIN_COMPARABLES the gap is
        # withheld rather than fabricated, and the score simply loses that
        # signal instead of gaining a false one.
        benchmark = (statistics.median(comparable)
                     if len(comparable) >= MIN_COMPARABLES else None)
        ppm = row.get("price_per_m2")
        discount = ((benchmark - float(ppm)) / benchmark * 100
                    if benchmark and ppm else None)

        # A stated agency is verified fact; anything else comes from the row's
        # own evidence, and "unknown" stays unknown. Only an evidenced private
        # sale earns the private_seller signal - scoring an unknown as private
        # would manufacture the very lead the agent is looking for.
        brokerage = (row.get("brokerage")
                     or ("agency" if (row.get("agency") or "").strip()
                         else "unknown"))
        is_private = brokerage == "private"
        suspect = implausible(row)
        quality = "תקין"
        if suspect:
            # A broken record is not a motivated seller. Scoring it would put a
            # typo at the top of the list an agent works from, so it is shown,
            # labelled, and left unranked.
            scored = {"lead_score": 0.0, "signals": [
                {"signal": "suspect_data", "points": 0,
                 "text": f"נתון חשוד — {suspect}"}]}
            kind = {"lead_type": "נתון חשוד",
                    "lead_reason": suspect,
                    "suggested_action": "לאמת מחיר ושטח מול המודעה לפני כל פנייה"}
            discount = None
        else:
            scored = score_lead(age=age, movement=movement,
                                discount_pct=discount, is_private=is_private,
                                rooms=row.get("rooms"))
            kind = classify_lead(age=age, movement=movement,
                                 discount_pct=discount, is_private=is_private,
                                 rooms=row.get("rooms"), price=row.get("price"))
            # A gap this wide is almost never a bargain. Measured on the live
            # store: a "10-room תל אביב flat at ₪1.56M, 83.9% under the local
            # median" - which is a part-share sale, a plot, or a mis-keyed
            # area, not an opportunity. It clears the plausibility envelope on
            # every individual field, so only the gap itself gives it away.
            # The listings view already caps these at 60; the lead ranking has
            # to agree, or the two screens disagree about the same listing.
            if discount is not None and discount > EXTREME_GAP_PCT:
                quality = "חריג — לאמת מחיר ופרטי מודעה"
                scored["lead_score"] = min(scored["lead_score"], 60.0)
                scored["signals"].append(
                    {"signal": "extreme_gap", "points": 0,
                     "text": f"פער {discount:.0f}% — חריג, לאמת מול המודעה"})
                kind = {**kind, "suggested_action":
                        "לאמת שטח, חלקיות בעלות ופרטי מודעה לפני כל פנייה"}
        # The independent cross-check: what buyers in this city actually paid,
        # re-priced to today. Every other figure on the row compares an asking
        # price to other asking prices, which cannot tell you that a whole
        # area is optimistically priced.
        paid = valuation.assess(locality=row.get("locality"),
                                price=row.get("price"),
                                property_type=row.get("property_type"),
                                snapshot=market)
        if not suspect:
            extra_bonus, extra = extra_signals(
                row, age, movement, discount_pct=discount, paid=paid)
            scored["lead_score"] = round(
                min(100.0, scored["lead_score"] + extra_bonus), 1)
            scored["signals"].extend(extra)
        # When the two market views disagree, the paid one wins.
        #
        # Measured on the live data: a חולון listing sits 27.5% under the local
        # *asking* median and was classified "הזדמנות השקעה" - while the
        # concluded-sales register puts it at the 90th percentile, 52.8% above
        # what buyers there actually pay. Both figures are right; they answer
        # different questions. Asking-vs-asking cannot see a whole area that is
        # priced optimistically, and calling such a listing a bargain is the
        # single most expensive mistake this screen could make.
        if (paid.get("available") and not suspect
                and paid.get("percentile", 0) >= 75
                and discount is not None and discount > 5):
            scored["lead_score"] = min(scored["lead_score"], 40.0)
            scored["signals"].append({
                "signal": "asking_above_paid", "points": 0,
                "text": (f"זול מול המבוקש אך באחוזון {paid['percentile']} "
                         f"מול עסקאות שבוצעו — המחירים המבוקשים באזור גבוהים")})
            kind = {
                "lead_type": "זול מול המבוקש, יקר מול העסקאות",
                "lead_reason": (f"פער {discount:.0f}% מהחציון המבוקש, אך "
                                f"{paid['gap_vs_paid_pct']:+.0f}% מול מה ששולם בפועל"),
                "suggested_action": ("להשוות מול עסקאות אחרונות ברחוב לפני "
                                     "שמציגים כהזדמנות ללקוח")}
        note = notes.get(row["listing_key"], {})
        enriched.append({
            # .get, not [], so a caller assembling rows by hand (the checks in
            # verify_lead_intel.py, a future importer) is not required to know
            # every optional field this module has grown.
            **{k: row.get(k) for k in
               ("listing_key", "source", "external_id", "url", "locality",
                "neighborhood", "street", "price", "rooms", "area_m2",
                "price_per_m2", "agency", "first_seen", "last_seen", "image",
                "image_date", "property_type", "house_number", "images")},
            **age, **movement, **scored, **kind,
            "age_band": age_band(age.get("days_on_market")),
            "discount_pct": round(discount, 1) if discount is not None else None,
            "benchmark_m2": round(benchmark) if benchmark else None,
            "benchmark_scope": scope,
            "comparable_count": len(comparable),
            "brokerage": labels[brokerage],
            "brokerage_state": brokerage,
            # A stated classification and an inferred one must not read the
            # same. "ללא תיווך" drives a call list; the user has to be able to
            # see which rows Yad2 actually said that about.
            "brokerage_confidence": ("מאומת — המקור פרסם את סוג המפרסם"
                                     if row.get("brokerage_verified")
                                     and brokerage != "unknown"
                                     else confidence[brokerage]),
            "data_quality": f"נתון חשוד — {suspect}" if suspect else quality,
            # Asking price vs concluded deals - a different question from the
            # ₪/m² gap above, and the only one grounded in money that changed
            # hands. Prefixed so nothing confuses the two.
            "paid_available": paid.get("available", False),
            "paid_median": paid.get("median_paid"),
            "paid_percentile": paid.get("percentile"),
            "paid_gap_pct": paid.get("gap_vs_paid_pct"),
            "paid_verdict": paid.get("verdict"),
            "paid_deals": paid.get("deals"),
            "paid_range": ([paid["range_p25"], paid["range_p75"]]
                           if paid.get("available") else None),
            "paid_confidence": paid.get("confidence"),
            "paid_type_matched": paid.get("type_matched"),
            "paid_reason": paid.get("reason"),
            "stage": note.get("stage"), "owner": note.get("owner"),
            "note": note.get("note"), "next_action": note.get("next_action"),
            "due_date": note.get("due_date"),
        })
    return enriched


def leads(*, locality="", neighborhood="", source="", lead_type="",
          min_days=None, max_days=None, min_score=None, price_cut_only=False,
          private_only=False, min_price=None, max_price=None,
          exclude_suspect=False, sort="lead_score", limit=80, offset=0) -> dict:
    """Ranked leads across every for-sale source PlanWatch holds.

    Filtering happens here, over the pre-scored population, and never feeds back
    into the scoring: the ₪/m² benchmark each row is compared against is the
    median of its area computed over *every* listing. Filtering first would
    recompute each benchmark from the surviving rows, so narrowing the price
    range would silently restate the gap on every row still on screen.
    """
    enriched = _enriched_rows()

    def keep(row) -> bool:
        if locality and _squash(locality) not in _squash(row.get("locality")):
            return False
        if neighborhood and _squash(neighborhood) not in _squash(
                row.get("neighborhood")):
            return False
        if source and source.lower() not in (row.get("source") or "").lower():
            return False
        if lead_type and row.get("lead_type") != lead_type:
            return False
        days = row.get("days_on_market")
        if min_days is not None and (days is None or days < float(min_days)):
            return False
        if max_days is not None and (days is None or days > float(max_days)):
            return False
        if min_score is not None and row["lead_score"] < float(min_score):
            return False
        if price_cut_only and not (
                row.get("total_change_pct") is not None
                and row["total_change_pct"] <= -MEANINGFUL_CUT_PCT):
            return False
        # Matches only evidenced private sales. An "unknown" row is excluded on
        # purpose: this filter is used to build a call list, and a maybe is a
        # wasted call at best and an annoyed agency at worst.
        if private_only and row.get("brokerage_state") != "private":
            return False
        price = row.get("price")
        if min_price is not None and (price is None or price < float(min_price)):
            return False
        if max_price is not None and (price is None or price > float(max_price)):
            return False
        if exclude_suspect and row.get("data_quality") != "תקין":
            return False
        return True

    filtered = [row for row in enriched if keep(row)]

    key, desc = SORTS.get(sort or "lead_score", SORTS["lead_score"])
    known = [r for r in filtered if r.get(key) is not None]
    missing = [r for r in filtered if r.get(key) is None]
    known.sort(key=lambda r: r["lead_score"], reverse=True)
    known.sort(key=lambda r: r[key], reverse=desc)
    ordered = known + missing

    by_type: dict = defaultdict(int)
    for row in filtered:
        by_type[row["lead_type"]] += 1

    by_basis = defaultdict(int)
    for row in enriched:
        by_basis[row["age_basis"]] += 1
    # "Dated" = we have real evidence of the ad's age, whether the seller's own
    # date or the photo's. Only `observed` rows are limited to our own scraping
    # window, and reporting those separately is what stops the screen implying
    # that a five-day-old observation is a five-day-old listing.
    dated = by_basis["published"] + by_basis["image"]
    return {
        "total": len(ordered),
        "rows": ordered[offset:offset + limit],
        "universe": len(enriched),
        "by_type": [{"lead_type": k, "leads": v}
                    for k, v in sorted(by_type.items(), key=lambda i: -i[1])],
        "lead_types": sorted({r["lead_type"] for r in enriched}),
        "sources": [{"source": k, "leads": v} for k, v in sorted(
            _tally(enriched, "source").items(), key=lambda i: -i[1])],
        "sorts": list(SORTS),
        "sort": sort if sort in SORTS else "lead_score",
        "stages": list(STAGES),
        "weights": WEIGHTS,
        "thresholds": {"fresh_days": FRESH_DAYS, "seasoned_days": SEASONED_DAYS,
                       "stale_days": STALE_DAYS,
                       "meaningful_cut_pct": MEANINGFUL_CUT_PCT,
                       "min_comparables": MIN_COMPARABLES},
        # Coverage of the *good* age basis, so the screen can be honest about
        # how much of the ranking rests on an observation floor.
        # How much of the feed we distrust, said out loud rather than left for
        # the user to discover by sorting.
        "suspect_held": sum(1 for r in enriched if r["data_quality"] != "תקין"),
        # Brokerage is reported as three states, never two. "לא ידוע" shown
        # separately is an explicit requirement of the plan (section ד) so the
        # screen cannot imply a private seller where the source said nothing.
        "brokerage_coverage": {
            _brokerage_tables()[0][state]: sum(
                1 for r in enriched if r["brokerage_state"] == state)
            for state in ("agency", "private", "unknown")},
        "age_coverage": {
            "published": by_basis["published"],
            "image": by_basis["image"],
            "observed": by_basis["observed"],
            "unknown": by_basis["unknown"],
            "dated": dated,
            "dated_pct": (round(dated / len(enriched) * 100, 1)
                          if enriched else 0.0),
            "published_pct": (round(by_basis["published"] / len(enriched) * 100, 1)
                              if enriched else 0.0),
        },
        "note": (
            "משך השיווק מחושב לפי סדר עדיפויות: תאריך הפרסום של המוכר כשקיים; "
            "אחרת התאריך המקודד בכתובת התמונה של המודעה — ראיה שהמודעה קיימת "
            "לפחות מאז אז; ורק אם אין גם אותו, מהתאריך שבו PlanWatch ראה את "
            "המודעה לראשונה. שתי האפשרויות האחרונות הן רף תחתון ומסומנות ב-≥. "
            "הניקוד הוא כלי תעדוף על בסיס מחיר מבוקש, לא שמאות."),
    }


def _tally(rows, key) -> dict:
    counts: dict = defaultdict(int)
    for row in rows:
        value = (row.get(key) or "").strip()
        if value:
            counts[value] += 1
    return counts


def _squash(value) -> str:
    import re
    return re.sub(r"[\s\-־]+", "", str(value or "")).strip()


# ----------------------------------------------------------------- CRM notes

#: Fields a caller may set on a lead. Anything not passed is left untouched.
NOTE_FIELDS = ("stage", "owner", "note", "next_action", "due_date")


def save_note(listing_key: str, **fields) -> dict:
    """Attach the minimal CRM state the plan asks for to one listing.

    Only the keys actually passed are written. That distinction matters: an
    empty string is a deliberate clear ("I finished that task"), while an absent
    key means "I am editing the stage, leave my note alone". Treating both as
    "no value" - which a COALESCE-everything update does - makes it impossible
    to remove a task once it has been typed.
    """
    listing_key = str(listing_key or "").strip()
    if not listing_key:
        raise ValueError("listing_key is required")
    unknown = set(fields) - set(NOTE_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    stage = fields.get("stage")
    if stage and stage not in STAGES:
        raise ValueError(f"stage must be one of: {', '.join(STAGES)}")

    conn = db.get_conn()
    try:
        ensure_schema(conn)
        now = _now().isoformat(timespec="seconds")
        values = {name: (fields[name] or None) for name in fields}
        columns = ["listing_key", *values, "updated_at"]
        updates = ", ".join(f"{name}=excluded.{name}" for name in values)
        conn.execute(
            f"""INSERT INTO listing_lead_notes ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                ON CONFLICT(listing_key) DO UPDATE SET
                    {updates + ", " if updates else ""}
                    updated_at=excluded.updated_at""",
            (listing_key, *values.values(), now))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM listing_lead_notes WHERE listing_key=?",
            (listing_key,)).fetchone()
        return dict(row)
    finally:
        conn.close()
