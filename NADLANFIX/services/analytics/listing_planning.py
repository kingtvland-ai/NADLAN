"""What is *planned* on the ground a listing stands on.

Why this belongs on a sale listing
----------------------------------
Every other measure in `listing_analytics` compares an asking price to other
asking prices. That answers "is this cheap for the area" and nothing else. It
cannot see the thing this project spent 36,860 plan geometries collecting: that
two identical flats on the same street can be worth very different money
because one of them sits inside a plan that adds 400 housing units and the
other does not.

No listing board carries this. Yad2 knows the price; only the cross-product of
the board and the planning register knows that the price is being asked on a
parcel with an approved plan on it. That is the whole reason both datasets are
in one database.

How the match is made, and what it does not claim
--------------------------------------------------
Every stored listing has real coordinates (100% of 64,317 rows), and every plan
carries a polygon. So the test is point-in-polygon, run through an STRtree so
that 64,317 points against 20,261 polygons is seconds rather than hours.

Two deliberate narrowings:

* Only plans that **add housing** (`housing_units > 0`) are loaded. A plan that
  reroutes a road is real planning and irrelevant to what a flat is worth.
* `covers`, not `contains` - a parcel exactly on a plan's edge is inside it.
  This is the same choice `sync._point_hits_plan` made, and for the same
  reason.

What this is **not** is a valuation. A plan covering a point says the ground is
subject to it, not that this particular flat gains from it, and certainly not
by how much. Rights, floor, orientation and the building's own condition decide
that, and none of them are here. It is a flag that says *look at this one* -
which is exactly what a screening tool should say.
"""

from __future__ import annotations

import json
from collections import defaultdict

import db

#: Plan statuses ordered from furthest along to earliest. Position in this list
#: is the whole ranking: an approved plan is a fact about the ground, a plan
#: still being deposited is a proposal that may never happen.
#:
#: Same reasoning as `opportunity.py`'s stage ranking - the peak of usefulness
#: is "approved but not yet built", where the statutory risk is spent and
#: nobody has started.
#: Taken from the register's own vocabulary, not invented: these are the exact
#: `short_status` values the 20,261 housing-adding plans actually carry, in
#: descending order of how settled the plan is.
STATUS_RANK = {
    "פרסום אישור": 100,      # 7,141 plans - approved and published
    "התכנית אושרה": 95,      # 5,577 - approved
    "אושרה הקלה": 85,
    "בתהליך אישור": 70,
    "פרסום הפקדה": 60,       # deposited: the objection window is open
    "בתהליך הפקדה": 55,
    "טרום הפקדה": 45,
    "החלטה בדיון": 40,
    "קיום תנאי סף": 30,      # 2,484 - threshold conditions, very early
    "קבלת תכנית": 25,
    "פרסום הבקשה": 20,
    # Dead ends. Ranked below everything so a rejected plan never reads as
    # planning upside - it is the opposite, and it is on the ground too.
    "דחיית תכנית": 0,
    "התכנית נדחתה": 0,
    "ביטול פרסום": 0,
    "לא מולאו תנאים": 0,
    # "סיום טיפול" (2,637) is deliberately absent: the register uses it for
    # both a finished approval and an abandoned file, so it cannot carry a
    # stage. It falls to DEFAULT_STATUS_RANK and counts only as "planning here".
}

#: Below this, a plan's stage is unknown rather than early. It still counts
#: toward "there is planning here", it just cannot carry the stage signal.
DEFAULT_STATUS_RANK = 30

#: Added units per dunam is what separates a plan that changes a street from a
#: city-wide master plan, and the raw unit count does not: נת/2035 adds 95,075
#: units - across the whole of Netanya, 2.7 per dunam. Ranking on units alone
#: put every flat in Netanya at the top for the same reason.
#:
#: Density has its own pathology at the other end. The register records some
#: amendment plans with only the amended parcel's area, which yields 4,261
#: units on 0.8 dunam - 5,681 per dunam, a number no building achieves. Both
#: guards below are therefore necessary; measured, the real distribution runs
#: p50 = 4.1 and p90 = 15.8 units per dunam.
MIN_PLAN_DUNAM = 0.5
MAX_PLAUSIBLE_DENSITY = 100.0

#: Density scored full. Roughly the p97 of the plausible range - dense enough
#: to rebuild a block, not so dense that only broken records reach it.
DENSITY_FULL = 25.0

_CACHE: dict = {}


def _density(units, area_dunam):
    """Added units per dunam, or None when the record cannot support one."""
    try:
        units, area = float(units or 0), float(area_dunam or 0)
    except (TypeError, ValueError):
        return None
    if units <= 0 or area < MIN_PLAN_DUNAM:
        return None
    density = units / area
    return density if density <= MAX_PLAUSIBLE_DENSITY else None


def _status_rank(status) -> int:
    text = str(status or "").strip()
    for key, rank in STATUS_RANK.items():
        if key in text:
            return rank
    return DEFAULT_STATUS_RANK


def _load_plans():
    """Plan polygons that add housing, with an STRtree over them."""
    from shapely.geometry import shape
    from shapely.strtree import STRtree

    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT pl_number, pl_name, short_status, housing_units,
                      area_dunam, geometry_json, jurisdiction_name
                 FROM plans
                WHERE geometry_json IS NOT NULL AND housing_units > 0""").fetchall()
    finally:
        conn.close()

    geometries, meta = [], []
    for row in rows:
        try:
            geom = shape(json.loads(row["geometry_json"]))
        except (ValueError, TypeError, AttributeError):
            continue
        if geom.is_empty:
            continue
        # A source polygon can be self-intersecting; buffer(0) repairs it. The
        # same fix sync.py needs, for the same malformed rows.
        if not geom.is_valid:
            geom = geom.buffer(0)
            if geom.is_empty:
                continue
        geometries.append(geom)
        meta.append({"number": row["pl_number"], "name": row["pl_name"],
                     "status": row["short_status"],
                     "units": int(row["housing_units"] or 0),
                     "density": _density(row["housing_units"], row["area_dunam"]),
                     "rank": _status_rank(row["short_status"])})
    return STRtree(geometries), geometries, meta


def _index():
    if "tree" not in _CACHE:
        tree, geometries, meta = _load_plans()
        _CACHE.update(tree=tree, geometries=geometries, meta=meta)
    return _CACHE["tree"], _CACHE["geometries"], _CACHE["meta"]


def renewal_by_locality() -> dict:
    """Urban-renewal weight per city.

    The renewal register has a locality but no polygon, so this cannot be a
    point test - it is a statement about the town, not the parcel, and is
    reported that way. A city with 40 declared complexes is a different market
    from one with none, whichever street a flat is on.
    """
    conn = db.get_conn()
    try:
        out = {}
        for row in conn.execute(
                """SELECT locality, COUNT(*) complexes,
                          SUM(COALESCE(units_added, 0)) units_added,
                          SUM(CASE WHEN in_execution THEN 1 ELSE 0 END) in_execution
                     FROM urban_renewal GROUP BY locality"""):
            key = str(row["locality"] or "").strip().lower()
            if key:
                out[key] = {"complexes": row["complexes"],
                            "units_added": int(row["units_added"] or 0),
                            "in_execution": row["in_execution"] or 0}
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def annotate(rows: list[dict]) -> list[dict]:
    """Attach plan coverage and renewal context to every listing with a point."""
    from shapely.geometry import Point

    try:
        tree, geometries, meta = _index()
    except Exception as exc:            # shapely missing, geometry unreadable
        for row in rows:
            row["planning_basis"] = f"שכבת התכנון לא נטענה: {type(exc).__name__}"
        return rows

    renewal = renewal_by_locality()
    for row in rows:
        lat, lon = row.get("lat"), row.get("lon")
        if lat is None or lon is None:
            row["planning_basis"] = "אין קואורדינטות למודעה"
            continue
        try:
            point = Point(float(lon), float(lat))
        except (TypeError, ValueError):
            continue
        hits = []
        for idx in tree.query(point):
            # `query` returns bbox candidates; this is the exact test. `covers`
            # rather than `contains` so a point on the boundary counts.
            if geometries[idx].covers(point):
                hits.append(meta[idx])
        row["plans_count"] = len(hits)
        row["plan_units"] = sum(h["units"] for h in hits)
        densities = [h["density"] for h in hits if h["density"]]
        row["plan_density"] = round(max(densities), 1) if densities else None
        if hits:
            # The plan that matters is the most advanced one, and among equally
            # advanced ones the densest - not the one with the biggest headline
            # unit count, which is always the city-wide master plan.
            best = max(hits, key=lambda h: (h["rank"], h["density"] or 0))
            row["plan_status"] = best["status"]
            row["plan_number"] = best["number"]
            row["plan_stage_rank"] = best["rank"]
            density_note = (f" · {row['plan_density']} יח״ד/דונם"
                            if row["plan_density"] else " · צפיפות לא ניתנת לחישוב")
            row["planning_basis"] = (
                f"{len(hits)} תכניות על הנקודה · {row['plan_units']:,} יח״ד"
                f"{density_note} · המתקדמת: {best['status'] or '—'}")
        else:
            row["plan_stage_rank"] = None
            row["planning_basis"] = "אין תכנית מוסיפת יח״ד על הנקודה"

        town = renewal.get(str(row.get("city") or "").strip().lower())
        if town:
            row["renewal_complexes"] = town["complexes"]
            row["renewal_units_added"] = town["units_added"]
    return rows


def coverage(rows) -> dict:
    matched = sum(1 for r in rows if r.get("plans_count"))
    return {"rows": len(rows), "with_plan": matched,
            "with_renewal": sum(1 for r in rows if r.get("renewal_complexes"))}
