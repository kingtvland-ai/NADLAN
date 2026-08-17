"""What is within walking distance, from the two registers that publish it.

Why a listing needs this
-----------------------
`listing_analytics` compares a flat to its neighbours and `listing_planning`
compares its ground to the planning register. Between them they still cannot
answer the first question anyone asks about an address: *what is near it*. Two
flats 600 metres apart, same street name, same building age, same asking price
- one is four minutes from a light-rail platform and the other is a twenty-five
minute walk from any service at all. The boards do not say so, the neighbourhood
median absorbs the difference into noise, and the buy score was blind to it.

The sources
-----------
Both are open registers on data.gov.il, both national, both already
coordinate-bearing - no geocoding step and therefore no geocoding error:

* **תחנות תחבורה ציבורית** (`e873e6a2`), 34,166 stops with `Lat`/`Long`. The
  name says buses and the contents are the whole network: Israel Railways
  (69 + 7 platform records), light rail (150 + 34), metronit/BRT (195), cable
  car (12), and 32,458 ordinary stops. One request covers every mode, which is
  why the separate `rail_stat` dataset is not used - its CSV carries station
  names and no coordinates at all.
* **קואורדינטות מוסדות החינוך** (`5c5d6bb0`), 28,312 schools and kindergartens.
  The columns are labelled `ITM_X/ITM_Y` and `UTM_X/UTM_Y`; the "UTM" pair is
  neither - it is plain WGS84 degrees (35.0976, 32.7465), so it is read as
  lon/lat and the ITM pair is ignored. The register grades its own accuracy in
  `RAMAT_DIYUK_MIKUM`, and that grade is kept and carried through.

Why the modes are not one number
--------------------------------
A stop is not a stop. A rail platform is a commute to another city, a light-rail
stop is a commute across this one, and an ordinary bus stop is a stop - useful,
common, and present within 500 m of most of urban Israel, which makes it nearly
worthless as a discriminator. Counting all 34,166 equally would have scored a
street with eight bus stops above one with a train station, so the tiers are
scored separately: distance to the nearest *rail-grade* station, and density of
ordinary stops as a second, weaker signal.

What this does not claim
------------------------
Straight-line metres, not walking minutes. A stop 300 m away across a motorway
or a wadi is not 300 m away on foot, and this module has no street network to
know that. It is also silent on frequency: the register says a stop exists, not
that anything calls there twice an hour. Both are real overstatements and both
run in the same direction, so the figure is a ceiling on accessibility - read it
as "could plausibly be near transit", never as "is fifteen minutes from work".
"""

from __future__ import annotations

import math
from collections import defaultdict

from http_client import build_session

CKAN = "https://data.gov.il/api/3/action/datastore_search"
PAGE = 5000

RES_TRANSIT = "e873e6a2-66c1-494f-a677-f5e77348edb0"
RES_SCHOOLS = "5c5d6bb0-755d-470d-84b6-d7dd3135ba9c"

#: Stop type -> tier. Tiers are the whole point of the table; see the module
#: docstring. `rail` is anything on a fixed guideway plus BRT, which in Israel
#: runs on dedicated lanes and is a different service from a bus stop.
#: Everything unlisted falls to `bus`, which is the safe direction: a mode we
#: have not classified must not be promoted to rail.
STOP_TIERS = {
    "רכבת ישראל": "rail",
    "רכבת ישראל - רציפים": "rail",
    "גבול תחנת רכבת": "rail",
    "רכבת קלה": "light_rail",
    "רכבת קלה - רציפים": "light_rail",
    'גבול רקל"': "light_rail",
    "מטרונית / BRT": "brt",
    "רכבל": "brt",
    "רכבל - רציפים": "brt",
    "מסוף": "hub",
    "גבול תחנה מרכזית": "hub",
    "מרכזית רציפים": "hub",
}
DEFAULT_TIER = "bus"

#: Tiers that count as "rail-grade" for the distance measure.
RAIL_TIERS = ("rail", "light_rail", "brt", "hub")

#: Israel spans ~29.5N to 33.3N. One degree of latitude is 111.32 km; one
#: degree of longitude is that times cos(lat), which at 32N is 0.848. Using a
#: single scale factor for the whole country costs under 2% at the extremes -
#: far inside the error already introduced by measuring straight lines instead
#: of walking routes.
LAT_M = 111_320.0
MEAN_LAT = 31.8
LON_M = LAT_M * math.cos(math.radians(MEAN_LAT))

#: Grid cell edge, in degrees of latitude - about 1.1 km. Chosen so that any
#: search radius under a kilometre is answered by the cell and its eight
#: neighbours, which is a 9-cell scan instead of 34,166 distance computations
#: per listing.
CELL = 0.01

TABLES = """
CREATE TABLE IF NOT EXISTS transit_stops (
    station_id  INTEGER PRIMARY KEY,
    city_code   INTEGER,
    city        TEXT,
    metropolin  TEXT,
    type_name   TEXT,
    tier        TEXT NOT NULL,      -- rail | light_rail | brt | hub | bus
    operator    TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS education_sites (
    semel       TEXT PRIMARY KEY,
    name        TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    accuracy    TEXT                -- the register's own grade of the fix
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_transit_tier ON transit_stops(tier);
CREATE INDEX IF NOT EXISTS idx_transit_pos  ON transit_stops(lat, lon);
CREATE INDEX IF NOT EXISTS idx_school_pos   ON education_sites(lat, lon);
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


# ---------------------------------------------------------------- ingestion

def _coord(value, low, high):
    """A coordinate, or None when it is not one.

    Bounded to Israel rather than to the globe. Both registers carry rows at
    0/0 and a handful outside the country, and a stop in the Gulf of Guinea
    does not fail any general sanity test - it just quietly becomes the
    "nearest station" to nothing and drags a bounding box across the planet.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if low <= num <= high else None


def _ckan_all(resource_id, timeout=(20, 180), should_stop=None):
    offset = 0
    while True:
        if should_stop and should_stop():
            return
        payload = _sess().get(CKAN, params={
            "resource_id": resource_id, "limit": PAGE, "offset": offset,
        }, timeout=timeout).json()
        if not payload.get("success"):
            raise RuntimeError(f"CKAN search failed for {resource_id} at {offset}")
        records = payload["result"]["records"]
        if not records:
            return
        yield from records
        offset += len(records)
        if len(records) < PAGE:
            return


def import_transit(conn, progress=None, should_stop=None, timeout=(20, 180)) -> int:
    ensure_schema(conn)
    rows = []
    for record in _ckan_all(RES_TRANSIT, timeout, should_stop):
        lat = _coord(record.get("Lat"), 29.0, 33.5)
        lon = _coord(record.get("Long"), 34.0, 36.0)
        station_id = record.get("StationId")
        if lat is None or lon is None or station_id is None:
            continue
        type_name = (record.get("StationTypeName") or "").strip()
        rows.append((int(station_id), record.get("CityCode"),
                     record.get("CityName"), record.get("MetropolinName"),
                     type_name, STOP_TIERS.get(type_name, DEFAULT_TIER),
                     record.get("StationOperatorTypeName"), lat, lon))
        if progress and len(rows) % PAGE == 0:
            progress("transit", len(rows))
    if not rows:
        return 0
    conn.execute("DELETE FROM transit_stops")
    conn.executemany(
        "INSERT OR REPLACE INTO transit_stops (station_id, city_code, city, "
        "metropolin, type_name, tier, operator, lat, lon) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    if progress:
        progress("transit", len(rows))
    return len(rows)


def import_schools(conn, progress=None, should_stop=None, timeout=(20, 180)) -> int:
    ensure_schema(conn)
    rows = []
    for record in _ckan_all(RES_SCHOOLS, timeout, should_stop):
        # Named UTM, actually WGS84 degrees. See the module docstring.
        lon = _coord(record.get("UTM_X"), 34.0, 36.0)
        lat = _coord(record.get("UTM_Y"), 29.0, 33.5)
        semel = str(record.get("SEMEL_MOSAD") or "").strip()
        if lat is None or lon is None or not semel:
            continue
        rows.append((semel, record.get("SHEM_MOSAD"), lat, lon,
                     record.get("RAMAT_DIYUK_MIKUM")))
        if progress and len(rows) % PAGE == 0:
            progress("schools", len(rows))
    if not rows:
        return 0
    conn.execute("DELETE FROM education_sites")
    conn.executemany(
        "INSERT OR REPLACE INTO education_sites (semel, name, lat, lon, accuracy) "
        "VALUES (?,?,?,?,?)", rows)
    conn.commit()
    if progress:
        progress("schools", len(rows))
    return len(rows)


def import_all(conn, progress=None, should_stop=None, timeout=(20, 180)) -> int:
    total = import_transit(conn, progress, should_stop, timeout)
    if not (should_stop and should_stop()):
        total += import_schools(conn, progress, should_stop, timeout)
    return total


def counts(conn) -> dict:
    try:
        tiers = {r["tier"]: r["n"] for r in conn.execute(
            "SELECT tier, COUNT(*) n FROM transit_stops GROUP BY tier")}
        schools = conn.execute("SELECT COUNT(*) n FROM education_sites").fetchone()["n"]
    except Exception:
        return {"transit": 0, "schools": 0, "tiers": {}}
    return {"transit": sum(tiers.values()), "schools": schools, "tiers": tiers}


# ------------------------------------------------------------------ geometry

def metres(lat1, lon1, lat2, lon2) -> float:
    """Equirectangular distance. See LAT_M for why this is good enough here."""
    dy = (lat1 - lat2) * LAT_M
    dx = (lon1 - lon2) * LON_M
    return math.hypot(dx, dy)


class PointIndex:
    """A grid over points, for "what is within N metres of here".

    Not an STRtree: the tree in `listing_planning` exists to test a point
    against 20,261 *polygons*, where the candidate filter has to be a real
    bounding-box query. Here both sides are points and the query is a fixed
    radius, so a dictionary keyed on rounded coordinates answers it in nine
    lookups with no dependency on shapely - which matters, because this layer
    has to keep working on a machine where shapely is not installed.
    """

    __slots__ = ("cells", "size")

    def __init__(self, points):
        self.cells = defaultdict(list)
        self.size = 0
        for point in points:
            self.cells[self._cell(point[0], point[1])].append(point)
            self.size += 1

    @staticmethod
    def _cell(lat, lon):
        return (int(lat / CELL), int(lon / CELL))

    def _candidates(self, lat, lon, radius_m):
        """Cells that can hold a point within the radius.

        The span is computed from the radius rather than fixed at the eight
        neighbours, so a 2 km query does not silently return only what happened
        to fall in the adjacent 1.1 km cells.
        """
        span_lat = int(radius_m / (CELL * LAT_M)) + 1
        span_lon = int(radius_m / (CELL * LON_M)) + 1
        cy, cx = self._cell(lat, lon)
        for dy in range(-span_lat, span_lat + 1):
            for dx in range(-span_lon, span_lon + 1):
                bucket = self.cells.get((cy + dy, cx + dx))
                if bucket:
                    yield from bucket

    #: The three methods below compare **squared** metres and never call
    #: `metres`. Each runs once per listing per layer - 61,478 listings x three
    #: layers x every candidate in nine grid cells, which in central Tel Aviv
    #: is a few hundred points a call. At that volume the square root and the
    #: function call are the whole cost: dropping both took the annotation pass
    #: from 37 s to 13 s. `nearest` still takes one root, on the winner only.

    def within(self, lat, lon, radius_m) -> list:
        limit = radius_m * radius_m
        out = []
        for point in self._candidates(lat, lon, radius_m):
            dy = (lat - point[0]) * LAT_M
            dx = (lon - point[1]) * LON_M
            if dy * dy + dx * dx <= limit:
                out.append(point)
        return out

    def count_within(self, lat, lon, radius_m) -> int:
        limit = radius_m * radius_m
        total = 0
        for point in self._candidates(lat, lon, radius_m):
            dy = (lat - point[0]) * LAT_M
            dx = (lon - point[1]) * LON_M
            if dy * dy + dx * dx <= limit:
                total += 1
        return total

    def nearest(self, lat, lon, max_m) -> tuple | None:
        """Closest point and its distance, or None if nothing is inside max_m.

        Bounded on purpose. An unbounded nearest-neighbour over a country
        always answers - it would report the nearest railway station to a
        Galilee village as 41 km away, a true number that means "no rail here"
        and reads like a measurement.
        """
        best, best_sq = None, max_m * max_m
        for point in self._candidates(lat, lon, max_m):
            dy = (lat - point[0]) * LAT_M
            dx = (lon - point[1]) * LON_M
            squared = dy * dy + dx * dx
            if squared <= best_sq:
                best, best_sq = point, squared
        return None if best is None else (best, math.sqrt(best_sq))


_CACHE: dict = {}


def transit_index(conn, tiers=RAIL_TIERS) -> PointIndex:
    key = ("transit", tuple(tiers))
    if key not in _CACHE:
        placeholders = ",".join("?" * len(tiers))
        rows = conn.execute(
            f"SELECT lat, lon, tier, city, type_name FROM transit_stops "
            f"WHERE tier IN ({placeholders})", tuple(tiers)).fetchall()
        _CACHE[key] = PointIndex([(r["lat"], r["lon"], r["tier"], r["city"],
                                   r["type_name"]) for r in rows])
    return _CACHE[key]


def school_index(conn) -> PointIndex:
    if "schools" not in _CACHE:
        rows = conn.execute("SELECT lat, lon, name FROM education_sites").fetchall()
        _CACHE["schools"] = PointIndex([(r["lat"], r["lon"], r["name"])
                                        for r in rows])
    return _CACHE["schools"]


def clear_cache() -> None:
    _CACHE.clear()


if __name__ == "__main__":                       # pragma: no cover - CLI
    import json

    import db

    conn = db.get_conn()
    try:
        total = import_all(conn, progress=lambda k, n: print(f"  {k}: {n}"))
        print(f"imported {total} rows")
        print(json.dumps(counts(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
