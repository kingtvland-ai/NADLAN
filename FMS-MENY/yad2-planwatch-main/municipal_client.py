"""
PlanWatch - municipal GIS (שכבות עירוניות)
===========================================
Fills the two biggest holes in the national data: **real parcel polygons** and
**per-building facts** (permits, floors, year built, dangerous-building orders).

Sources: Tel Aviv-Yafo's public ArcGIS server (`gisn.tel-aviv.gov.il`,
`IView2/MapServer`, 250 layers) and Ashdod's (`gis.ashdod.muni.il`,
`גושים_וחלקות`). Both open, no key. Verified live 2026-07-28/29.

Layers imported
---------------
| id  | layer                | rows   | why it matters                        |
|-----|----------------------|--------|---------------------------------------|
| 524 | חלקות                | 45,206 | **parcel POLYGONS** with gush/chelka  |
| 772 | בקשות והיתרי בניה    | 10,554 | permits + **תמ"א 38 flags** + units   |
| 591 | מבנים מסוכנים        |    618 | dangerous-building orders             |
| 513 | מבנים                | 45,837 | footprint, floors, height, year       |

Why this is worth a city-specific module
----------------------------------------
The national services give plan polygons but NOT parcel polygons: GovMap returns
only a centroid, `api.govmap.gov.il` needs a key (403 without one), and the
national cadastral shapefiles on `e.data.gov.il` sit behind an obfuscated JS
bot-challenge. A municipal ArcGIS server hands over the same geometry openly.

`מבנים מסוכנים` is the single most direct urban-renewal signal in the whole
project: a standing order under סעיף 3 means the building is a hazard *now*, so
redevelopment is not speculative.

Coverage limit, stated plainly
------------------------------
**Two cities, not the country.**

| city | parcels | permits | dangerous | buildings |
|---|---|---|---|---|
| תל אביב-יפו | 45,206 | 10,554 | 618 | 45,837 |
| אשדוד | 8,924 | - | - | - |

Twenty-one municipal hosts have been probed across two sweeps; these two answer
publicly. Ashdod publishes parcels only. `SERVERS` is the extension point: add a
city with its layer ids and a `fields` mapping, and every importer works
unchanged - each server names its columns differently (`ms_gush` vs `Gush`),
which is exactly what `fields` exists to absorb.

**Do NOT present these counts as national.** `counts()` returns the city list
and a coverage note for this reason.

Field traps, verified against live responses
--------------------------------------------
* `tr_chelka` is epoch **milliseconds and can be NEGATIVE** (-2209161600000 =
  1900-01-01). Treated as a date only inside a sane window.
* `ms_shetach_rashum` (registered area) and `ms_shetach` (computed) disagree by
  design - both kept, neither preferred silently.
* `sw_tama_38` / `_chadash` / `_tosefet` are the STRINGS "כן"/"לא", not booleans.
* `permission_num = 0` means "no permit issued", not permit number zero.
* `ms_komot` is 0 or NULL on many footprints; 0 is "unknown", not a ground-level
  slab.
* Geometry comes back in Web Mercator unless `outSR=4326` is forced.
"""

from __future__ import annotations

import json

from http_client import DEFAULT_TIMEOUT, build_session

#: city key -> ArcGIS MapServer. Extension point for more municipalities.
#:
#: `fields` maps this project's column names onto whatever the city calls them.
#: Every server names them differently - Tel Aviv uses `ms_gush`/`ms_chelka`,
#: Ashdod uses `Gush`/`Helka` - so a per-city mapping is the only thing that
#: keeps one importer working across servers.
SERVERS = {
    "tel-aviv": {
        "name": "תל אביב-יפו",
        "url": ("https://gisn.tel-aviv.gov.il/arcgis/rest/services/"
                "IView2/MapServer"),
        "layers": {
            "parcels": 524,
            "permits": 772,
            "dangerous": 591,
            "buildings": 513,
        },
        "fields": {
            "parcels": {"oid": "oid_chelka", "gush": "ms_gush",
                        "helka": "ms_chelka",
                        "area_registered": "ms_shetach_rashum",
                        "area_computed": "ms_shetach",
                        "settled": "k_status_hesder", "ownership": "sw_baalut",
                        "updated": "tr_chelka"},
        },
    },
    #: Found in the second sweep of municipal servers (2026-07-29). Ten other
    #: hosts were probed and did not answer publicly; this one does.
    #: Parcels only - Ashdod does not publish permits, dangerous buildings or
    #: building footprints with year-built.
    "ashdod": {
        "name": "אשדוד",
        "url": ("https://gis.ashdod.muni.il/arcgis/rest/services/"
                "%D7%92%D7%95%D7%A9%D7%99%D7%9D_%D7%95%D7%97%D7%9C%D7%A7%D7%95"
                "%D7%AA/MapServer"),
        "layers": {"parcels": 1},
        "fields": {
            "parcels": {"oid": "OBJECTID", "gush": "Gush", "helka": "Helka",
                        # Helka_Area is in m^2 and is the only area published.
                        "area_registered": None, "area_computed": "Helka_Area",
                        "settled": None, "ownership": None, "updated": None},
        },
    },
}

PAGE = 1000

TABLES = """
CREATE TABLE IF NOT EXISTS muni_parcels (
    city         TEXT NOT NULL,
    object_id    INTEGER NOT NULL,
    gush         TEXT,
    helka        TEXT,
    area_registered_m2 REAL,   -- ms_shetach_rashum, from the register
    area_computed_m2   REAL,   -- ms_shetach, from the geometry
    settled_code INTEGER,      -- k_status_hesder
    ownership_flag INTEGER,    -- sw_baalut
    updated_at   TEXT,
    geometry_json TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
    fetched_at   TEXT,
    PRIMARY KEY (city, object_id)
);

CREATE TABLE IF NOT EXISTS muni_permits (
    city          TEXT NOT NULL,
    object_id     INTEGER NOT NULL,
    request_num   TEXT,
    permit_num    TEXT,        -- NULL when the source published 0
    requested_at  TEXT,
    permitted_at  TEXT,
    expires_at    TEXT,
    building_num  TEXT,
    housing_units INTEGER,
    is_tama38     INTEGER,     -- from "כן"/"לא"
    tama38_new    INTEGER,
    tama38_added  INTEGER,
    request_kind  TEXT,
    request_text  TEXT,
    stage         TEXT,
    addresses     TEXT,          -- full street addresses; MULTIPLE when the
                                 -- permit covers an assembled site
    title         TEXT,          -- koteret, e.g. "בקשת רישוי 11/19"
    track         TEXT,          -- maslul_rishuy: which licensing route
    seq_no        INTEGER,       -- `progress`: a row sequence number 1..N,
                                 -- NOT a percentage. Values run 1-10,874 on a
                                 -- 10,554-row layer; naming it progress_pct
                                 -- produced "6640%" in the UI.
    file_num      TEXT,          -- ms_tik_binyan, the building file
    archive_url   TEXT,          -- municipal archive for this request
    started_at    TEXT,
    finished      TEXT,
    geometry_json TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
    fetched_at    TEXT,
    PRIMARY KEY (city, object_id)
);

CREATE TABLE IF NOT EXISTS muni_dangerous (
    city        TEXT NOT NULL,
    object_id   INTEGER NOT NULL,
    street      TEXT,
    house_num   TEXT,
    entrance    TEXT,
    order_kind  TEXT,          -- t_tzav, e.g. "הודעה לפי סעיף 3"
    order_code  INTEGER,
    temporary   INTEGER,
    findings    TEXT,
    address     TEXT,
    lat REAL, lon REAL,
    fetched_at  TEXT,
    PRIMARY KEY (city, object_id)
);

CREATE TABLE IF NOT EXISTS muni_buildings (
    city        TEXT NOT NULL,
    object_id   INTEGER NOT NULL,
    building_id TEXT,
    kind        TEXT,
    floors      INTEGER,       -- NULL when the source said 0 (= unknown)
    name        TEXT,
    on_pillars  TEXT,
    height_m    REAL,
    year_built  INTEGER,
    asbestos    TEXT,
    geometry_json TEXT,
    min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
    fetched_at  TEXT,
    PRIMARY KEY (city, object_id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_mp_gh    ON muni_parcels (gush, helka);
CREATE INDEX IF NOT EXISTS idx_mp_bbox  ON muni_parcels (min_lon, max_lon, min_lat, max_lat);
CREATE INDEX IF NOT EXISTS idx_mperm_t38 ON muni_permits (is_tama38);
CREATE INDEX IF NOT EXISTS idx_mperm_bbox ON muni_permits (min_lon, max_lon, min_lat, max_lat);
CREATE INDEX IF NOT EXISTS idx_mperm_addr ON muni_permits (addresses);
CREATE INDEX IF NOT EXISTS idx_mdang_ll  ON muni_dangerous (lat, lon);
CREATE INDEX IF NOT EXISTS idx_mbld_bbox ON muni_buildings (min_lon, max_lon, min_lat, max_lat);
CREATE INDEX IF NOT EXISTS idx_mbld_year ON muni_buildings (year_built);
"""

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    # `CREATE TABLE IF NOT EXISTS` will not alter an existing table, so new
    # columns need an explicit ALTER - the same trap that has bitten min_lon,
    # pl_number_sq and doc_helka_range in this project.
    existing = {r["name"] for r in conn.execute("PRAGMA table_xinfo(muni_permits)")}
    for column, decl in _PERMIT_ADDED.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE muni_permits ADD COLUMN {column} {decl}")
    conn.executescript(INDEXES)
    conn.commit()


# ------------------------------------------------------------------ parsing

def _text(value):
    if value is None:
        return None
    out = " ".join(str(value).replace("\xa0", " ").split())
    return out or None


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    out = _num(value)
    return int(out) if out is not None else None


def _zero_is_unknown(value):
    """`permission_num`/`ms_komot` publish 0 for "none/unknown", not zero."""
    out = _int(value)
    return out if out else None


def _yesno(value):
    """`sw_tama_38` is the string "כן"/"לא", not a boolean."""
    text = _text(value)
    if text is None:
        return None
    return 1 if text == "כן" else 0


def _epoch_ms(value):
    """
    ArcGIS epoch-ms -> ISO date.

    These fields can be NEGATIVE: `tr_chelka` = -2209161600000 is 1900-01-01,
    a placeholder. Anything outside 1950..2050 is rejected rather than stored as
    a real date.
    """
    if value in (None, ""):
        return None
    try:
        from datetime import datetime, timezone
        out = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return out.date().isoformat() if 1950 <= out.year <= 2050 else None


def _bbox(geometry):
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


_OID_CACHE: dict = {}


def oid_field(city: str, layer: str, timeout=DEFAULT_TIMEOUT) -> str:
    """
    Discover the layer's OID field name from the service metadata.

    Every layer here names it differently - `oid_chelka`, `oid_permit`,
    `oid_mivne` - and `objectIdField` is null in the service JSON. Sending the
    ArcGIS default `OBJECTID` as `orderByFields` fails the whole query with
    HTTP 400 "Invalid or missing input parameters", so this has to be read from
    the field list rather than assumed.

    Order-by is not optional: without a stable sort, `resultOffset` paging can
    repeat or skip rows and the import silently truncates.
    """
    key = (city, layer)
    if key in _OID_CACHE:
        return _OID_CACHE[key]
    server = SERVERS[city]
    meta = _sess().get(f"{server['url']}/{server['layers'][layer]}",
                       params={"f": "json"}, timeout=timeout).json()
    names = [f["name"] for f in meta.get("fields", [])
             if f.get("type") == "esriFieldTypeOID"]
    if not names:
        # Fall back to the configured field name before giving up: some servers
        # omit the OID type in metadata even though the column exists.
        configured = ((server.get("fields") or {}).get(layer) or {}).get("oid")
        if configured:
            _OID_CACHE[key] = configured
            return configured
        raise RuntimeError(f"{city}/{layer}: no OID field in service metadata")
    _OID_CACHE[key] = names[0]
    return names[0]


def iter_layer(city: str, layer: str, *, where="1=1", include_geometry=True,
               timeout=DEFAULT_TIMEOUT, should_stop=None, progress=None):
    """
    Page one municipal layer.

    `outSR=4326` is forced - the server otherwise answers in Web Mercator and
    every coordinate would land in the Atlantic once treated as lat/lon.
    """
    server = SERVERS[city]
    layer_id = server["layers"][layer]
    order_by = oid_field(city, layer, timeout=timeout)
    offset = 0
    while True:
        if should_stop and should_stop():
            return
        params = {
            "where": where, "outFields": "*",
            "returnGeometry": "true" if include_geometry else "false",
            "outSR": 4326,
            "f": "geojson" if include_geometry else "json",
            "orderByFields": f"{order_by} ASC",
            "resultOffset": offset, "resultRecordCount": PAGE,
        }
        resp = _sess().get(f"{server['url']}/{layer_id}/query", params=params,
                           timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"{city}/{layer}: {payload['error']}")
        feats = payload.get("features") or []
        if not feats:
            return
        for feat in feats:
            attrs = feat.get("properties") or feat.get("attributes") or {}
            yield attrs, feat.get("geometry")
        offset += len(feats)
        if progress:
            progress(f"muni:{layer}", offset)
        if len(feats) < PAGE:
            return


def _upsert(conn, table, cols, rows):
    if not rows:
        return 0
    marks = ",".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols
                        if c not in ("city", "object_id"))
    conn.executemany(
        f"""INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})
            ON CONFLICT (city, object_id) DO UPDATE SET {updates}""", rows)
    conn.commit()
    return len(rows)


# ------------------------------------------------------------------- imports

_PARCEL_COLS = ("city", "object_id", "gush", "helka", "area_registered_m2",
                "area_computed_m2", "settled_code", "ownership_flag",
                "updated_at", "geometry_json",
                "min_lon", "min_lat", "max_lon", "max_lat", "fetched_at")


def import_parcels(conn, city="tel-aviv", progress=None, should_stop=None,
                   timeout=DEFAULT_TIMEOUT) -> int:
    """Parcel POLYGONS - the geometry GovMap does not give."""
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    # Field names differ per city - see SERVERS[...]["fields"].
    fmap = (SERVERS[city].get("fields") or {}).get("parcels") or {}

    def field(key):
        name = fmap.get(key)
        return attrs.get(name) if name else None

    total, batch = 0, []
    for attrs, geom in iter_layer(city, "parcels", timeout=timeout,
                                  should_stop=should_stop, progress=progress):
        oid = _int(field("oid"))
        if oid is None:
            continue
        gush, helka = _int(field("gush")), _int(field("helka"))
        lo_x, lo_y, hi_x, hi_y = _bbox(geom)
        batch.append((
            city, oid,
            str(gush) if gush else None, str(helka) if helka else None,
            _num(field("area_registered")), _num(field("area_computed")),
            _int(field("settled")), _int(field("ownership")),
            _epoch_ms(field("updated")),
            json.dumps(geom, ensure_ascii=False) if geom else None,
            lo_x, lo_y, hi_x, hi_y, now,
        ))
        if len(batch) >= 500:
            total += _upsert(conn, "muni_parcels", _PARCEL_COLS, batch)
            batch.clear()
    total += _upsert(conn, "muni_parcels", _PARCEL_COLS, batch)
    return total


_PERMIT_COLS = ("city", "object_id", "request_num", "permit_num",
                "requested_at", "permitted_at", "expires_at", "building_num",
                "housing_units", "is_tama38", "tama38_new", "tama38_added",
                "request_kind", "request_text", "stage", "addresses", "title",
                "track", "seq_no", "file_num", "archive_url",
                "started_at", "finished", "geometry_json",
                "min_lon", "min_lat", "max_lon", "max_lat", "fetched_at")

#: Columns added after muni_permits first shipped.
_PERMIT_ADDED = {
    "addresses": "TEXT", "title": "TEXT", "track": "TEXT",
    "seq_no": "INTEGER", "file_num": "TEXT", "archive_url": "TEXT",
}


def import_permits(conn, city="tel-aviv", progress=None, should_stop=None,
                   timeout=DEFAULT_TIMEOUT) -> int:
    """Building permits with תמ"א 38 flags - real per-building activity."""
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    total, batch = 0, []
    for attrs, geom in iter_layer(city, "permits", timeout=timeout,
                                  should_stop=should_stop, progress=progress):
        oid = _int(attrs.get("oid_permit"))
        if oid is None:
            continue
        lo_x, lo_y, hi_x, hi_y = _bbox(geom)
        batch.append((
            city, oid, _text(attrs.get("request_num")),
            # 0 means "no permit issued yet", not permit number zero.
            str(_zero_is_unknown(attrs.get("permission_num")) or "") or None,
            _epoch_ms(attrs.get("open_request")),
            _epoch_ms(attrs.get("permission_date")),
            _epoch_ms(attrs.get("expiry_date")),
            _text(attrs.get("building_num")), _int(attrs.get("yechidot_diyur")),
            _yesno(attrs.get("sw_tama_38")), _yesno(attrs.get("sw_tama_38_chadash")),
            _yesno(attrs.get("sw_tama_38_tosefet")),
            _text(attrs.get("sug_bakasha")),
            (_text(attrs.get("tochen_bakasha")) or "")[:1200] or None,
            _text(attrs.get("building_stage")),
            # `addresses` is the useful one: it carries the real street
            # address(es), and a permit covering an assembled site lists them
            # all - e.g. 7 addresses on one פינוי בינוי request.
            _text(attrs.get("addresses")), _text(attrs.get("koteret")),
            _text(attrs.get("maslul_rishuy")), _int(attrs.get("progress")),
            _text(attrs.get("ms_tik_binyan")), _text(attrs.get("url_hadmaya")),
            _epoch_ms(attrs.get("tr_hathalat_bniya")),
            _text(attrs.get("finished")),
            json.dumps(geom, ensure_ascii=False) if geom else None,
            lo_x, lo_y, hi_x, hi_y, now,
        ))
        if len(batch) >= 500:
            total += _upsert(conn, "muni_permits", _PERMIT_COLS, batch)
            batch.clear()
    total += _upsert(conn, "muni_permits", _PERMIT_COLS, batch)
    return total


_DANGER_COLS = ("city", "object_id", "street", "house_num", "entrance",
                "order_kind", "order_code", "temporary", "findings", "address",
                "lat", "lon", "fetched_at")


def import_dangerous(conn, city="tel-aviv", progress=None, should_stop=None,
                     timeout=DEFAULT_TIMEOUT) -> int:
    """
    Dangerous-building orders.

    The strongest urban-renewal trigger available anywhere in this project: a
    standing order means the building is a hazard now, so redevelopment is a
    live necessity rather than a speculation.
    """
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    rows = []
    for attrs, geom in iter_layer(city, "dangerous", timeout=timeout,
                                  should_stop=should_stop, progress=progress):
        oid = _int(attrs.get("oid_mivne"))
        if oid is None:
            continue
        coords = (geom or {}).get("coordinates") or []
        lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (None, None)
        rows.append((
            city, oid, _text(attrs.get("shem_rechov")),
            _text(attrs.get("ms_bayit")), _text(attrs.get("knisa")),
            _text(attrs.get("t_tzav")), _int(attrs.get("sug_tzav")),
            _int(attrs.get("sw_zmanit")),
            (_text(attrs.get("t_mimzaim")) or "")[:1200] or None,
            _text(attrs.get("t_ktovet")), lat, lon, now,
        ))
    return _upsert(conn, "muni_dangerous", _DANGER_COLS, rows)


_BLD_COLS = ("city", "object_id", "building_id", "kind", "floors", "name",
             "on_pillars", "height_m", "year_built", "asbestos",
             "geometry_json", "min_lon", "min_lat", "max_lon", "max_lat",
             "fetched_at")


def import_buildings(conn, city="tel-aviv", progress=None, should_stop=None,
                     timeout=DEFAULT_TIMEOUT) -> int:
    """Building footprints: floors, height, year built."""
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    total, batch = 0, []
    for attrs, geom in iter_layer(city, "buildings", timeout=timeout,
                                  should_stop=should_stop, progress=progress):
        oid = _int(attrs.get("oid_mivne"))
        if oid is None:
            continue
        lo_x, lo_y, hi_x, hi_y = _bbox(geom)
        year = _int(attrs.get("year"))
        batch.append((
            city, oid, _text(attrs.get("id_binyan")),
            _text(attrs.get("t_sug_mivne")),
            # 0 floors is "unknown", not a ground slab.
            _zero_is_unknown(attrs.get("ms_komot")),
            _text(attrs.get("shem_mivne")), _text(attrs.get("t_amudim")),
            _num(attrs.get("gova_simplex_2019")) or _num(attrs.get("gova_mpi_2009")),
            year if year and 1800 <= year <= 2050 else None,
            _text(attrs.get("t_asbest_level")),
            json.dumps(geom, ensure_ascii=False) if geom else None,
            lo_x, lo_y, hi_x, hi_y, now,
        ))
        if len(batch) >= 500:
            total += _upsert(conn, "muni_buildings", _BLD_COLS, batch)
            batch.clear()
    total += _upsert(conn, "muni_buildings", _BLD_COLS, batch)
    return total


IMPORTERS = {
    "muni_parcels": import_parcels,
    "muni_permits": import_permits,
    "muni_dangerous": import_dangerous,
    "muni_buildings": import_buildings,
}


#: importer key -> the layer it needs. A city that does not publish that layer
#: is skipped rather than erroring: Ashdod has parcels only.
_NEEDS_LAYER = {"muni_parcels": "parcels", "muni_permits": "permits",
                "muni_dangerous": "dangerous", "muni_buildings": "buildings"}


def import_all(conn, city="tel-aviv", progress=None, should_stop=None,
               timeout=DEFAULT_TIMEOUT) -> int:
    """Import every layer this city actually publishes."""
    available = SERVERS[city]["layers"]
    total = 0
    for key, fn in IMPORTERS.items():
        if should_stop and should_stop():
            break
        if _NEEDS_LAYER.get(key) not in available:
            continue
        total += fn(conn, city=city, progress=progress,
                    should_stop=should_stop, timeout=timeout)
    return total


def import_every_city(conn, progress=None, should_stop=None,
                      timeout=DEFAULT_TIMEOUT) -> dict:
    """Import all configured cities. One city failing must not stop the rest."""
    out = {}
    for city in SERVERS:
        if should_stop and should_stop():
            break
        try:
            out[city] = import_all(conn, city=city, progress=progress,
                                   should_stop=should_stop, timeout=timeout)
        except Exception as exc:
            out[city] = f"error: {type(exc).__name__}: {exc}"[:160]
    return out


# ------------------------------------------------------------------- readers

def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def counts(conn) -> dict:
    out = {}
    for table in ("muni_parcels", "muni_permits", "muni_dangerous",
                  "muni_buildings"):
        out[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] if _has(conn, table) else 0
    out["cities"] = [s["name"] for s in SERVERS.values()]
    # Derived from SERVERS, never hardcoded: this note said "Tel Aviv only"
    # after Ashdod was added, which made the response actively wrong.
    out["by_city"] = {
        s["name"]: {
            "layers": sorted(s["layers"]),
            "parcels": conn.execute(
                "SELECT COUNT(*) FROM muni_parcels WHERE city = ?", (key,)
            ).fetchone()[0] if _has(conn, "muni_parcels") else 0,
        }
        for key, s in SERVERS.items()
    }
    names = " · ".join(s["name"] for s in SERVERS.values())
    out["coverage_note"] = (
        f"כיסוי עירוני: {names} בלבד — לא ארצי. "
        f"{len(SERVERS)} שרתים עונים מתוך 21 שנבדקו. "
        f"לא כל עיר מפרסמת את כל השכבות (ראה by_city).")
    return out


def parcel_polygon(conn, gush, helka) -> dict | None:
    """The parcel's real polygon - what GovMap cannot give."""
    if not _has(conn, "muni_parcels"):
        return None
    row = conn.execute(
        """SELECT * FROM muni_parcels WHERE gush=? AND helka=?
           ORDER BY area_computed_m2 DESC LIMIT 1""",
        (str(gush).strip(), str(helka).strip())).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["geometry"] = json.loads(out.pop("geometry_json") or "null")
    return out


def parcel_at_point(conn, lat, lon) -> dict | None:
    """Which parcel a coordinate falls in - the inverse of `parcel_polygon`.

    This is what turns an address into a gush/helka. GovMap's public search
    resolves "דיזנגוף 100" to a point and returns **no parcel** on its ADDRESS
    layer (the parcel layer needs the key we do not have), so the point has to
    be tested against polygons we hold ourselves.

    Two-stage, like every other point test here: the indexed bbox narrows
    45,206 polygons to a handful, then shapely decides. `covers` rather than
    `contains` so a point exactly on a boundary resolves instead of falling
    through - the same choice `sync._point_hits_plan` makes.

    Returns None outside the cities whose polygons are imported (Tel Aviv and
    Ashdod today), which is an honest "not covered here" and not an error.
    """
    if not _has(conn, "muni_parcels"):
        return None
    rows = conn.execute(
        """SELECT city, object_id, gush, helka, area_registered_m2,
                  area_computed_m2, geometry_json
             FROM muni_parcels
            WHERE min_lon <= ? AND max_lon >= ? AND min_lat <= ? AND max_lat >= ?
              AND gush IS NOT NULL AND helka IS NOT NULL""",
        (lon, lon, lat, lat)).fetchall()
    if not rows:
        return None
    try:
        from shapely.geometry import Point, shape
    except ImportError:
        return None
    point = Point(float(lon), float(lat))
    for row in rows:
        try:
            geometry = shape(json.loads(row["geometry_json"] or "null"))
        except (ValueError, TypeError, AttributeError):
            continue
        if not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty or not geometry.covers(point):
            continue
        out = dict(row)
        out.pop("geometry_json", None)
        return out
    return None


def permits_near(conn, lat, lon, radius_m=150, limit=40) -> list[dict]:
    """Building permits near a point, newest first."""
    if not _has(conn, "muni_permits"):
        return []
    pad = radius_m / 111_000.0
    rows = conn.execute(
        """SELECT city, object_id, request_num, permit_num, permitted_at,
                  requested_at, housing_units, is_tama38, tama38_new,
                  tama38_added, request_kind, stage, building_num,
                  addresses, title, track, seq_no, archive_url
           FROM muni_permits
           WHERE min_lon <= ? AND max_lon >= ? AND min_lat <= ? AND max_lat >= ?
           ORDER BY COALESCE(permitted_at, requested_at) DESC LIMIT ?""",
        (lon + pad, lon - pad, lat + pad, lat - pad, limit)).fetchall()
    return [dict(r) for r in rows]


def dangerous_near(conn, lat, lon, radius_m=300, limit=20) -> list[dict]:
    """Dangerous-building orders near a point, with true metric distance."""
    if not _has(conn, "muni_dangerous"):
        return []
    import math
    pad_lat = radius_m / 110_574.0
    pad_lon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    rows = conn.execute(
        """SELECT * FROM muni_dangerous
           WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
        (lat - pad_lat, lat + pad_lat, lon - pad_lon, lon + pad_lon)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        dy = (item["lat"] - lat) * 110_574.0
        dx = (item["lon"] - lon) * 111_320.0 * math.cos(math.radians(lat))
        item["distance_m"] = round(math.hypot(dx, dy), 1)
        if item["distance_m"] <= radius_m:
            out.append(item)
    out.sort(key=lambda r: r["distance_m"])
    return out[:limit]


#: Dangerous-building order severity, 0-1. An immediate demolition or
#: evacuation order is a far stronger renewal trigger than a request for a
#: private engineer's inspection, and the source's own wording grades them.
#: Verified order types and counts (Tel Aviv, 2026-07): private-engineer check
#: 452, s.3 notice 74, immediate works 19, demolition notice 17, retaining
#: walls 16, sealing 13, municipal asset 12, immediate demolition 7, s.9 4,
#: immediate evacuation 2, immediate closure 2.
_DANGER_SEVERITY = (
    ("הריסה מיידית", 1.00),
    ("פינוי מיידי", 1.00),
    ("סגירה מיידית", 0.95),
    ("ביצוע עבודות מיידי", 0.85),
    ("הריסה", 0.90),
    ("אטימה", 0.80),
    ("קירות תומכים", 0.55),
    ("סעיף 9", 0.50),
    ("סעיף 4", 0.80),
    ("בדיקת מהנדס פרטי", 0.35),
    ("סעיף 3", 0.45),
)


def danger_severity(order_kind) -> float:
    """
    0-1 severity for a dangerous-building order.

    Ordered longest-signal-first: "הריסה מיידית" must win over the bare
    "הריסה", and "צו 3 - בדיקת מהנדס פרטי" must not be scored as a plain
    "סעיף 3" notice.
    """
    text = _text(order_kind) or ""
    for needle, score in _DANGER_SEVERITY:
        if needle in text:
            return score
    return 0.3 if text else 0.0


def renewal_pressure(conn, lat, lon, radius_m=300) -> dict:
    """
    How hard the built environment is pushing for renewal at a point.

    Combines the two municipal signals the national feeds cannot provide:
    standing dangerous-building orders (weighted by severity) and תמ"א 38
    permit activity nearby. Returns the raw evidence alongside the score so it
    stays auditable - same rule as opportunity.py.
    """
    orders = dangerous_near(conn, lat, lon, radius_m=radius_m, limit=50)
    permits = permits_near(conn, lat, lon, radius_m=radius_m, limit=50)
    t38 = [p for p in permits if p.get("is_tama38")]

    worst = max((danger_severity(o["order_kind"]) for o in orders), default=0.0)
    # Score is driven by the WORST order, not the count: one immediate
    # demolition order matters more than ten engineer-inspection requests.
    danger_score = round(worst * 100, 1) if orders else None
    t38_score = round(min(100.0, len(t38) * 20.0), 1) if permits else None

    return {
        "radius_m": radius_m,
        "dangerous_orders": len(orders),
        "worst_severity": worst if orders else None,
        "worst_order": max(orders, key=lambda o: danger_severity(o["order_kind"])
                           )["order_kind"] if orders else None,
        "tama38_permits_near": len(t38),
        "permits_near": len(permits),
        "danger_score": danger_score,
        "tama38_score": t38_score,
        "orders": orders[:8],
        "note": "צו הריסה או פינוי מיידי הוא האות החזק ביותר להתחדשות — "
                "המבנה מסוכן עכשיו, לא בתיאוריה. כיסוי: תל אביב-יפו.",
    }


def tama38_permits(conn, limit=100) -> dict:
    """
    Permits flagged תמ"א 38 - real, per-building renewal activity.

    Complements `opportunity.tama38_hotspots()`, which counts PLANS. A permit
    is one step further: the plan exists and someone applied to build on it.
    """
    if not _has(conn, "muni_permits"):
        return {"total": 0, "rows": []}
    total = conn.execute(
        "SELECT COUNT(*) FROM muni_permits WHERE is_tama38=1").fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        """SELECT city, object_id, request_num, permit_num, permitted_at,
                  requested_at, housing_units, tama38_new, tama38_added,
                  request_kind, stage, min_lat, min_lon
           FROM muni_permits WHERE is_tama38=1
           ORDER BY COALESCE(permitted_at, requested_at) DESC LIMIT ?""",
        (limit,))]
    breakdown = [dict(r) for r in conn.execute(
        """SELECT COALESCE(stage,'לא צויין') stage, COUNT(*) n,
                  COALESCE(SUM(housing_units),0) units
           FROM muni_permits WHERE is_tama38=1 GROUP BY 1 ORDER BY n DESC""")]
    return {"total": total, "rows": rows, "by_stage": breakdown}


def building_ages(conn, limit=40) -> dict:
    """
    Building stock by decade - the ACTUAL age data the national feeds lack.

    Note the coverage caveat: `year` is populated on only part of the layer, so
    the response reports how many footprints carry a year at all.
    """
    if not _has(conn, "muni_buildings"):
        return {"with_year": 0}
    total = conn.execute("SELECT COUNT(*) FROM muni_buildings").fetchone()[0]
    with_year = conn.execute(
        "SELECT COUNT(*) FROM muni_buildings WHERE year_built IS NOT NULL"
    ).fetchone()[0]
    decades = [dict(r) for r in conn.execute(
        """SELECT (year_built/10)*10 decade, COUNT(*) buildings,
                  ROUND(AVG(floors),1) avg_floors
           FROM muni_buildings WHERE year_built IS NOT NULL
           GROUP BY 1 ORDER BY 1""")]
    pre1980 = conn.execute(
        "SELECT COUNT(*) FROM muni_buildings WHERE year_built < 1980"
    ).fetchone()[0]
    return {"buildings": total, "with_year": with_year,
            "coverage_pct": round(with_year / total * 100, 1) if total else None,
            "pre_1980": pre1980, "decades": decades,
            "note": 'מבנים שנבנו לפני 1980 הם אלה שתמ"א 38 חלה עליהם '
                    '(לפני תקן 413).'}


if __name__ == "__main__":
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # unit tests for the traps
    assert _yesno("כן") == 1 and _yesno("לא") == 0 and _yesno(None) is None
    assert _zero_is_unknown(0) is None
    assert _zero_is_unknown(5) == 5
    # negative epoch = 1900 placeholder, must be rejected
    assert _epoch_ms(-2209161600000) is None, _epoch_ms(-2209161600000)
    assert _epoch_ms(1546332540000) == "2019-01-01", _epoch_ms(1546332540000)
    assert _epoch_ms(None) is None
    print("parser self-tests passed\n")

    conn = db.get_conn()
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which == "all":
        total = import_all(conn, progress=lambda k, n: print(f"  {k}: {n:,}", end="\r"))
    else:
        total = IMPORTERS[which](conn, progress=lambda k, n: print(f"  {k}: {n:,}", end="\r"))
    print(f"\nimported {total:,} rows")
    print("counts:", json.dumps(counts(conn), ensure_ascii=False))
    print("\nages:", json.dumps(building_ages(conn), ensure_ascii=False)[:340])
    t38 = tama38_permits(conn, limit=3)
    print(f"\nתמ\"א 38 permits: {t38['total']}")
    for r in t38.get("by_stage", [])[:5]:
        print(f"   {str(r['stage'])[:34]:34} {r['n']:>4} permits {r['units']:>5} units")
    conn.close()
