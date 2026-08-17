"""
PlanWatch - opportunity engine (זיהוי הזדמנויות)
=================================================
Turns the tables PlanWatch already holds into ranked, explainable candidates.

Three products
--------------
1. `renewal_opportunities()` - declared urban-renewal complexes, ranked. This
   is the "old buildings worth redeveloping" answer: a declared complex IS a
   block of old flats, with the ministry's own count of existing units.
2. `tama38_hotspots()` - localities by תמ"א 38 activity. A תמ"א 38 plan is
   *legal proof* of pre-1980 buildings on the ground (the standard only applies
   to buildings permitted before תקן 413, 1980), so these are the most reliable
   old-stock markers in the whole database.
3. `deals()` - for-sale listings scored against the local ₪/m² benchmark. Needs
   the `listings` table populated from a feed you are licensed for; returns an
   explicit note when it is empty rather than a misleading empty result.

Why building age is NOT scored from plan dates
----------------------------------------------
The obvious idea - "old governing plan ⇒ old buildings" - does not survive
contact with the data. `plans.depositing_date` histogram:

    1980s: 1     2000s: 2     2010s: 6,172     2020s: 8,029

It is the date a plan entered the *digital* deposit workflow (≈2010 onward),
not the age of anything built. `open_date` is worse: it starts in 2008. Buildings
from the 1960s-70s - the prime renewal targets - are governed by paper plans
that are not in this dataset at all.

So age is inferred from **renewal evidence** instead: a declared complex or a
תמ"א 38 plan proves old stock at that location, on the record, with no
date arithmetic. Less coverage, but every hit is real.

Scoring rules
-------------
* Every score is 0-100 and **always returned with its components**. A bare
  number nobody can audit is worse than no number.
* A component with no data is **dropped and the remaining weights are
  renormalised** - it is not scored 0. Treating "no ₪/m² benchmark for this
  town" as "worthless location" would systematically bury exactly the older,
  smaller localities this tool exists to surface.
* `coverage` reports which components actually fired, so a score built from two
  signals is never mistaken for one built from five.

This is a screening and ranking tool. It is not an appraisal, it does not
price a specific apartment, and it cannot see building condition, tenant
consent, parking, or title encumbrances - the things that actually kill deals.
"""

from __future__ import annotations

import math
from services.external import market_client as mc

# ---------------------------------------------------------------- parameters

#: Renewal pipeline stage -> readiness score. Peaks at "approved, not yet
#: realised": the statutory risk is spent but nobody has broken ground, which
#: is the window an entrant can still get into. Verified stage counts
#: (2026-07): early 115, statutory 278, approved-pre 343, post-licensing 177,
#: executing 55.
STAGE_SCORE = {
    "תכנון ראשוני": 55,
    "תכנון סטטוטורי": 75,
    "תכנית מאושרת לפני מימוש": 100,
    "תכנית מאושרת - אחרי רישוי": 45,
    "תכנית מאושרת במימוש": 15,
}

#: Component weights for renewal_opportunities(). They are renormalised over
#: whichever components have data, so these are ratios, not percentages.
RENEWAL_WEIGHTS = {
    "stage": 30,        # how open the window still is
    "multiplier": 25,   # units_planned / units_existing - the ministry's own uplift
    "scale": 15,        # how many existing flats (deal size)
    "price": 20,        # local ₪/m² percentile - feasibility floor
    "momentum": 10,     # CBS district 12-month change
}

DEAL_WEIGHTS = {
    "discount": 45,     # ₪/m² below the local benchmark
    "age": 20,          # older building = renewal candidate
    "plan_upside": 20,  # an approved plan covers the parcel
    "renewal_near": 15, # a declared complex is nearby
}

#: A renewal multiplier below this is usually not worth a developer's time;
#: above it, each extra 0.5x adds real value. Used to shape the 0-100 curve,
#: not as a hard filter.
MULTIPLIER_FLOOR = 1.3
MULTIPLIER_STRONG = 3.0


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _blend(components: dict, weights: dict) -> tuple[float, list[str], float]:
    """
    Weighted mean over the components that have a value, renormalised.

    Returns `(score, components_used, confidence)` where confidence is the
    share of total weight that actually had data, 0-1.

    **Why confidence exists.** Renormalising alone is not enough: the first
    version of this ranked a complex with 0 existing units and no multiplier at
    100.0, because the only two components that fired (stage, price) were both
    high. Dropping missing components stops them dragging a score down, but it
    also lets a nearly-dataless row float to the top. Callers must therefore
    rank on `score * confidence` (`rank_score`), and show `score` only as the
    quality of what was measurable.
    """
    used = {k: v for k, v in components.items() if v is not None}
    if not used:
        return 0.0, [], 0.0
    covered = sum(weights[k] for k in used)
    score = sum(used[k] * weights[k] for k in used) / covered
    confidence = covered / sum(weights.values())
    return round(score, 1), sorted(used), round(confidence, 2)


# ------------------------------------------------------- locality → district

def locality_districts(conn) -> dict:
    """
    Map locality name -> CBS district, derived from `plans`.

    `urban_renewal` carries a locality but no district, and CBS publishes its
    index by district. The most common `district_name` among plans in that
    jurisdiction is the answer; there is no official crosswalk in any of the
    feeds, and this one is checkable against the data.
    """
    out = {}
    for row in conn.execute(
        """SELECT jurisdiction_name, district_name, COUNT(*) n
           FROM plans
           WHERE jurisdiction_name <> '' AND district_name <> ''
           GROUP BY 1, 2 ORDER BY 1, n DESC"""):
        out.setdefault(row["jurisdiction_name"], row["district_name"])
    return out


def _squash(name):
    """Locality names differ by spaces/hyphens across feeds ('תל אביב -יפו')."""
    return (name or "").replace(" ", "").replace("-", "").replace("'", "")


def _price_context(conn):
    """
    Pre-compute the ₪/m² benchmark per locality plus the national spread, so
    scoring 969 complexes does not run 969 aggregate queries.
    """
    spread = mc.national_price_percentiles(conn)
    prices = {}
    for row in conn.execute(
        """SELECT locality, AVG(price_per_m2) avg_m2, SUM(units) units,
                  SUM(subscribers) subs, COUNT(*) n
           FROM ml_projects
           WHERE price_per_m2 IS NOT NULL AND locality IS NOT NULL
           GROUP BY locality"""):
        prices[_squash(row["locality"])] = {
            "avg_m2": round(row["avg_m2"], 0),
            "projects": row["n"],
            "demand_ratio": (round((row["subs"] or 0) / row["units"], 1)
                             if row["units"] else None),
        }
    return prices, spread


def _price_score(avg_m2, spread) -> float | None:
    """
    Where this locality's ₪/m² sits in the national spread, 0-100.

    Renewal feasibility rises with the sale price: a demolish-and-rebuild only
    pencils out where the finished flats are worth enough. p10 -> ~0,
    p90 -> ~100, linear in between on the percentile scale.
    """
    if avg_m2 is None or not spread:
        return None
    p10, p90 = spread.get("p10"), spread.get("p90")
    if not p10 or not p90 or p90 <= p10:
        return None
    return _clamp((avg_m2 - p10) / (p90 - p10) * 100)


def _multiplier_score(existing, planned) -> tuple[float | None, float | None]:
    """(score, multiplier). None when the source lacks either count."""
    if not existing or not planned or existing <= 0:
        return None, None
    multiplier = planned / existing
    if multiplier <= MULTIPLIER_FLOOR:
        return 0.0, round(multiplier, 2)
    span = MULTIPLIER_STRONG - MULTIPLIER_FLOOR
    return (_clamp((multiplier - MULTIPLIER_FLOOR) / span * 100),
            round(multiplier, 2))


def _scale_score(existing) -> float | None:
    """
    Deal size from the number of existing flats, log-shaped: the step from 20
    to 60 flats matters far more than 400 to 440.
    """
    if not existing or existing <= 0:
        return None
    return _clamp(math.log10(existing) / math.log10(500) * 100)


def _momentum_score(trend) -> float | None:
    """
    CBS district 12-month change -> 0-100. -5% or worse ≈ 0, +10% ≈ 100.
    The national index is currently NEGATIVE (-2.0% y/y as of 2026-04), so
    this component legitimately drags most scores down right now. That is the
    market, not a bug.
    """
    if not trend or trend.get("pct_year") is None:
        return None
    return _clamp((trend["pct_year"] + 5) / 15 * 100)


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------ renewal candidates

def renewal_opportunities(conn, locality="", track="", stage="",
                          min_existing=None, min_multiplier=None,
                          max_permits=None, limit=60, offset=0) -> dict:
    """
    Declared urban-renewal complexes, ranked by opportunity score.

    Each row is a real block of existing (old) flats with the ministry's own
    numbers on it. Filters are optional and AND together.

    `max_permits=0` is the useful one: complexes where no building permit has
    issued yet, i.e. the window is still open.
    """
    from services.external import urban_renewal_client as ur

    if not ur.count_local(conn):
        return {"total": 0, "rows": [], "has_renewal": False,
                "note": "מאגר ההתחדשות העירונית לא יובא. ייבא אותו בלשונית "
                        "'מקורות נתונים' ואז נסה שוב."}

    prices, spread = _price_context(conn)
    districts = locality_districts(conn)
    trends: dict = {}

    clauses, params = ["1=1"], []
    if locality:
        clauses.append("u.locality LIKE ?")
        params.append(f"%{locality}%")
    if track:
        clauses.append("u.track = ?")
        params.append(track)
    if stage:
        clauses.append("u.status LIKE ?")
        params.append(f"%{stage}%")
    if min_existing is not None:
        clauses.append("CAST(u.units_existing AS INTEGER) >= ?")
        params.append(min_existing)
    if max_permits is not None:
        clauses.append("COALESCE(CAST(u.permits AS INTEGER), 0) <= ?")
        params.append(max_permits)

    rows = conn.execute(
        f"""SELECT u.*, p.object_id, p.pl_name, p.short_status, p.station,
                   p.area_dunam, p.district_name, p.jurisdiction_name,
                   p.housing_units, p.pl_url
            FROM urban_renewal u
            LEFT JOIN plans p ON p.pl_number = u.plan_number
            WHERE {" AND ".join(clauses)}""", params).fetchall()

    scored = []
    for row in rows:
        item = dict(row)
        existing = _as_int(item.get("units_existing"))
        planned = _as_int(item.get("units_planned"))
        added = _as_int(item.get("units_added"))
        permits = _as_int(item.get("permits")) or 0

        multiplier_score, multiplier = _multiplier_score(existing, planned)
        if min_multiplier is not None and (multiplier or 0) < min_multiplier:
            continue

        district = item.get("district_name") or districts.get(item.get("locality"))
        if district and district not in trends:
            trends[district] = mc.district_trend(conn, district)
        trend = trends.get(district)

        price = prices.get(_squash(item.get("locality")))
        components = {
            "stage": STAGE_SCORE.get((item.get("status") or "").strip()),
            "multiplier": multiplier_score,
            "scale": _scale_score(existing),
            "price": _price_score(price["avg_m2"] if price else None, spread),
            "momentum": _momentum_score(trend),
        }
        score, used, confidence = _blend(components, RENEWAL_WEIGHTS)

        item.update(
            score=score,
            rank_score=round(score * confidence, 1),
            confidence=confidence,
            components={k: (round(v, 1) if v is not None else None)
                        for k, v in components.items()},
            coverage=used,
            multiplier=multiplier,
            units_existing_n=existing,
            units_planned_n=planned,
            units_added_n=added,
            permits_n=permits,
            district=district,
            price_per_m2=price["avg_m2"] if price else None,
            price_projects=price["projects"] if price else 0,
            demand_ratio=price["demand_ratio"] if price else None,
            district_pct_year=trend["pct_year"] if trend else None,
        )
        scored.append(item)

    # rank_score, not score - see _blend(). Ties break on deal size.
    scored.sort(key=lambda r: (-r["rank_score"], -(r["units_existing_n"] or 0)))
    return {
        "total": len(scored),
        "limit": limit, "offset": offset,
        "rows": scored[offset:offset + limit],
        "has_renewal": True,
        "weights": RENEWAL_WEIGHTS,
        "price_spread": spread,
        "scored_with_price": sum(1 for r in scored if r["price_per_m2"]),
        "full_confidence": sum(1 for r in scored if r["confidence"] >= 1.0),
        "note": ("הניקוד הוא כלי מיון והשוואה, לא הערכת שווי. מחירי ה-₪/מ\"ר "
                 "מגיעים ממחיר למשתכן ולכן נמוכים שיטתית ממחירי השוק החופשי."),
    }


def tama38_hotspots(conn, limit=25) -> dict:
    """
    Localities ranked by תמ"א 38 activity.

    Why this is the strongest old-building signal available: תמ"א 38 applies
    only to buildings permitted before 1980 (pre-תקן 413). A תמ"א 38 plan on
    record therefore *proves* pre-1980 stock at that spot - no inference from
    plan dates, which do not carry building age at all.

    Matching uses the exact spellings, not '%38%': a bare 38 matches street
    numbers ("חפץ חיים 38") and plan numbers, and pulled 550 false positives.
    """
    import dashboard  # reuse the one audited TAMA-38 clause

    clause = dashboard.Handler._KIND_CLAUSES["tama38"]
    prices, spread = _price_context(conn)

    rows = []
    for row in conn.execute(
        f"""SELECT p.jurisdiction_name locality, p.district_name district,
                   COUNT(*) plans,
                   SUM(CASE WHEN p.station LIKE '%אישור%' THEN 1 ELSE 0 END) approved,
                   COALESCE(SUM(p.housing_units), 0) units
            FROM plans p
            WHERE ({clause}) AND p.jurisdiction_name <> ''
            GROUP BY 1, 2 ORDER BY plans DESC LIMIT ?""", (limit,)):
        item = dict(row)
        price = prices.get(_squash(item["locality"]))
        item["price_per_m2"] = price["avg_m2"] if price else None
        item["demand_ratio"] = price["demand_ratio"] if price else None
        trend = mc.district_trend(conn, item["district"])
        item["district_pct_year"] = trend["pct_year"] if trend else None
        rows.append(item)

    total = conn.execute(
        f"SELECT COUNT(*) FROM plans p WHERE ({clause})").fetchone()[0]
    return {"total_plans": total, "rows": rows, "price_spread": spread,
            "note": 'תמ"א 38 חלה רק על מבנים שהיתר הבנייה שלהם קדם ל-1980, '
                    'ולכן תוכנית כזו היא הוכחה למלאי בנוי ישן באותו מקום.'}


# ---------------------------------------------------------------- deals

def deals(conn, locality="", neighborhood="", street="", source="",
          min_price=None, max_price=None, min_area=None, max_area=None,
          min_rooms=None, max_rooms=None, min_ppm=None, max_ppm=None,
          date_from="", date_to="", max_year_built=None,
          min_discount_pct=None, max_discount_pct=None, has_url=None,
          exclude_suspect=False, sort="score", limit=60, offset=0) -> dict:
    """
    For-sale listings ranked by how far below the local benchmark they sit.

    Requires `listings` (see market_client.import_listings_csv). PlanWatch does
    not fetch Yad2 or Madlan - both forbid it in robots.txt - so this stays
    empty until you load a feed you are licensed for.

    The signature deliberately mirrors external_listings_view.listings(): the
    dashboard picks between the two at request time, so a filter the user sets
    must mean the same thing whichever one answers. When this path accepted
    only a subset, loading a licensed CSV made the other filters silently
    inert - the UI still offered them and simply returned unfiltered rows.

    Two differences are forced by the schema, not by choice: this table has
    `address` where the scraper has `street`, and it dates rows by
    `fetched_at` rather than `first_seen`.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listings'"
    ).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] if exists else 0
    source = (source or "").strip()
    sources = [dict(row) for row in conn.execute(
        """SELECT source, COUNT(*) AS listings, MAX(fetched_at) AS last_fetched_at
           FROM listings WHERE (?='' OR source=?)
           GROUP BY source ORDER BY last_fetched_at DESC, source""", (source, source)
    )] if count else []
    if not count:
        return {
            "total": 0, "rows": [], "has_listings": False,
            "note": "טבלת הנכסים למכירה ריקה. PlanWatch לא מושך מיד2 וממדלן - "
                    "שני האתרים אוסרים זאת ב-robots.txt. טען פיד שיש לך רשות "
                    "אליו: python -c \"import db,market_client as m; "
                    "m.import_listings_csv(db.get_conn(),'listings.csv')\"",
            "expected_columns": list(mc.LISTING_FIELDS), "sources": sources,
        }

    prices, spread = _price_context(conn)
    clauses, params = ["l.price IS NOT NULL", "l.area_m2 > 0"], []
    if source:
        clauses.append("l.source = ?")
        params.append(source)
    if locality:
        clauses.append("l.locality LIKE ?")
        params.append(f"%{locality}%")
    if neighborhood:
        clauses.append("l.neighborhood LIKE ?")
        params.append(f"%{neighborhood}%")
    if street:
        # No `street` column here; the licensed-feed schema carries the whole
        # address in one field.
        clauses.append("l.address LIKE ?")
        params.append(f"%{street}%")
    if min_price is not None:
        clauses.append("l.price >= ?")
        params.append(min_price)
    if max_price is not None:
        clauses.append("l.price <= ?")
        params.append(max_price)
    if min_area is not None:
        clauses.append("l.area_m2 >= ?")
        params.append(min_area)
    if max_area is not None:
        clauses.append("l.area_m2 <= ?")
        params.append(max_area)
    if min_rooms is not None:
        clauses.append("l.rooms IS NOT NULL AND l.rooms >= ?")
        params.append(min_rooms)
    if max_rooms is not None:
        clauses.append("l.rooms IS NOT NULL AND l.rooms <= ?")
        params.append(max_rooms)
    # area_m2 > 0 is already required above, so price/area is always defined
    # and the bound can be applied without a stored price_per_m2.
    if min_ppm is not None:
        clauses.append("COALESCE(l.price_per_m2, l.price / l.area_m2) >= ?")
        params.append(min_ppm)
    if max_ppm is not None:
        clauses.append("COALESCE(l.price_per_m2, l.price / l.area_m2) <= ?")
        params.append(max_ppm)
    if has_url:
        clauses.append("l.url IS NOT NULL AND trim(l.url) <> ''")
    # Dated on fetched_at - the local counterpart of the scraper's first_seen.
    # substr() reduces a timestamp to its calendar day so an inclusive upper
    # bound does not exclude everything logged after midnight.
    if date_from:
        clauses.append("substr(l.fetched_at, 1, 10) >= ?")
        params.append(str(date_from)[:10])
    if date_to:
        clauses.append("substr(l.fetched_at, 1, 10) <= ?")
        params.append(str(date_to)[:10])
    if max_year_built is not None:
        clauses.append("l.year_built IS NOT NULL AND l.year_built <= ?")
        params.append(max_year_built)

    rows = conn.execute(
        f"SELECT l.* FROM listings l WHERE {' AND '.join(clauses)}",
        params).fetchall()

    import db as _db
    from services.external import urban_renewal_client as ur

    scored = []
    for row in rows:
        item = dict(row)
        item.pop("raw_json", None)
        per_m2 = item.get("price_per_m2") or (item["price"] / item["area_m2"])
        item["price_per_m2"] = round(per_m2, 0)

        benchmark = prices.get(_squash(item.get("locality")))
        discount_pct = discount_score = None
        if benchmark and benchmark["avg_m2"]:
            discount_pct = round((1 - per_m2 / benchmark["avg_m2"]) * 100, 1)
            # 0% below benchmark -> 50; 30% below -> 100; 30% above -> 0.
            discount_score = _clamp(50 + discount_pct / 30 * 50)
        if min_discount_pct is not None and (discount_pct or -999) < min_discount_pct:
            continue
        if max_discount_pct is not None and (
                discount_pct is None or discount_pct > max_discount_pct):
            continue

        year = item.get("year_built")
        # 1960 or earlier -> 100, 2010 or later -> 0. Pre-1980 scores >= 60,
        # which is where תמ"א 38 eligibility begins.
        age_score = _clamp((2010 - year) / 50 * 100) if year else None

        plan_score = renewal_score = None
        if item.get("lat") and item.get("lon"):
            covering = _db.plans_covering_point(conn, item["lon"], item["lat"])
            approved = [p for p in covering
                        if "אישור" in (p["station"] or "")]
            plan_score = _clamp(len(approved) * 40) if covering else 0.0
            item["plans_covering"] = len(covering)
            item["plans_approved"] = len(approved)
            try:
                near = ur.near_point(conn, item["lat"], item["lon"],
                                     radius_m=500, limit=5)
            except Exception:
                near = []
            renewal_score = _clamp(len(near) * 50)
            item["renewal_nearby"] = len(near)

        components = {"discount": discount_score, "age": age_score,
                      "plan_upside": plan_score, "renewal_near": renewal_score}
        score, used, confidence = _blend(components, DEAL_WEIGHTS)
        item.update(score=score, rank_score=round(score * confidence, 1),
                    confidence=confidence, components=components, coverage=used,
                    benchmark_m2=benchmark["avg_m2"] if benchmark else None,
                    discount_pct=discount_pct)
        scored.append(item)

    # Same ordering vocabulary as the scraper path, over this table's column
    # names, so switching feeds does not change what "מיון: מחיר" means.
    from ingestion.feeds import external_listings_view as _ext
    key, desc = _ext.SORTS.get(sort or "score", _ext.SORTS["score"])
    key = {"area_m2": "area_m2", "first_seen": "fetched_at"}.get(key, key)
    known = [r for r in scored if r.get(key) is not None]
    missing = [r for r in scored if r.get(key) is None]
    known.sort(key=lambda r: -r["rank_score"])
    known.sort(key=lambda r: r[key], reverse=desc)
    missing.sort(key=lambda r: -r["rank_score"])
    scored = known + missing
    # Ranking needs every candidate scored before any can be dropped, so paging
    # is a slice of the finished ranking rather than a SQL LIMIT.
    return {"total": len(scored), "rows": scored[offset:offset + limit],
            "limit": limit, "offset": offset, "has_listings": True,
            "listings_held": count, "weights": DEAL_WEIGHTS,
            "sort": sort if sort in _ext.SORTS else "score",
            "sorts": list(_ext.SORTS),
            "price_spread": spread, "sources": sources,
            "note": "ההשוואה היא מול מחירי מחיר-למשתכן, שנמוכים שיטתית משוק "
                    "חופשי. 'הנחה' כאן היא יחסית לבנצ'מרק הזה בלבד."}


# ------------------------------------------------------------- statistics

def market_snapshot(conn) -> dict:
    """Headline market statistics for the dashboard."""
    counts = mc.counts(conn)
    national = mc.national_trend(conn)
    districts = []
    for name in mc.DISTRICT_TO_CBS:
        trend = mc.district_trend(conn, name)
        if trend:
            districts.append(trend)
    districts.sort(key=lambda d: -(d.get("pct_year") or -99))

    top = [dict(r) for r in conn.execute(
        """SELECT locality, ROUND(AVG(price_per_m2)) avg_m2, COUNT(*) projects,
                  SUM(units) units, SUM(subscribers) subscribers
           FROM ml_projects WHERE price_per_m2 IS NOT NULL AND locality IS NOT NULL
           GROUP BY locality ORDER BY avg_m2 DESC LIMIT 10""")]
    cheap = [dict(r) for r in conn.execute(
        """SELECT locality, ROUND(AVG(price_per_m2)) avg_m2, COUNT(*) projects,
                  SUM(units) units, SUM(subscribers) subscribers
           FROM ml_projects WHERE price_per_m2 IS NOT NULL AND locality IS NOT NULL
           GROUP BY locality ORDER BY avg_m2 ASC LIMIT 10""")]
    demand = [dict(r) for r in conn.execute(
        """SELECT locality, SUM(subscribers) subs, SUM(units) units,
                  ROUND(SUM(subscribers)*1.0/SUM(units), 1) ratio,
                  ROUND(AVG(price_per_m2)) avg_m2
           FROM ml_projects WHERE units > 0 AND locality IS NOT NULL
           GROUP BY locality HAVING SUM(units) >= 100
           ORDER BY ratio DESC LIMIT 10""")]

    pipeline = [dict(r) for r in conn.execute(
        """SELECT stage, COUNT(*) plans, COALESCE(SUM(units_potential),0) units
           FROM rami_inventory WHERE stage IS NOT NULL
           GROUP BY 1 ORDER BY units DESC""")] if counts["rami_inventory"] else []

    renewal_stages = [dict(r) for r in conn.execute(
        """SELECT COALESCE(status,'לא צויין') stage, COUNT(*) complexes,
                  COALESCE(SUM(CAST(units_existing AS INTEGER)),0) existing,
                  COALESCE(SUM(CAST(units_planned  AS INTEGER)),0) planned,
                  SUM(CASE WHEN COALESCE(CAST(permits AS INTEGER),0)=0
                           THEN 1 ELSE 0 END) no_permit
           FROM urban_renewal GROUP BY 1 ORDER BY complexes DESC""")] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                        " AND name='urban_renewal'").fetchone() else []

    return {
        "counts": counts,
        "national": national,
        "districts": districts,
        "price_spread": mc.national_price_percentiles(conn),
        "most_expensive": top,
        "cheapest": cheap,
        "highest_demand": demand,
        "rami_pipeline": pipeline,
        "renewal_stages": renewal_stages,
        "caveats": [
            'מחירי ה-₪/מ"ר הם ממחיר למשתכן (דירה בהנחה) ולכן נמוכים שיטתית '
            'ממחירי השוק החופשי. השתמש בהם לדירוג יחסי בין ישובים ולרצפת מחיר.',
            'מדד מחירי הדירות של הלמ"ס הוא מדד, לא מחיר בשקלים.',
            'גיל בניין אינו נגזר מתאריכי תוכניות - ראה את ה-docstring של '
            'opportunity.py. הוא מוסק מראיות התחדשות בלבד.',
        ],
    }


def locality_ranking(conn, limit=40) -> dict:
    """
    One row per locality with everything PlanWatch knows, for cross-comparison.
    This is the "statistics by search" view.
    """
    prices, spread = _price_context(conn)
    districts = locality_districts(conn)
    has_renewal = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='urban_renewal'"
    ).fetchone()

    rows = {}
    for row in conn.execute(
        """SELECT jurisdiction_name locality, COUNT(*) plans,
                  COALESCE(SUM(housing_units),0) units,
                  SUM(CASE WHEN station LIKE '%אישור%' THEN 1 ELSE 0 END) approved
           FROM plans WHERE jurisdiction_name <> ''
           GROUP BY 1 ORDER BY units DESC LIMIT ?""", (limit,)):
        item = dict(row)
        price = prices.get(_squash(item["locality"]))
        item["price_per_m2"] = price["avg_m2"] if price else None
        item["demand_ratio"] = price["demand_ratio"] if price else None
        item["district"] = districts.get(item["locality"])
        item["complexes"] = item["existing_units"] = 0
        rows[item["locality"]] = item

    if has_renewal and rows:
        marks = ",".join("?" for _ in rows)
        for row in conn.execute(
            f"""SELECT locality, COUNT(*) complexes,
                       COALESCE(SUM(CAST(units_existing AS INTEGER)),0) existing
                FROM urban_renewal WHERE locality IN ({marks})
                GROUP BY 1""", list(rows)):
            if row["locality"] in rows:
                rows[row["locality"]]["complexes"] = row["complexes"]
                rows[row["locality"]]["existing_units"] = row["existing"]

    return {"rows": list(rows.values()), "price_spread": spread}


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    conn = db.get_conn()
    snap = market_snapshot(conn)
    print("national:", json.dumps(snap["national"], ensure_ascii=False))
    print("spread:  ", json.dumps(snap["price_spread"], ensure_ascii=False))
    print("\ntop renewal opportunities (no permit yet):")
    out = renewal_opportunities(conn, max_permits=0, limit=8)
    print(f"  {out['total']} candidates,"
          f" {out['scored_with_price']} with a price benchmark,"
          f" {out['full_confidence']} at full confidence")
    print("  rank  score conf  locality     name")
    for r in out["rows"]:
        print(f"  {r['rank_score']:5.1f} {r['score']:5.1f} {r['confidence']:4.2f}"
              f"  {(r['locality'] or '?')[:12]:12}"
              f" {(r['name'] or '?')[:24]:24} exist={r['units_existing_n'] or 0:>4}"
              f" x{r['multiplier'] or 0:<5} {(r['status'] or '?')[:23]:23}")
    print("\ntama38 hotspots:")
    for r in tama38_hotspots(conn, limit=6)["rows"]:
        print(f"  {r['locality'][:14]:14} plans={r['plans']:>4}"
              f" approved={r['approved']:>4} ₪/m²={r['price_per_m2'] or '-'}")
    print("\ndeals:", json.dumps(deals(conn, limit=2), ensure_ascii=False)[:260])
    conn.close()
