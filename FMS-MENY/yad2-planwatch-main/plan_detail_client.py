"""
PlanWatch - plan interior client (תוכן התכנית, לא רק הקו הכחול)
================================================================
Until now PlanWatch held only layer 1 of the Xplan service: the blue line -
one polygon per plan, with the plan's headline attributes. That answers "where
is the plan" but not "what does the plan actually say".

This module pulls the other four layers of the SAME MapServer, which together
are the plan's contents:

    layer 0  ישויות נקודתיות     point entities   (blocks, engineering sites, antiquities)
    layer 2  ישויות קוויות       line entities    (promenades, walls, section lines)
    layer 3  ישויות פוליגונליות  polygon entities (מתחם boundaries, envelopes)
    layer 4  יעודי קרקע          LAND-USE CELLS   <- the important one

Layer 4 is the payload a broker actually wants. One row per תא שטח (land-use
cell) inside the plan, each carrying:

    num          תא שטח identifier as printed on the תשריט
    mavat_name   the zoning designation ("מגורים", "מגורים מסחר ותעסוקה", ...)
    mavat_code   the מבא"ת code behind that name (stable; the name is not)
    legal_area   registered area in DUNAM per the plan document
    shape_area   computed area in SQUARE METRES from the geometry

Verified live 2026-07-28 on plan 408-0242412 (נת/2035, תכנית מתאר נתניה):
172 land-use cells, 64 polygon entities, 60 line entities, 16 point entities.

Two field traps, both real
--------------------------
* `legal_area` is DUNAM, `shape_area` is SQUARE METRES. Different units in
  adjacent columns of the same row. Mixing them is a 1000x error.
* `legal_area` is frequently 0.0 even on approved plans (all 172 cells of the
  test plan). It means "not stated in the document", NOT "zero area". Treat 0
  as NULL and fall back to `shape_area`; never divide by it.

What this module does NOT do
---------------------------
The plan's *documents* - the takanon (הוראות התכנית) and the tasrit (תשריט)
PDFs - live on mavat.iplan.gov.il, whose API is reCAPTCHA-gated by design
(`IsReCaptcha:!0` with a site key sits in its own JS bundle,
main.64ac1de054ec96de.js). PlanWatch does not attempt to get past that.
`mavat_plan_url()` below builds the official deep link so a human can open the
document in one click, which is the supported path.

Correction to an earlier note in this project: mavat's `robots.txt` does NOT
broadly disallow crawling - it only disallows the file extensions
`.gif/.jpg/.jpeg/.pdf`. The blocker there is the reCAPTCHA, not robots.txt.
"""

from __future__ import annotations

import json

from http_client import DEFAULT_TIMEOUT, assert_json_response, build_session

SERVICE_URL = ("https://ags.iplan.gov.il/arcgisiplan/rest/services/"
               "PlanningPublic/Xplan/MapServer")

#: layer id -> (local kind, Hebrew label)
LAYERS = {
    0: ("point", "ישויות נקודתיות"),
    2: ("line", "ישויות קוויות"),
    3: ("polygon", "ישויות פוליגונליות"),
    4: ("landuse", "יעודי קרקע"),
}
LANDUSE_LAYER = 4

MAX_PAGE_SIZE = 1000

#: Fields that exist on layer 4 only; the entity layers do not have them.
_LANDUSE_ONLY = ("num", "legal_area")

TABLES = """
CREATE TABLE IF NOT EXISTS plan_landuse (
    object_id    INTEGER PRIMARY KEY,   -- ArcGIS objectid of the CELL
    mp_id        INTEGER,               -- joins plans.mp_id
    pl_number    TEXT,                  -- joins plans.pl_number
    pl_name      TEXT,
    cell_num     TEXT,                  -- תא שטח as printed on the תשריט
    mavat_code   INTEGER,               -- stable code; prefer over the name
    mavat_name   TEXT,                  -- designation, e.g. "מגורים"
    legal_area_dunam REAL,              -- DUNAM, NULL when the doc omits it
    shape_area_m2    REAL,              -- SQUARE METRES, from the geometry
    perimeter_m  REAL,
    station      INTEGER,
    station_desc TEXT,
    layer_id     INTEGER,
    last_update  TEXT,
    geometry_json TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
    fetched_at   TEXT
);

CREATE TABLE IF NOT EXISTS plan_entities (
    object_id    INTEGER NOT NULL,
    kind         TEXT NOT NULL,         -- point | line | polygon
    mp_id        INTEGER,
    pl_number    TEXT,
    pl_name      TEXT,
    mavat_code   INTEGER,
    mavat_name   TEXT,
    shape_area_m2   REAL,
    shape_length_m  REAL,
    station      INTEGER,
    station_desc TEXT,
    layer_id     INTEGER,
    last_update  TEXT,
    geometry_json TEXT,
    fetched_at   TEXT,
    PRIMARY KEY (kind, object_id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_lu_mp      ON plan_landuse (mp_id);
CREATE INDEX IF NOT EXISTS idx_lu_plnum   ON plan_landuse (pl_number);
CREATE INDEX IF NOT EXISTS idx_lu_code    ON plan_landuse (mavat_code);
CREATE INDEX IF NOT EXISTS idx_lu_bbox    ON plan_landuse (min_lon, max_lon, min_lat, max_lat);
CREATE INDEX IF NOT EXISTS idx_ent_mp     ON plan_entities (mp_id);
CREATE INDEX IF NOT EXISTS idx_ent_plnum  ON plan_entities (pl_number);
"""

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


# ------------------------------------------------------------------ helpers

def _num(value):
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _dunam(value):
    """
    `legal_area` is in DUNAM and is 0.0 whenever the plan document does not
    state it - which is most of the time. 0 means "unstated", not "no area",
    so it becomes NULL rather than a number someone might divide by.
    """
    out = _num(value)
    return out if out else None


def _epoch_ms_to_iso(value):
    """ArcGIS Date fields come back as epoch milliseconds."""
    if value in (None, ""):
        return None
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(value) / 1000.0,
                                      tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _text(value):
    if value is None:
        return None
    out = " ".join(str(value).replace("\xa0", " ").split())
    return out or None


def _bbox(geometry):
    """Bounding box straight off a GeoJSON geometry, no shapely needed."""
    if not geometry:
        return (None, None, None, None)
    xs, ys = [], []

    def walk(node):
        if isinstance(node, (int, float)):
            return
        if (node and isinstance(node, list) and len(node) >= 2
                and all(isinstance(c, (int, float)) for c in node[:2])):
            xs.append(node[0])
            ys.append(node[1])
            return
        for child in node or []:
            walk(child)

    walk(geometry.get("coordinates"))
    if not xs:
        return (None, None, None, None)
    return (min(xs), min(ys), max(xs), max(ys))


def mavat_plan_url(pl_number: str) -> str:
    """
    Official mavat deep link for a plan's documents (takanon + tasrit).

    The documents themselves are behind mavat's reCAPTCHA, so this is a link
    for a person to click - not something to fetch. Kept here so the UI always
    has the supported route to the source document.
    """
    from urllib.parse import quote
    return ("https://mavat.iplan.gov.il/SV4?text="
            f"{quote(str(pl_number or '').strip())}")


# -------------------------------------------------------------------- fetch

def iter_layer(layer_id: int, where: str = "1=1", *, session=None,
               page_size: int = MAX_PAGE_SIZE, timeout=DEFAULT_TIMEOUT,
               include_geometry: bool = True, progress=None, should_stop=None):
    """
    Page one layer of the Xplan MapServer.

    Same paging contract as blue_lines_client.iter_plans: `resultOffset` plus a
    stable `orderByFields=objectid ASC`. Without the stable sort the service
    can repeat or skip rows between pages, which silently truncates the pull.
    """
    session = session or _sess()
    offset = 0
    while True:
        if should_stop and should_stop():
            return
        params = {
            "where": where,
            "outFields": "*",
            "returnGeometry": "true" if include_geometry else "false",
            "outSR": 4326,
            "f": "geojson" if include_geometry else "json",
            "orderByFields": "objectid ASC",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        resp = session.get(f"{SERVICE_URL}/{layer_id}/query", params=params,
                           timeout=timeout)
        resp.raise_for_status()
        assert_json_response(resp)
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"layer {layer_id}: {payload['error']}")

        if include_geometry:
            rows = payload.get("features") or []
            batch = [(f.get("properties") or {}, f.get("geometry")) for f in rows]
        else:
            rows = payload.get("features") or []
            batch = [(f.get("attributes") or {}, None) for f in rows]

        if not batch:
            return
        yield from batch
        offset += len(batch)
        if progress:
            progress(layer_id, offset)
        if len(batch) < page_size:
            return


def _landuse_row(attrs: dict, geometry, now: str) -> tuple:
    geo = json.dumps(geometry, ensure_ascii=False) if geometry else None
    min_lon, min_lat, max_lon, max_lat = _bbox(geometry)
    return (
        int(attrs["objectid"]),
        int(attrs["mp_id"]) if attrs.get("mp_id") else None,
        _text(attrs.get("pl_number")),
        _text(attrs.get("pl_name")),
        _text(attrs.get("num")),
        int(attrs["mavat_code"]) if attrs.get("mavat_code") is not None else None,
        _text(attrs.get("mavat_name")),
        _dunam(attrs.get("legal_area")),
        _num(attrs.get("shape_area")),
        _num(attrs.get("shape_length")),
        int(attrs["station"]) if attrs.get("station") is not None else None,
        _text(attrs.get("station_desc")),
        int(attrs["layer_id"]) if attrs.get("layer_id") is not None else None,
        _epoch_ms_to_iso(attrs.get("last_update_date")),
        geo, min_lon, min_lat, max_lon, max_lat, now,
    )


def _entity_row(kind: str, attrs: dict, geometry, now: str) -> tuple:
    geo = json.dumps(geometry, ensure_ascii=False) if geometry else None
    return (
        int(attrs["objectid"]), kind,
        int(attrs["mp_id"]) if attrs.get("mp_id") else None,
        _text(attrs.get("pl_number")),
        _text(attrs.get("pl_name")),
        int(attrs["mavat_code"]) if attrs.get("mavat_code") is not None else None,
        _text(attrs.get("mavat_name")),
        _num(attrs.get("shape_area")),
        _num(attrs.get("shape_length")),
        int(attrs["station"]) if attrs.get("station") is not None else None,
        _text(attrs.get("station_desc")),
        int(attrs["layer_id"]) if attrs.get("layer_id") is not None else None,
        _epoch_ms_to_iso(attrs.get("last_update_date")),
        geo, now,
    )


_LU_COLS = ("object_id", "mp_id", "pl_number", "pl_name", "cell_num",
            "mavat_code", "mavat_name", "legal_area_dunam", "shape_area_m2",
            "perimeter_m", "station", "station_desc", "layer_id",
            "last_update", "geometry_json",
            "min_lon", "min_lat", "max_lon", "max_lat", "fetched_at")

_ENT_COLS = ("object_id", "kind", "mp_id", "pl_number", "pl_name",
             "mavat_code", "mavat_name", "shape_area_m2", "shape_length_m",
             "station", "station_desc", "layer_id", "last_update",
             "geometry_json", "fetched_at")


def _upsert(conn, table, cols, rows, pk):
    if not rows:
        return 0
    marks = ",".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    conn.executemany(
        f"""INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})
            ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}""", rows)
    return len(rows)


def fetch_plan(conn, mp_id: int, *, include_geometry=True,
               timeout=DEFAULT_TIMEOUT, layers=None) -> dict:
    """
    Pull the full interior of ONE plan and store it. Idempotent: re-running
    upserts the same object ids rather than duplicating.
    """
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    wanted = layers or list(LAYERS)
    out: dict[str, int] = {}

    for layer_id in wanted:
        kind, _label = LAYERS[layer_id]
        lu_rows, ent_rows = [], []
        for attrs, geometry in iter_layer(
                layer_id, where=f"mp_id={int(mp_id)}",
                include_geometry=include_geometry, timeout=timeout):
            if not attrs.get("objectid"):
                continue
            if layer_id == LANDUSE_LAYER:
                lu_rows.append(_landuse_row(attrs, geometry, now))
            else:
                ent_rows.append(_entity_row(kind, attrs, geometry, now))
        out[kind] = (_upsert(conn, "plan_landuse", _LU_COLS, lu_rows, ("object_id",))
                     if layer_id == LANDUSE_LAYER else
                     _upsert(conn, "plan_entities", _ENT_COLS, ent_rows,
                             ("kind", "object_id")))
    conn.commit()
    return out


def import_landuse_bulk(conn, *, where="1=1", include_geometry=True,
                        progress=None, should_stop=None,
                        timeout=DEFAULT_TIMEOUT, page_size=MAX_PAGE_SIZE) -> int:
    """
    Bulk-pull the national land-use layer.

    Upsert, not staged-swap: this is a long pull and a cancel halfway should
    leave what was already fetched in place, keyed by objectid, so a re-run
    resumes usefully instead of starting from an empty table.
    """
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    total, batch = 0, []
    for attrs, geometry in iter_layer(
            LANDUSE_LAYER, where=where, include_geometry=include_geometry,
            page_size=page_size, timeout=timeout, should_stop=should_stop):
        if not attrs.get("objectid"):
            continue
        batch.append(_landuse_row(attrs, geometry, now))
        if len(batch) >= 500:
            total += _upsert(conn, "plan_landuse", _LU_COLS, batch, ("object_id",))
            conn.commit()
            batch.clear()
            if progress:
                progress("landuse", total)
    if batch:
        total += _upsert(conn, "plan_landuse", _LU_COLS, batch, ("object_id",))
        conn.commit()
    if progress:
        progress("landuse", total)
    return total


# ------------------------------------------------------------------ readers

def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def counts(conn) -> dict:
    out = {}
    for table in ("plan_landuse", "plan_entities"):
        out[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] if _has(conn, table) else 0
    out["plans_with_landuse"] = conn.execute(
        "SELECT COUNT(DISTINCT mp_id) FROM plan_landuse").fetchone()[0] \
        if _has(conn, "plan_landuse") else 0
    return out


def landuse_for_plan(conn, mp_id=None, pl_number=None,
                     geometry=False) -> list[dict]:
    """Land-use cells of one plan, largest first."""
    if not _has(conn, "plan_landuse"):
        return []
    cols = [c for c in _LU_COLS if geometry or c != "geometry_json"]
    if mp_id:
        where, params = "mp_id = ?", (int(mp_id),)
    elif pl_number:
        where, params = "pl_number = ?", (pl_number,)
    else:
        return []
    return [dict(r) for r in conn.execute(
        f"""SELECT {', '.join(cols)} FROM plan_landuse
            WHERE {where} ORDER BY shape_area_m2 DESC""", params)]


def landuse_mix(conn, mp_id=None, pl_number=None) -> dict:
    """
    The plan's designation mix: area per zoning type, and the residential share.

    Areas are summed from `shape_area_m2` (square metres) because
    `legal_area_dunam` is unstated (0) on most cells. The response says which
    field it used so nobody has to guess.
    """
    rows = landuse_for_plan(conn, mp_id=mp_id, pl_number=pl_number)
    if not rows:
        return {"cells": 0, "mix": [], "total_m2": 0.0}

    buckets: dict[tuple, dict] = {}
    for row in rows:
        key = (row["mavat_code"], row["mavat_name"])
        item = buckets.setdefault(key, {
            "mavat_code": row["mavat_code"],
            "mavat_name": row["mavat_name"],
            "cells": 0, "area_m2": 0.0, "legal_dunam": 0.0,
        })
        item["cells"] += 1
        item["area_m2"] += row["shape_area_m2"] or 0.0
        item["legal_dunam"] += row["legal_area_dunam"] or 0.0

    total = sum(b["area_m2"] for b in buckets.values())
    mix = sorted(buckets.values(), key=lambda b: -b["area_m2"])
    for item in mix:
        item["area_m2"] = round(item["area_m2"], 1)
        item["area_dunam"] = round(item["area_m2"] / 1000.0, 2)
        item["legal_dunam"] = round(item["legal_dunam"], 2) or None
        item["share_pct"] = round(item["area_m2"] / total * 100, 1) if total else None

    residential = sum(i["area_m2"] for i in mix
                      if i["mavat_name"] and "מגורים" in i["mavat_name"])
    return {
        "cells": len(rows),
        "designations": len(mix),
        "total_m2": round(total, 1),
        "total_dunam": round(total / 1000.0, 2),
        "residential_m2": round(residential, 1),
        "residential_share_pct": round(residential / total * 100, 1) if total else None,
        "mix": mix,
        "area_source": "shape_area_m2",
        "note": 'legal_area מגיע בדונם ולרוב 0 (לא צויין במסמך); שטחים כאן '
                'מחושבים מ-shape_area במ"ר.',
    }


def landuse_at_point(conn, lon: float, lat: float, limit: int = 20) -> list[dict]:
    """
    Which land-use cells cover a point. bbox prefilter, then exact geometry -
    the same two-stage pattern as db.plans_covering_point.
    """
    if not _has(conn, "plan_landuse"):
        return []
    from shapely.geometry import Point, shape

    candidates = conn.execute(
        """SELECT * FROM plan_landuse
           WHERE geometry_json IS NOT NULL
             AND min_lon <= ? AND max_lon >= ?
             AND min_lat <= ? AND max_lat >= ?""",
        (lon, lon, lat, lat)).fetchall()
    point = Point(lon, lat)
    hits = []
    for row in candidates:
        try:
            geom = shape(json.loads(row["geometry_json"]))
            if not geom.is_valid:
                geom = geom.buffer(0)
        except Exception:
            continue
        if geom.covers(point):
            item = {k: row[k] for k in row.keys() if k != "geometry_json"}
            hits.append(item)
    hits.sort(key=lambda r: r["shape_area_m2"] or 0.0)
    return hits[:limit]


def entities_for_plan(conn, mp_id=None, pl_number=None) -> dict:
    """Point/line/polygon entities of a plan, grouped by kind."""
    if not _has(conn, "plan_entities"):
        return {}
    if mp_id:
        where, params = "mp_id = ?", (int(mp_id),)
    elif pl_number:
        where, params = "pl_number = ?", (pl_number,)
    else:
        return {}
    out: dict[str, list] = {}
    for row in conn.execute(
        f"""SELECT kind, mavat_code, mavat_name, shape_area_m2, shape_length_m,
                   station_desc, COUNT(*) n
            FROM plan_entities WHERE {where}
            GROUP BY kind, mavat_code, mavat_name
            ORDER BY kind, n DESC""", params):
        out.setdefault(row["kind"], []).append(dict(row))
    return out


def designation_catalog(conn, limit=60) -> list[dict]:
    """
    Every מבא"ת designation seen, with how much land it covers.
    Useful as the source of truth for a zoning filter dropdown.
    """
    if not _has(conn, "plan_landuse"):
        return []
    return [dict(r) for r in conn.execute(
        """SELECT mavat_code, mavat_name, COUNT(*) cells,
                  COUNT(DISTINCT mp_id) plans,
                  ROUND(SUM(shape_area_m2)/1000.0, 1) total_dunam
           FROM plan_landuse WHERE mavat_name IS NOT NULL
           GROUP BY mavat_code, mavat_name
           ORDER BY total_dunam DESC LIMIT ?""", (limit,))]


if __name__ == "__main__":
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    conn = db.get_conn()
    row = conn.execute(
        """SELECT mp_id, pl_number, pl_name FROM plans
           WHERE housing_units > 500 AND pl_number <> ''
           ORDER BY housing_units DESC LIMIT 1""").fetchone()
    print(f"plan: {row['pl_number']} | {row['pl_name']}")
    got = fetch_plan(conn, row["mp_id"])
    print("stored:", got)
    mix = landuse_mix(conn, mp_id=row["mp_id"])
    print(f"\ncells={mix['cells']} designations={mix['designations']} "
          f"total={mix['total_dunam']:,} dunam "
          f"residential={mix['residential_share_pct']}%")
    for item in mix["mix"][:8]:
        print(f"  {item['mavat_name'][:34]:34} {item['area_dunam']:>9,.2f} dunam"
              f"  {item['share_pct']:>5}%  ({item['cells']} cells)")
    print("\ndocument (human link):", mavat_plan_url(row["pl_number"]))
    print("counts:", counts(conn))
    conn.close()
