"""
PlanWatch - urban renewal (התחדשות עירונית) client
===================================================
Declared urban-renewal complexes from משרד הבינוי והשיכון, published on
data.gov.il as dataset `urban_renewal`. Verified live 2026-07: 969 complexes,
930 of them carrying a plan number, of which 698 join straight onto the
`plans` table by `pl_number`.

This is the piece the Blue Lines service does not have: a complex is the
*business* unit of urban renewal (פינוי בינוי / עיבוי בנייה), and it carries
what an investor actually asks - existing units, added units, planned units,
declaration date, track, and how many building permits have issued.

Related things checked and deliberately NOT imported
----------------------------------------------------
* Section 77/78 early notices - the Xplan_77_78 service (427 rows) is the same
  schema as the main layer, and 424 of them are ALREADY in `plans` as
  entity_subtype "הודעה לפי סעיף 77 ו 78". Importing again would duplicate.
* `pl_by_auth_of` is NOT a parent-plan pointer (it holds section codes like
  2.0 / 3.0), so it cannot be used to build plan lineage.
* חלקות/גושים shapefiles (data.gov.il `shape`, `subgushallshape`) are ZIP-only,
  no datastore API, and need a shapefile reader. GovMap search already resolves
  parcels, so it is not on the critical path.
"""

from __future__ import annotations

import json
import re
from legacy.tools.http_client import build_session

RESOURCE_ID = "f65a0daf-f737-49c5-9424-d378d52104f5"
SEARCH_URL = "https://data.gov.il/api/3/action/datastore_search"
PAGE_SIZE = 5_000

#: source column -> local column
COLMAP = {
    "MisparMitham": "mitham_id",
    "Yeshuv": "locality",
    "SemelYeshuv": "locality_code",
    "ShemMitcham": "name",
    "YachadKayam": "units_existing",
    "YachadTosafti": "units_added",
    "YachadMutza": "units_planned",
    "TaarichHachraza": "declared_at",
    "MisparTochnit": "plan_number",
    "SachHeterim": "permits",
    "Maslul": "track",
    "ShnatMatanTokef": "valid_year",
    "Bebitzua": "in_execution",
    "Status": "status",
    "KishurLatar": "url",
    "KishurLaMapa": "map_url",
}

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS urban_renewal (
    mitham_id      TEXT,
    locality       TEXT,
    locality_code  TEXT,
    name           TEXT,
    units_existing INTEGER,
    units_added    INTEGER,
    units_planned  INTEGER,
    declared_at    TEXT,
    plan_number    TEXT,
    permits        INTEGER,
    track          TEXT,
    valid_year     TEXT,
    in_execution   TEXT,
    status         TEXT,
    url            TEXT,
    map_url        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ur_plan     ON urban_renewal (plan_number);
CREATE INDEX IF NOT EXISTS idx_ur_locality ON urban_renewal (locality);
CREATE INDEX IF NOT EXISTS idx_ur_track    ON urban_renewal (track);
"""

_INT_COLS = {"units_existing", "units_added", "units_planned", "permits"}

#: Whitespace-stripped plan number, to match plans.pl_number_sq. See db.py.
SQ_PLAN = "replace(replace(replace(u.plan_number,' ',''),char(9),''),char(160),'')" 

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def _clean(value, column: str):
    """Source pads every string with a wall of spaces; ints arrive as floats."""
    if value is None:
        return None
    if column in _INT_COLS:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    # The source pads with regular AND non-breaking spaces; \xa0 survives
    # .strip() and shows up as "תכנית מאושרת\xa0-\xa0\xa0אחרי\xa0רישוי".
    text = " ".join(str(value).replace("\xa0", " ").split())
    if column == "declared_at" and text:
        # The source publishes DD/MM/YYYY, not ISO. Truncating to 10 chars left
        # "20/08/2006", so `substr(declared_at,1,4)` yielded "20/0" and every
        # date comparison or year grouping on this column was wrong.
        text = _to_iso_date(text)
    return text or None


_DMY_DATE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")


def _to_iso_date(text):
    """DD/MM/YYYY -> YYYY-MM-DD. Already-ISO passes through; junk returns None."""
    match = _DMY_DATE.match(text)
    if match:
        day, month, year = match.groups()
        if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            return f"{year}-{int(month):02d}-{int(day):02d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return None       # e.g. the single row that publishes "118"


def _repair_shifted(row: dict) -> dict:
    """
    One published row (mitham 5009236, רמת גן) has its trailing columns shifted
    by one: `Status` holds a year, `ShnatMatanTokef` holds the map URL, and
    `Bebitzua` holds the track. Confirmed upstream - the mapping here is by
    field NAME, so it is not a parsing artefact. Detect that exact signature and
    slide the values back rather than showing "סטטוס: 2025" in the UI.
    """
    status = (row.get("status") or "").strip()
    valid_year = (row.get("valid_year") or "").strip()
    if status.isdigit() and len(status) == 4 and valid_year.startswith("http"):
        row["map_url"] = row.get("map_url") or valid_year
        row["valid_year"] = status
        row["track"] = row.get("track") or (row.get("in_execution") or "").strip() or None
        row["in_execution"] = None
        row["status"] = None
        row["_repaired"] = True
    return row


def count_local(conn) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='urban_renewal'"
    ).fetchone()
    return conn.execute("SELECT COUNT(*) FROM urban_renewal").fetchone()[0] if exists else 0


def import_all(conn, progress=None, should_stop=None, timeout=(30, 120)) -> int:
    """
    Refresh the complex list into `urban_renewal` (staged, then swapped).
    Returns the row count, or 0 if cancelled.
    """
    conn.executescript(TABLE_SQL)
    columns = list(COLMAP.values())
    conn.execute("DROP TABLE IF EXISTS urban_renewal_new")
    conn.execute(f"CREATE TABLE urban_renewal_new ({', '.join(c + ' TEXT' for c in columns)})")
    placeholders = ",".join("?" for _ in columns)

    total, offset = 0, 0
    repaired = [0]
    while True:
        if should_stop and should_stop():
            conn.execute("DROP TABLE IF EXISTS urban_renewal_new")
            conn.commit()
            return 0
        resp = _sess().get(SEARCH_URL, params={
            "resource_id": RESOURCE_ID, "limit": PAGE_SIZE, "offset": offset,
        }, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(f"urban_renewal: search failed at {offset}")
        records = payload["result"]["records"]
        if not records:
            break

        batch = []
        for rec in records:
            row = {col: _clean(rec.get(src), col) for src, col in COLMAP.items()}
            row = _repair_shifted(row)
            if row.pop("_repaired", False):
                repaired[0] += 1
            batch.append(tuple(row[c] for c in columns))
        conn.executemany(
            f"INSERT INTO urban_renewal_new VALUES ({placeholders})", batch)
        conn.commit()
        total += len(batch)
        offset += len(records)
        if progress:
            progress("urban_renewal", total)
        if len(records) < PAGE_SIZE:
            break

    conn.execute("DROP TABLE IF EXISTS urban_renewal")
    conn.execute("ALTER TABLE urban_renewal_new RENAME TO urban_renewal")
    conn.executescript(TABLE_SQL)      # (re)create the indexes
    conn.commit()
    if progress:
        progress("urban_renewal", total)
    if repaired[0]:
        print(f"  (repaired {repaired[0]} row(s) with shifted source columns)")
    return total


def summary(conn) -> dict:
    """Headline numbers for the dashboard."""
    if not count_local(conn):
        return {"complexes": 0}
    row = conn.execute(
        """SELECT COUNT(*),
                  COALESCE(SUM(CAST(units_existing AS INTEGER)),0),
                  COALESCE(SUM(CAST(units_planned  AS INTEGER)),0),
                  COALESCE(SUM(CAST(units_added    AS INTEGER)),0),
                  COALESCE(SUM(CAST(permits        AS INTEGER)),0)
           FROM urban_renewal""").fetchone()
    tracks = [dict(r) for r in conn.execute(
        """SELECT COALESCE(track,'לא צויין') label, COUNT(*) n
           FROM urban_renewal GROUP BY 1 ORDER BY n DESC""")]
    statuses = [dict(r) for r in conn.execute(
        """SELECT COALESCE(status,'לא צויין') label, COUNT(*) n
           FROM urban_renewal GROUP BY 1 ORDER BY n DESC LIMIT 12""")]
    # Normalised join: `plans` stores "תמל/ 1064" and this feed publishes
    # "תמל/1064", so matching on pl_number alone lost 56 complexes (698 -> 754).
    linked = conn.execute(
        """SELECT COUNT(*) FROM urban_renewal u
           WHERE EXISTS (SELECT 1 FROM plans p
                         WHERE p.pl_number_sq = """ + SQ_PLAN + """)"""
    ).fetchone()[0]
    return {"complexes": row[0], "units_existing": row[1],
            "units_planned": row[2], "units_added": row[3], "permits": row[4],
            "linked_to_plans": linked, "tracks": tracks, "statuses": statuses}


def search(conn, locality: str = "", track: str = "", status: str = "",
           min_added: int | None = None, text: str = "",
           limit: int = 100, offset: int = 0) -> list[dict]:
    """Filtered complex list, each row carrying its matched plan when there is one."""
    if not count_local(conn):
        return []
    clauses, params = ["1=1"], []
    if locality:
        clauses.append("u.locality LIKE ?")
        params.append(f"%{locality}%")
    if track:
        clauses.append("u.track = ?")
        params.append(track)
    if status:
        clauses.append("u.status LIKE ?")
        params.append(f"%{status}%")
    if min_added is not None:
        clauses.append("CAST(u.units_added AS INTEGER) >= ?")
        params.append(min_added)
    if text:
        clauses.append("(u.name LIKE ? OR u.plan_number LIKE ? OR u.mitham_id LIKE ?)")
        params += [f"%{text}%"] * 3

    rows = conn.execute(
        f"""SELECT u.*, p.object_id, p.pl_name, p.short_status, p.station,
                   p.area_dunam, p.jurisdiction_name
            FROM urban_renewal u
            LEFT JOIN plans p ON p.pl_number_sq = replace(replace(replace(u.plan_number,' ',''),char(9),''),char(160),'')
            WHERE {" AND ".join(clauses)}
            ORDER BY CAST(u.units_added AS INTEGER) DESC NULLS LAST,
                     u.mitham_id
            LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
    return [dict(r) for r in rows]


def for_plan(conn, plan_number: str) -> list[dict]:
    """Complexes attached to a given plan number (used by the plan drawer)."""
    if not plan_number or not count_local(conn):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT * FROM urban_renewal WHERE plan_number = ?", (plan_number,))]


def near_point(conn, lat: float, lon: float, radius_m: float = 1500,
               limit: int = 20) -> list[dict]:
    """
    Complexes whose plan polygon is near a point. The complex table has no
    geometry of its own, so this rides the plan geometry via the join.
    """
    from legacy.tools import sync
    from shapely.geometry import Point

    if not count_local(conn):
        return []
    pad_lon, pad_lat = sync._radius_pads(radius_m, lat)
    point = Point(lon, lat)
    out = []
    for plan in db_plans_near(conn, lon, lat, pad_lon, pad_lat):
        hit, method = sync._point_hits_plan(point, plan, radius_m)
        if not hit:
            continue
        for complex_ in for_plan(conn, plan["pl_number"]):
            geom = sync._load_geometry(plan)
            complex_["distance_m"] = 0.0 if method == "point_in_polygon" else \
                round(sync._distance_m(geom, point, lat), 1)
            complex_["object_id"] = plan["object_id"]
            complex_["pl_name"] = plan["pl_name"]
            out.append(complex_)
    out.sort(key=lambda c: c["distance_m"])
    return out[:limit]


def db_plans_near(conn, lon, lat, pad_lon, pad_lat):
    import db
    return db.plans_covering_point(conn, lon, lat, pad_lon=pad_lon, pad_lat=pad_lat)


if __name__ == "__main__":
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    conn = db.get_conn()
    n = import_all(conn, progress=lambda k, t: print(f"  {t:,}", end="\r"))
    print(f"\nimported {n:,} urban-renewal complexes")
    print(json.dumps(summary(conn), ensure_ascii=False, indent=1)[:900])
    conn.close()
