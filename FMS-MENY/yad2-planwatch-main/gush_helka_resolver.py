"""
PlanWatch - גוש/חלקה -> coordinates resolver
=============================================
Turns a cadastral parcel id (גוש/חלקה) into a (lat, lon) that sync.py can
point-in-polygon against plan geometry.

Why this is pluggable
---------------------
The Blue Lines service publishes plan geometry but NOT cadastral boundaries,
and Israel's cadastre is not available as a single free public API. Verified
while building this:

  * ags.iplan.gov.il/arcgisiplan - PlanningPublic folder has no parcel layer
    (its `reka` service is just a Middle East background polygon).
  * data.gov.il - only "שכבת חלקות שומה" (municipal *assessment* parcels),
    which is not the official גוש/חלקה cadastre.

So the source is a licensing decision, not a coding one, and this module keeps
it behind one small interface. Options, roughly by how "official" they are:

  1. GovMap (govmap.gov.il) - the state map portal has search-by-gush/helka in
     its UI. Ask מרכז המיפוי הממשלתי about approved programmatic/commercial
     access to the cadastral layer rather than reverse-engineering the site.
  2. Survey of Israel / Israel Land Authority cadastral extracts, licensed
     directly.
  3. A third-party GIS vendor already licensed to redistribute Israeli
     cadastral data - usually fastest for a paid product, since the licensing
     question is already solved.

Once you have data from any of those, you do not need to touch sync.py:

  * a parcels file (GeoJSON / newline-delimited GeoJSON) -> `LocalParcelsProvider`
  * an ArcGIS FeatureServer of parcels                   -> `ArcGisParcelsProvider`
  * anything else -> implement `resolve(gush, helka) -> (lat, lon) | None`

Resolved coordinates are cached in the `parcel_coords` table, so a licensed
source is queried once per parcel.

Usage
-----
    import gush_helka_resolver as ghr

    ghr.set_provider(ghr.LocalParcelsProvider("parcels.geojson"))
    latlon = ghr.resolve("6941", "23")          # -> (32.07, 34.78) or None

    # or, honouring the cache and writing subscriptions back:
    ghr.resolve_pending_subscriptions(conn)

Environment configuration (so no code change is needed in deployment):
    PLANWATCH_PARCELS_FILE=C:\\data\\parcels.geojson
    PLANWATCH_PARCELS_URL=https://.../FeatureServer/0
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Protocol

#: `parcel_coords` normally comes from db.TABLES. This is only a safety net for
#: a connection opened outside get_conn(); it uses execute() rather than
#: executescript(), which would implicitly COMMIT the caller's open transaction.
_CACHE_DDL = """CREATE TABLE IF NOT EXISTS parcel_coords (
    gush TEXT NOT NULL, helka TEXT NOT NULL, lat REAL, lon REAL,
    source TEXT, resolved_at TEXT NOT NULL, PRIMARY KEY (gush, helka))"""


class ParcelNotFound(LookupError):
    """The provider ran but has no such parcel."""


class ResolverNotConfigured(RuntimeError):
    """No cadastral provider has been configured."""


class ParcelProvider(Protocol):
    """Anything that can turn a גוש/חלקה into a WGS84 (lat, lon)."""

    name: str

    def lookup(self, gush: str, helka: str) -> tuple[float, float] | None:
        ...


def _norm(value) -> str:
    """
    Normalise a parcel id for comparison: strip whitespace, drop leading zeros
    ("0023" and "23" are the same חלקה in most exports).
    """
    text = str(value).strip()
    return text.lstrip("0") or "0"


class LocalParcelsProvider:
    """
    Reads parcels from a local GeoJSON (or newline-delimited GeoJSON) file -
    the shape you get after exporting a licensed cadastral layer.

    Features are expected to carry gush/helka in their properties under any of
    the usual spellings (`gush`/`GUSH`/`gush_num`/`gush_suffix`, `helka`/
    `HELKA`/`parcel`/`helka_num`, or a combined `gush_helka` like "6941/23").
    Coordinates must be WGS84 (EPSG:4326); reproject on export if they are in
    Israel TM Grid (EPSG:2039).

    The index is built lazily on first lookup and kept in memory.
    """

    name = "local_parcels_file"

    GUSH_KEYS = ("gush", "GUSH", "gush_num", "GUSH_NUM", "gush_suffix", "ms_gush")
    HELKA_KEYS = ("helka", "HELKA", "helka_num", "HELKA_NUM", "parcel",
                  "PARCEL", "ms_helka")
    COMBINED_KEYS = ("gush_helka", "GUSH_HELKA", "gush_chelka")

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._index: dict[tuple[str, str], tuple[float, float]] | None = None

    def _load(self) -> dict:
        if self._index is not None:
            return self._index
        if not self.path.exists():
            raise ResolverNotConfigured(f"Parcels file not found: {self.path}")

        index: dict[tuple[str, str], tuple[float, float]] = {}
        for feature in self._iter_features():
            key = self._key(feature.get("properties") or {})
            if key is None:
                continue
            centroid = self._centroid(feature.get("geometry"))
            if centroid:
                index[key] = centroid

        if not index:
            raise ResolverNotConfigured(
                f"No parcels with recognisable gush/helka properties in {self.path}. "
                f"Expected one of {self.GUSH_KEYS} + {self.HELKA_KEYS}, "
                f"or a combined {self.COMBINED_KEYS}."
            )
        self._index = index
        return index

    def _iter_features(self):
        """Handles both a FeatureCollection and newline-delimited GeoJSON."""
        text = self.path.read_text(encoding="utf-8")
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                if data.get("type") == "FeatureCollection":
                    yield from data.get("features", [])
                    return
                if data.get("type") == "Feature":
                    yield data
                    return
        # newline-delimited GeoJSON fallback
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "Feature":
                yield obj

    @classmethod
    def _key(cls, props: dict) -> tuple[str, str] | None:
        for key in cls.COMBINED_KEYS:
            if props.get(key):
                text = str(props[key])
                for sep in ("/", "-", "_", " "):
                    if sep in text:
                        gush, _, helka = text.partition(sep)
                        return _norm(gush), _norm(helka)
        gush = next((props[k] for k in cls.GUSH_KEYS if props.get(k) is not None), None)
        helka = next((props[k] for k in cls.HELKA_KEYS if props.get(k) is not None), None)
        if gush is None or helka is None:
            return None
        return _norm(gush), _norm(helka)

    @staticmethod
    def _centroid(geometry) -> tuple[float, float] | None:
        """(lat, lon) centroid. Uses shapely so it lands inside odd shapes."""
        if not geometry:
            return None
        try:
            from shapely.geometry import shape

            geom = shape(geometry)
            if not geom.is_valid:
                geom = geom.buffer(0)
            point = geom.representative_point() if geom.area else geom.centroid
            return (point.y, point.x)
        except Exception:
            return None

    def lookup(self, gush: str, helka: str) -> tuple[float, float] | None:
        return self._load().get((_norm(gush), _norm(helka)))


class ArcGisParcelsProvider:
    """
    Queries a licensed ArcGIS FeatureServer/MapServer parcels layer.

    `url` is the layer endpoint (…/FeatureServer/0). Field names default to the
    common Survey-of-Israel spellings; override to match your layer. Reuses the
    project's TLS/WAF-hardened session, so it works against gov endpoints with
    the same legacy-cipher quirk as the Blue Lines service.
    """

    name = "arcgis_parcels"

    def __init__(self, url: str, gush_field: str = "GUSH_NUM",
                 helka_field: str = "PARCEL", token: str | None = None,
                 session=None):
        self.url = url.rstrip("/")
        self.gush_field = gush_field
        self.helka_field = helka_field
        self.token = token
        self._session = session

    @property
    def session(self):
        if self._session is None:
            from http_client import build_session

            self._session = build_session()
        return self._session

    def lookup(self, gush: str, helka: str) -> tuple[float, float] | None:
        from http_client import DEFAULT_TIMEOUT, assert_json_response

        params = {
            "f": "geojson",
            "where": f"{self.gush_field}={int(_norm(gush))} "
                     f"AND {self.helka_field}={int(_norm(helka))}",
            "outFields": f"{self.gush_field},{self.helka_field}",
            "returnGeometry": "true",
            "outSR": "4326",
            "resultRecordCount": 1,
        }
        if self.token:
            params["token"] = self.token

        resp = self.session.get(f"{self.url}/query", params=params,
                                timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        assert_json_response(resp)
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Parcels service error: {data['error']}")

        features = data.get("features") or []
        if not features:
            return None
        return LocalParcelsProvider._centroid(features[0].get("geometry"))


class GovMapProvider:
    """
    Resolves via govmap.gov.il's public search API (the same endpoint the site's
    own front-end uses; verified live 2026-07). Returns the parcel centroid in
    WGS84. This is the zero-configuration default - the file/ArcGIS providers
    remain for licensed bulk data, which is still the right choice for high
    volume or offline use.
    """

    name = "govmap_search"

    def lookup(self, gush: str, helka: str) -> tuple[float, float] | None:
        import govmap_client
        return govmap_client.parcel_point(_norm(gush), _norm(helka))


_provider: ParcelProvider | None = None


def set_provider(provider: ParcelProvider | None) -> None:
    """Install the cadastral provider used by `resolve()`."""
    global _provider
    _provider = provider


def get_provider() -> ParcelProvider:
    """
    The configured provider, auto-wiring from the environment on first use:
    PLANWATCH_PARCELS_FILE, else PLANWATCH_PARCELS_URL.
    """
    global _provider
    if _provider is not None:
        return _provider

    path = os.environ.get("PLANWATCH_PARCELS_FILE")
    if path:
        _provider = LocalParcelsProvider(path)
        return _provider

    url = os.environ.get("PLANWATCH_PARCELS_URL")
    if url:
        _provider = ArcGisParcelsProvider(
            url,
            gush_field=os.environ.get("PLANWATCH_PARCELS_GUSH_FIELD", "GUSH_NUM"),
            helka_field=os.environ.get("PLANWATCH_PARCELS_HELKA_FIELD", "PARCEL"),
            token=os.environ.get("PLANWATCH_PARCELS_TOKEN"),
        )
        return _provider

    # Zero-config default: govmap's public parcel search (cached per parcel
    # in parcel_coords, so each lookup hits the service once, ever).
    _provider = GovMapProvider()
    return _provider


def resolve(gush: str, helka: str) -> tuple[float, float] | None:
    """
    (lat, lon) for a parcel, or None if the configured source has no such
    parcel. Raises ResolverNotConfigured if no source is set up.
    """
    return get_provider().lookup(gush, helka)


def resolve_cached(conn, gush: str, helka: str) -> tuple[float, float] | None:
    """`resolve()` with a persistent cache, so a paid source is hit once."""
    conn.execute(_CACHE_DDL)
    gush_n, helka_n = _norm(gush), _norm(helka)

    row = conn.execute(
        "SELECT lat, lon FROM parcel_coords WHERE gush=? AND helka=?",
        (gush_n, helka_n),
    ).fetchone()
    if row is not None:
        return (row["lat"], row["lon"]) if row["lat"] is not None else None

    provider = get_provider()
    result = provider.lookup(gush_n, helka_n)

    import db as _db

    conn.execute(
        """INSERT OR REPLACE INTO parcel_coords
           (gush, helka, lat, lon, source, resolved_at) VALUES (?,?,?,?,?,?)""",
        (gush_n, helka_n,
         result[0] if result else None,
         result[1] if result else None,
         getattr(provider, "name", "unknown"), _db.now_iso()),
    )
    conn.commit()
    return result


def resolve_pending_subscriptions(conn, verbose: bool = True) -> int:
    """
    Fill in lat/lon for every active subscription that has a גוש/חלקה but no
    coordinates yet. Returns how many were resolved.

    Run this after adding subscriptions and before sync.py's matching, or the
    matcher will skip them (it only considers subscriptions with coordinates).
    """
    pending = conn.execute(
        """SELECT id, gush, helka, label FROM subscriptions
           WHERE active=1 AND (lat IS NULL OR lon IS NULL)
             AND gush IS NOT NULL AND helka IS NOT NULL"""
    ).fetchall()
    if not pending:
        if verbose:
            print("No subscriptions awaiting coordinate resolution.")
        return 0

    resolved = 0
    for sub in pending:
        try:
            result = resolve_cached(conn, sub["gush"], sub["helka"])
        except ResolverNotConfigured:
            raise
        except Exception as exc:
            if verbose:
                print(f"  gush {sub['gush']} helka {sub['helka']}: lookup failed - {exc}")
            continue

        if not result:
            if verbose:
                print(f"  gush {sub['gush']} helka {sub['helka']}: not found")
            continue

        lat, lon = result
        conn.execute("UPDATE subscriptions SET lat=?, lon=? WHERE id=?",
                     (lat, lon, sub["id"]))
        resolved += 1
        if verbose:
            label = sub["label"] or f"{sub['gush']}/{sub['helka']}"
            print(f"  {label}: {lat:.6f}, {lon:.6f}")

    conn.commit()
    if verbose:
        print(f"Resolved {resolved}/{len(pending)} subscriptions.")
    return resolved


if __name__ == "__main__":
    import argparse

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Resolve גוש/חלקה to coordinates.")
    parser.add_argument("gush", nargs="?")
    parser.add_argument("helka", nargs="?")
    parser.add_argument("--pending", action="store_true",
                        help="resolve all subscriptions missing coordinates")
    args = parser.parse_args()

    import db as _db

    try:
        if args.pending:
            conn = _db.get_conn()
            resolve_pending_subscriptions(conn)
            conn.close()
        elif args.gush and args.helka:
            result = resolve(args.gush, args.helka)
            print(f"גוש {args.gush} חלקה {args.helka} -> {result or 'not found'}")
        else:
            parser.print_help()
    except ResolverNotConfigured as exc:
        print(f"Not configured: {exc}")
        sys.exit(2)
