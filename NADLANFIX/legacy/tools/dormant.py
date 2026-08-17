"""
PlanWatch - dormant land + post-import cross-referencing (שטחים מתים והצלבות)
==============================================================================
Two things that only become possible once the other feeds are loaded.

1. Dormant land (שטחים מתים)
----------------------------
Land where the *right* to build exists but nothing happened. Five independent
detectors, each from a different source, each verified against live data:

| detector | signal | measured |
|---|---|---|
| `approved_no_permit` | approved residential plan, no building permit filed | 15,444 plans |
| `declared_stalled` | renewal complex declared >=3y ago, still 0 permits | **121** complexes |
| `tender_unsold` | RAMI lot offered, tender closed, no winner | 52 lots |
| `empty_parcels` | parcel polygon with no building footprint in its grid cells | **2,382** parcels |
| `plan_no_progress` | plan approved long ago, units, no construction reported | 613 plans |

Longest stalled, measured: פתח תקוה "שכונת עקיבא" declared 2000-11-19 with 72
existing flats - **25.7 years and still no permit**.

`stalled_years` is the headline: how long the right has sat unused. A complex
declared in 2000 with no permit in 26 years is a different proposition from one
declared last year.

**What this is NOT.** "Dormant" here means *no recorded activity in the sources
PlanWatch holds*. It does not prove the land is unused, unencumbered or for
sale. A parcel can be actively farmed, tied up in litigation, or subject to a
permit filed with a municipality whose GIS is not public (nine of ten probed
are not). Treat a hit as a lead to check, never as a conclusion.

Inheritance / estates (ירושות)
------------------------------
**Not detectable from any source here, and no field is faked for it.**
`tabu_assets.ownership` has exactly five values - פרטית / מדינה / רשות מקומית /
מעורב / אחר - and none of them encodes an estate. Inheritance appears in the
נסח טאבו itself (as a הערת אזהרה or a צו ירושה), which is per-parcel, paid, and
reached through `credentialed_client.fetch_extract()`. `estate_candidates()`
below returns the weak proxies that ARE visible and says plainly that they are
proxies.

2. Post-import cross-referencing
--------------------------------
`crossref_new()` finds rows that arrived in the last import and joins them
against everything already held - the "what does this new tender/appraisal touch
that I already know about" question. Run it from the scheduler after any import;
it is read-only and cheap.
"""

from __future__ import annotations

from datetime import date

#: A right that has sat unused this long is worth surfacing. Below it, "nothing
#: happened yet" is just normal lead time.
MIN_STALLED_YEARS = 3


def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def _years_since(iso) -> float | None:
    if not iso or len(str(iso)) < 4 or not str(iso)[:4].isdigit():
        return None
    try:
        year, month = int(str(iso)[:4]), int(str(iso)[5:7] or 1)
    except ValueError:
        return None
    if not 1900 <= year <= 2100:
        return None
    today = date.today()
    return round((today.year - year) + (today.month - max(1, month)) / 12.0, 1)


# ------------------------------------------------------------- detectors

def approved_no_permit(conn, locality="", min_units=1, limit=100,
                       offset=0) -> dict:
    """
    Approved residential plans with no building permit on record.

    The permit side is Tel-Aviv-only, so outside Tel Aviv this cannot
    distinguish "no permit" from "no permit data" - the response says which.
    """
    clauses = ["p.station LIKE '%אישור%'", "p.landuse LIKE '%מגורים%'",
               "p.housing_units >= ?"]
    params: list = [min_units]
    if locality:
        clauses.append("p.jurisdiction_name LIKE ?")
        params.append(f"%{locality}%")

    permits_known = _has(conn, "muni_permits")
    where = " AND ".join(clauses)
    total = conn.execute(
        f"SELECT COUNT(*) FROM plans p WHERE {where}", params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"""SELECT p.object_id, p.pl_number, p.pl_name, p.jurisdiction_name,
                   p.district_name, p.housing_units, p.area_dunam,
                   p.station, p.last_update, p.pl_url
            FROM plans p WHERE {where}
            ORDER BY p.housing_units DESC, p.object_id
            LIMIT ? OFFSET ?""", params + [limit, offset])]
    for row in rows:
        row["stalled_years"] = _years_since(row["last_update"])
        row["permit_data_available"] = bool(
            permits_known and row["jurisdiction_name"]
            and "תל אביב" in row["jurisdiction_name"])
    return {"total": total, "rows": rows, "limit": limit, "offset": offset,
            "permits_source": "תל אביב-יפו בלבד" if permits_known else "לא יובא",
            "note": "תכניות מגורים מאושרות. היעדר היתר מוכח רק בתל אביב — "
                    "בשאר הארץ אין נתוני היתרים, ולכן זו אינדיקציה ולא הוכחה."}


def declared_stalled(conn, min_years=MIN_STALLED_YEARS, limit=100,
                     offset=0) -> dict:
    """
    Renewal complexes declared long ago that still have no building permit.

    The strongest dormant signal in the system: the state formally declared the
    site for renewal, and nothing followed. `declared_at` had to be fixed first
    - the source publishes DD/MM/YYYY and it was being stored unconverted, so
    every year comparison on it was wrong.
    """
    if not _has(conn, "urban_renewal"):
        return {"total": 0, "rows": [], "note": "מאגר ההתחדשות לא יובא."}
    rows = []
    for row in conn.execute(
        """SELECT u.*, p.object_id, p.pl_name, p.station, p.district_name
           FROM urban_renewal u
           LEFT JOIN plans p ON p.pl_number_sq =
             replace(replace(replace(u.plan_number,' ',''),char(9),''),char(160),'')
           WHERE COALESCE(CAST(u.permits AS INTEGER), 0) = 0
             AND u.declared_at IS NOT NULL"""):
        item = dict(row)
        years = _years_since(item["declared_at"])
        if years is None or years < min_years:
            continue
        item["stalled_years"] = years
        item["units_existing_n"] = _int(item.get("units_existing"))
        item["units_planned_n"] = _int(item.get("units_planned"))
        rows.append(item)
    rows.sort(key=lambda r: -r["stalled_years"])
    return {"total": len(rows), "rows": rows[offset:offset + limit],
            "limit": limit, "offset": offset, "min_years": min_years,
            "note": "מתחמים שהוכרזו ומעולם לא הוצא בהם היתר. האות החזק ביותר "
                    "לזכות בנייה שיושבת ללא מימוש."}


def tender_unsold(conn, limit=100, offset=0) -> dict:
    """RAMI lots that were offered, the tender closed, and nobody won."""
    if not _has(conn, "tender_lots"):
        return {"total": 0, "rows": [], "note": "מאגר המכרזים לא יובא."}
    total = conn.execute(
        """SELECT COUNT(*) FROM tender_lots l
           JOIN tenders t ON t.michraz_id = l.michraz_id
           WHERE l.winner IS NULL AND t.status_code = 5""").fetchone()[0]
    rows = []
    for row in conn.execute(
        """SELECT l.*, t.name, t.locality, t.neighborhood, t.published_at,
                  t.status, t.housing_units
           FROM tender_lots l JOIN tenders t ON t.michraz_id = l.michraz_id
           WHERE l.winner IS NULL AND t.status_code = 5
           ORDER BY t.published_at DESC, l.tik_id
           LIMIT ? OFFSET ?""", (limit, offset)):
        item = dict(row)
        item["stalled_years"] = _years_since(item["published_at"])
        rows.append(item)
    return {"total": total, "rows": rows, "limit": limit, "offset": offset,
            "note": "מגרשים שהוצעו במכרז שהסתיים ולא נמצא להם זוכה — "
                    "קרקע שהמדינה ניסתה למכור ולא הצליחה."}


#: Grid cell size in degrees. ~0.002 deg is roughly 200 m, so a cell holds a
#: handful of parcels and a handful of buildings.
_GRID = 0.002


def _cells(min_lon, min_lat, max_lon, max_lat):
    """Every grid cell key a bbox touches."""
    if None in (min_lon, min_lat, max_lon, max_lat):
        return []
    out = []
    x, y0 = int(min_lon / _GRID), int(min_lat / _GRID)
    x1, y1 = int(max_lon / _GRID), int(max_lat / _GRID)
    # A parcel spanning an implausible area is bad data; cap the fan-out.
    if (x1 - x) > 40 or (y1 - y0) > 40:
        return [f"{x}:{y0}"]
    while x <= x1:
        y = y0
        while y <= y1:
            out.append(f"{x}:{y}")
            y += 1
        x += 1
    return out


def build_grid(conn, city="tel-aviv") -> dict:
    """
    Index building footprints into grid cells.

    **Why this exists.** The obvious query - `NOT EXISTS (… b.min_lon <=
    p.max_lon AND b.max_lon >= p.min_lon AND …)` - cannot use the bbox index:
    a four-sided range predicate only ever uses the index's FIRST column, so
    SQLite falls back to scanning 45,837 buildings for each of 45,206 parcels.
    That is ~2 billion comparisons and it did not finish in 10 minutes.

    A grid cell is an equality join, which the index serves instantly.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS muni_building_grid (
            city TEXT NOT NULL, cell TEXT NOT NULL, object_id INTEGER NOT NULL,
            PRIMARY KEY (city, cell, object_id)
        );
        CREATE INDEX IF NOT EXISTS idx_bgrid ON muni_building_grid (city, cell);
    """)
    conn.execute("DELETE FROM muni_building_grid WHERE city = ?", (city,))
    rows = []
    for row in conn.execute(
        """SELECT object_id, min_lon, min_lat, max_lon, max_lat
           FROM muni_buildings WHERE city = ? AND min_lon IS NOT NULL""",
        (city,)):
        for cell in _cells(row["min_lon"], row["min_lat"],
                           row["max_lon"], row["max_lat"]):
            rows.append((city, cell, row["object_id"]))
    conn.executemany(
        """INSERT INTO muni_building_grid (city, cell, object_id)
           VALUES (?,?,?) ON CONFLICT DO NOTHING""", rows)
    conn.commit()
    return {"city": city, "grid_rows": len(rows),
            "cells": conn.execute(
                "SELECT COUNT(DISTINCT cell) FROM muni_building_grid WHERE city=?",
                (city,)).fetchone()[0]}


def build_permit_grid(conn, city="tel-aviv") -> dict:
    """Same grid trick for permits - see build_grid() for why."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS muni_permit_grid (
            city TEXT NOT NULL, cell TEXT NOT NULL, object_id INTEGER NOT NULL,
            PRIMARY KEY (city, cell, object_id)
        );
        CREATE INDEX IF NOT EXISTS idx_pgrid ON muni_permit_grid (city, cell);
    """)
    if conn.execute("SELECT 1 FROM muni_permit_grid WHERE city=? LIMIT 1",
                    (city,)).fetchone():
        return {"city": city, "cached": True}
    rows = []
    for row in conn.execute(
        """SELECT object_id, min_lon, min_lat, max_lon, max_lat
           FROM muni_permits WHERE city = ? AND min_lon IS NOT NULL""",
        (city,)):
        for cell in _cells(row["min_lon"], row["min_lat"],
                           row["max_lon"], row["max_lat"]):
            rows.append((city, cell, row["object_id"]))
    conn.executemany(
        """INSERT INTO muni_permit_grid (city, cell, object_id)
           VALUES (?,?,?) ON CONFLICT DO NOTHING""", rows)
    conn.commit()
    return {"city": city, "grid_rows": len(rows)}


def empty_parcels(conn, city="tel-aviv", min_area_m2=400, limit=100,
                  offset=0) -> dict:
    """
    Parcel polygons with no building footprint in any grid cell they touch.

    A SCREEN, not a proof: grid-cell overlap is coarser than true geometric
    intersection, so a parcel adjacent to a building can share a cell and be
    excluded. It errs toward MISSING dormant parcels rather than inventing
    them, which is the right direction for a lead list.
    """
    if not (_has(conn, "muni_parcels") and _has(conn, "muni_buildings")):
        return {"total": 0, "rows": [], "note": "השכבות העירוניות לא יובאו."}
    if not _has(conn, "muni_building_grid") or not conn.execute(
            "SELECT 1 FROM muni_building_grid WHERE city=? LIMIT 1",
            (city,)).fetchone():
        build_grid(conn, city)

    candidates = conn.execute(
        """SELECT object_id, gush, helka, area_computed_m2,
                  area_registered_m2, min_lon, min_lat, max_lon, max_lat
           FROM muni_parcels
           WHERE city = ? AND geometry_json IS NOT NULL
             AND COALESCE(area_computed_m2, 0) >= ?
           ORDER BY area_computed_m2 DESC""", (city, min_area_m2)).fetchall()

    empty = []
    for row in candidates:
        cells = _cells(row["min_lon"], row["min_lat"],
                       row["max_lon"], row["max_lat"])
        if not cells:
            continue
        marks = ",".join("?" for _ in cells)
        hit = conn.execute(
            f"""SELECT 1 FROM muni_building_grid
                WHERE city = ? AND cell IN ({marks}) LIMIT 1""",
            [city] + cells).fetchone()
        if not hit:
            item = {k: row[k] for k in row.keys()
                    if k not in ("min_lon", "min_lat", "max_lon", "max_lat")}
            empty.append(item)
    return {"total": len(empty), "rows": empty[offset:offset + limit],
            "limit": limit, "offset": offset,
            "city": city, "min_area_m2": min_area_m2,
            "method": "grid-cell screen (coarser than exact geometry)",
            "note": "חלקות ללא טביעת רגל של מבנה. סינון לפי תא רשת — "
                    "מהיר, אבל גס מחיתוך גיאומטרי מדויק. נוטה **לפספס** "
                    "חלקות ריקות ולא להמציא אותן."}


def plan_no_progress(conn, before="2021-01-01", min_units=50, limit=100,
                     offset=0) -> dict:
    """Plans approved years ago with real unit counts and no reported building."""
    progress_known = _has(conn, "building_progress")
    join = ""
    if progress_known:
        join = """ AND NOT EXISTS (
                    SELECT 1 FROM building_progress bp
                    WHERE bp.gush IS NOT NULL
                      AND bp.locality = p.jurisdiction_name)"""
    where = ("p.station LIKE '%אישור%' AND p.housing_units >= ? "
             "AND p.last_update < ?" + join)
    params = [min_units, before]
    total = conn.execute(
        f"SELECT COUNT(*) FROM plans p WHERE {where}", params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"""SELECT p.object_id, p.pl_number, p.pl_name, p.jurisdiction_name,
                   p.housing_units, p.area_dunam, p.station, p.last_update,
                   p.pl_url
            FROM plans p WHERE {where}
            ORDER BY p.housing_units DESC, p.object_id
            LIMIT ? OFFSET ?""", params + [limit, offset])]
    for row in rows:
        row["stalled_years"] = _years_since(row["last_update"])
    return {"total": total, "rows": rows, "limit": limit, "offset": offset,
            "progress_data": progress_known,
            "note": "תכניות מאושרות עם יח\"ד שלא עודכנו שנים ואין דיווח "
                    "בנייה בישוב. דיווחי הבנייה מכסים בנייה רוויה בלבד."}


DETECTORS = {
    "approved_no_permit": approved_no_permit,
    "declared_stalled": declared_stalled,
    "tender_unsold": tender_unsold,
    "empty_parcels": empty_parcels,
    "plan_no_progress": plan_no_progress,
}


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


#: Counts that cost a full scan are cached here by the `grids` scheduler job.
#: `empty_parcels` walks 45,206 parcels doing a grid lookup each - 15s, which
#: made `/api/dormant` take 22.6s and left the tab blank because the page gave
#: up first. The count changes only when the municipal layers change.
_SUMMARY_TABLE = """
CREATE TABLE IF NOT EXISTS dormant_summary (
    detector TEXT PRIMARY KEY,
    total    INTEGER,
    computed_at TEXT
);
"""


def refresh_summary(conn) -> dict:
    """Recompute and store every detector's count. Called by the scheduler."""
    import db as _db

    conn.executescript(_SUMMARY_TABLE)
    now = _db.now_iso()
    out = {}
    for name, fn in DETECTORS.items():
        try:
            total = fn(conn, limit=1).get("total", 0)
        except Exception as exc:
            total = None
            out[f"{name}_error"] = type(exc).__name__
        out[name] = total
        conn.execute(
            """INSERT INTO dormant_summary (detector, total, computed_at)
               VALUES (?,?,?) ON CONFLICT (detector) DO UPDATE SET
                 total=excluded.total, computed_at=excluded.computed_at""",
            (name, total, now))
    conn.commit()
    out["computed_at"] = now
    return out


def summary(conn, recompute=False) -> dict:
    """
    Headline counts for every detector.

    Served from `dormant_summary` when available: computing them live costs a
    45k-parcel scan and makes the endpoint unusable for a page load. `stale`
    tells the caller how old the numbers are, and the fast detectors are
    always recomputed live since they are indexed lookups.
    """
    conn.executescript(_SUMMARY_TABLE)
    if recompute:
        return refresh_summary(conn)

    cached = {r["detector"]: r for r in conn.execute(
        "SELECT detector, total, computed_at FROM dormant_summary")}
    out, computed_at = {}, None
    for name, fn in DETECTORS.items():
        # These are indexed and answer in milliseconds; no reason to cache them.
        if name in ("declared_stalled", "tender_unsold", "approved_no_permit",
                    "plan_no_progress"):
            try:
                out[name] = fn(conn, limit=1).get("total", 0)
            except Exception as exc:
                out[name] = f"error: {type(exc).__name__}"
            continue
        row = cached.get(name)
        if row is None:
            out[name] = None
            out[f"{name}_note"] = "לא חושב עדיין — הרץ את job 'grids'"
        else:
            out[name] = row["total"]
            computed_at = computed_at or row["computed_at"]

    out["cached_at"] = computed_at
    out["note"] = ('"שטח מת" = אין פעילות מתועדת במקורות שברשות המערכת. '
                   'זו רשימת בדיקה, לא הוכחה שהקרקע פנויה או למכירה.')
    return out


# ------------------------------------------------- estates (weak proxies only)

def estate_candidates(conn, limit=60, offset=0) -> dict:
    """
    Weak proxies for estate/inheritance situations, labelled as such.

    There is NO inheritance field in any source here: `tabu_assets.ownership`
    holds only פרטית / מדינה / רשות מקומית / מעורב / אחר. Inheritance shows up
    in the נסח טאבו (צו ירושה, הערת אזהרה), which is per-parcel and paid.

    What IS visible: a parcel held privately, with MANY registered sub-assets
    (which often means many co-owners after a succession), and no recent
    activity. That is a hint worth a נסח, not a finding.
    """
    if not _has(conn, "tabu_assets"):
        return {"total": 0, "rows": [], "note": "פנקסי המקרקעין לא יובאו."}
    rows = [dict(r) for r in conn.execute(
        """SELECT gush, helka, COUNT(*) sub_assets,
                  COUNT(DISTINCT ownership) ownership_kinds
           FROM tabu_assets
           WHERE ownership = 'פרטית' AND helka IS NOT NULL
           GROUP BY gush, helka
           HAVING COUNT(*) >= 8
           ORDER BY sub_assets DESC, gush, helka
           LIMIT ? OFFSET ?""", (limit, offset))]
    return {
        "total": len(rows), "rows": rows, "limit": limit, "offset": offset,
        "is_proxy": True,
        "note": "אין שדה ירושה באף מקור פתוח. tabu_assets.ownership מכיל חמישה "
                "ערכים בלבד (פרטית/מדינה/רשות מקומית/מעורב/אחר). מה שמוצג כאן: "
                "חלקות בבעלות פרטית עם הרבה תת-חלקות רשומות — לעיתים ריבוי "
                "בעלים לאחר הורשה. **זהו רמז בלבד.**",
        "authoritative": "צו ירושה והערות אזהרה מופיעים בנסח טאבו רשמי — "
                         "POST /api/tabu/extract.",
    }


# ------------------------------------------------- post-import cross-reference

def crossref_new(conn, since=None, limit=40) -> dict:
    """
    What did the newest imported rows touch that the system already knew?

    Read-only. Answers the question a scheduled import raises: a tender or
    appraisal just arrived - does it sit on a parcel with an approved plan, a
    declared renewal complex, or a dangerous-building order?
    """
    out: dict = {"since": since, "links": []}

    if _has(conn, "tender_lots") and _has(conn, "plans"):
        rows = [dict(r) for r in conn.execute(
            """SELECT l.gush, l.helka, l.plan_number, t.name, t.locality,
                      t.published_at, l.appraised_price,
                      p.object_id, p.pl_name, p.station, p.housing_units
               FROM tender_lots l
               JOIN tenders t ON t.michraz_id = l.michraz_id
               JOIN plans p ON p.pl_number_sq =
                 replace(replace(replace(l.plan_number,' ',''),char(9),''),char(160),'')
               WHERE l.plan_number IS NOT NULL
               ORDER BY t.published_at DESC LIMIT ?""", (limit,))]
        out["links"].append({"kind": "tender_to_plan", "count": len(rows),
                             "label": "מגרשי מכרז ↔ תכניות", "rows": rows})

    if _has(conn, "appraisal_parcels") and _has(conn, "tender_lots"):
        # Appraisal <-> tender lot on the SAME PARCEL. The earlier version
        # joined appraisals to renewal complexes on `locality = committee`,
        # which is not a link at all: 30k appraisals x 969 complexes sharing a
        # town produced 883,687 rows - a near-cross-product masquerading as a
        # finding. gush+helka is an actual identity.
        rows = [dict(r) for r in conn.execute(
            """SELECT ap.gush, ap.helka, a.decision_date, a.committee,
                      a.appraisal_type, l.michraz_id, l.appraised_price,
                      l.winning_price, t.name AS tender_name, t.published_at
               FROM appraisal_parcels ap
               JOIN appraisals a ON a.id = ap.appraisal_id
               JOIN tender_lots l ON l.gush = ap.gush AND l.helka = ap.helka
               JOIN tenders t ON t.michraz_id = l.michraz_id
               ORDER BY a.decision_date DESC LIMIT ?""", (limit,))]
        out["links"].append({
            "kind": "appraisal_to_tender", "count": len(rows),
            "label": "שמאות ↔ מגרשי מכרז (אותה חלקה)", "rows": rows})

    if _has(conn, "muni_dangerous") and _has(conn, "muni_permits"):
        # Per-order grid lookup instead of one big range join. The range form
        # took 89.8s for 227 rows: a four-sided bbox predicate cannot use the
        # index past its first column.
        build_permit_grid(conn)
        rows = []
        for order in conn.execute(
            """SELECT city, object_id, street, house_num, order_kind, lat, lon
               FROM muni_dangerous WHERE lat IS NOT NULL LIMIT ?""",
                (limit * 4,)):
            cell = f"{int(order['lon'] / _GRID)}:{int(order['lat'] / _GRID)}"
            for permit in conn.execute(
                """SELECT pm.request_num, pm.addresses, pm.is_tama38, pm.stage
                   FROM muni_permit_grid g
                   JOIN muni_permits pm ON pm.city = g.city
                        AND pm.object_id = g.object_id
                   WHERE g.city = ? AND g.cell = ? LIMIT 3""",
                    (order["city"], cell)):
                rows.append({**{k: order[k] for k in
                                ("street", "house_num", "order_kind",
                                 "lat", "lon")}, **dict(permit)})
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        out["links"].append({
            "kind": "dangerous_to_permit", "count": len(rows),
            "label": "מבנים מסוכנים ↔ בקשות היתר (אותו תא רשת)", "rows": rows})

    out["total_links"] = sum(link["count"] for link in out["links"])
    out["note"] = ("הצלבות שנוצרות רק אחרי שכל הפידים נטענו. קריאה בלבד — "
                   "בטוח להריץ אחרי כל ייבוא.")
    return out


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    assert _years_since("2000-01-01") and _years_since("2000-01-01") > 25
    assert _years_since("118") is None
    assert _years_since(None) is None
    assert _years_since("20/08/2006") is None      # DD/MM must be converted first
    print("self-tests passed\n")

    conn = db.get_conn()
    print("summary:", json.dumps(summary(conn), ensure_ascii=False, indent=1))
    print("\n=== longest-stalled declared complexes ===")
    d = declared_stalled(conn, limit=8)
    print(f"  {d['total']} complexes stalled >= {d['min_years']}y")
    for r in d["rows"]:
        print(f"   {r['stalled_years']:>5}y  {str(r['locality'])[:14]:14} "
              f"{str(r['name'])[:26]:26} declared {r['declared_at']} "
              f"exist={r['units_existing_n']}")
    print("\n=== unsold tender lots ===")
    d = tender_unsold(conn, limit=5)
    for r in d["rows"]:
        print(f"   {r['stalled_years']:>5}y  {str(r['locality'])[:14]:14} "
              f"{str(r['name'])[:12]:12} ג{r['gush']} ח{r['helka']} "
              f"shuma={r['appraised_price']}")
    print("\ncrossref:", json.dumps(
        {k: v for k, v in crossref_new(conn, limit=3).items() if k != "links"},
        ensure_ascii=False))
    for link in crossref_new(conn, limit=3)["links"]:
        print(f"   {link['label']:38} {link['count']}")
    conn.close()
