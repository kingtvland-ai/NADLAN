"""
PlanWatch - GovMap search client
=================================
Uses the PUBLIC search API that govmap.gov.il's own front-end calls (no token,
no login) to turn a גוש/חלקה into coordinates:

    https://es.govmap.gov.il/TldSearch/api/AutoComplete?query=...&gid=govmap
    https://es.govmap.gov.il/TldSearch/api/DetailsByQuery?query=...&lyrs=...&gid=govmap

Verified live (2026-07): DetailsByQuery for "גוש 6941 חלקה 9" returns the
parcel centroid in the PARCEL_ALL_SHUMA layer. This is the same endpoint the
open-source Meirim project uses. Be a polite client: single lookups, cached
(PlanWatch caches in the parcel_coords table), no bulk scraping.

Coordinates come back in the Israeli TM Grid (ITM / EPSG:2039), so this module
also carries a pure-Python ITM<->WGS84 conversion - no pyproj dependency. The
new ITM datum (IG05) is aligned with WGS84 to well under a metre, which is
noise at parcel-centroid scale.
"""

from __future__ import annotations

import math

from http_client import build_session

BASE = "https://es.govmap.gov.il/TldSearch/api"
GID = "govmap"
#: Layer bitmask observed in govmap's own requests - includes the parcel layers.
LYRS = "276267023"

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


# ----------------------------------------------------------- ITM <-> WGS84
# Transverse Mercator on GRS80 with the official ITM parameters.

_A = 6378137.0                # GRS80 semi-major
_F = 1 / 298.257222101        # GRS80 flattening
_E2 = _F * (2 - _F)           # first eccentricity^2
_EP2 = _E2 / (1 - _E2)        # second eccentricity^2

_K0 = 1.0000067               # ITM scale factor
_LON0 = math.radians(35 + 12 / 60 + 16.261 / 3600)   # 35°12'16.261"E
_LAT0 = math.radians(31 + 44 / 60 + 3.817 / 3600)    # 31°44'03.817"N
_FE = 219529.584              # false easting
_FN = 626907.390              # false northing


def _merid_arc(lat: float) -> float:
    """Meridian arc length from the equator (series expansion, GRS80)."""
    e2 = _E2
    return _A * (
        (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256) * lat
        - (3 * e2 / 8 + 3 * e2**2 / 32 + 45 * e2**3 / 1024) * math.sin(2 * lat)
        + (15 * e2**2 / 256 + 45 * e2**3 / 1024) * math.sin(4 * lat)
        - (35 * e2**3 / 3072) * math.sin(6 * lat)
    )


_M0 = _merid_arc(_LAT0)


def itm_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """(easting, northing) in ITM -> (lat, lon) in WGS84 degrees."""
    m = _M0 + (y - _FN) / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))

    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
            + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
            + (151 * e1**3 / 96) * math.sin(6 * mu)
            + (1097 * e1**4 / 512) * math.sin(8 * mu))

    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    n1 = _A / math.sqrt(1 - _E2 * sin1**2)
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1**2) ** 1.5
    t1 = tan1**2
    c1 = _EP2 * cos1**2
    d = (x - _FE) / (n1 * _K0)

    lat = phi1 - (n1 * tan1 / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _EP2 - 3 * c1**2) * d**6 / 720
    )
    lon = _LON0 + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _EP2 + 24 * t1**2) * d**5 / 120
    ) / cos1
    return math.degrees(lat), math.degrees(lon)


def wgs84_to_itm(lat: float, lon: float) -> tuple[float, float]:
    """(lat, lon) WGS84 degrees -> (easting, northing) ITM."""
    phi, lam = math.radians(lat), math.radians(lon)
    sin_p, cos_p, tan_p = math.sin(phi), math.cos(phi), math.tan(phi)
    n = _A / math.sqrt(1 - _E2 * sin_p**2)
    t = tan_p**2
    c = _EP2 * cos_p**2
    a_ = (lam - _LON0) * cos_p
    m = _merid_arc(phi)

    x = _FE + _K0 * n * (
        a_ + (1 - t + c) * a_**3 / 6
        + (5 - 18 * t + t**2 + 72 * c - 58 * _EP2) * a_**5 / 120
    )
    y = _FN + _K0 * (m - _M0 + n * tan_p * (
        a_**2 / 2 + (5 - t + 9 * c + 4 * c**2) * a_**4 / 24
        + (61 - 58 * t + t**2 + 600 * c - 330 * _EP2) * a_**6 / 720
    ))
    return x, y


# ------------------------------------------------------------------ search

class GovMapError(RuntimeError):
    pass


def _get(path: str, params: dict, timeout=30) -> dict:
    resp = _sess().get(f"{BASE}/{path}", params={**params, "gid": GID},
                       timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data


def parcel_point(gush: str | int, helka: str | int) -> tuple[float, float] | None:
    """
    WGS84 (lat, lon) centroid for a parcel, or None if GovMap has no match.
    Accepts both regular and shuma numbering ("גוש 6941 חלקה 9" resolves either).
    """
    query = f"גוש {gush} חלקה {helka}"
    data = _get("DetailsByQuery", {"query": query, "lyrs": LYRS})
    if data.get("Error") not in (0, None):
        return None
    for layer in data.get("order") or []:
        for hit in data.get("data", {}).get(layer) or []:
            x, y = hit.get("X"), hit.get("Y")
            if x and y:
                return itm_to_wgs84(float(x), float(y))
    return None


def search(query: str) -> list[dict]:
    """
    Free-text search (addresses, gush/helka, settlements). Returns
    [{label, layer, lat, lon}] - useful for a dashboard search box.
    """
    data = _get("DetailsByQuery", {"query": query, "lyrs": LYRS})
    results = []
    if data.get("Error") not in (0, None):
        return results
    for layer in data.get("order") or []:
        for hit in data.get("data", {}).get(layer) or []:
            x, y = hit.get("X"), hit.get("Y")
            if not (x and y):
                continue
            lat, lon = itm_to_wgs84(float(x), float(y))
            results.append({
                "label": hit.get("ResultLable"),
                "layer": layer,
                "gush": hit.get("Gush") or None,
                "helka": hit.get("Parcel") or None,
                "lat": round(lat, 7), "lon": round(lon, 7),
            })
    return results


#: The national cadastre, and the answer to "which parcel is this point in".
#:
#: `TldSearch` resolves an address to a point and returns **no parcel at all**
#: on its ADDRESS layer, so address -> gush/helka needed a second source. This
#: is it: the identify endpoint govmap's own map calls when a user clicks, on
#: `PARCEL_ALL` - the Survey of Israel's registered-parcel layer (the service
#: tags it קדסטר · המרכז למיפוי ישראל). No token, no login, same posture as the
#: search endpoint above.
#:
#: It is a POST. Every GET spelling of it answers 404 with the site's HTML
#: error page, which is why this looked unavailable at first.
IDENTIFY_URL = "https://ags.govmap.gov.il/Identify/IdentifyByXY"

#: Field names in the response, which is keyed in Hebrew rather than by code.
_F_GUSH, _F_HELKA = "מספר גוש", "חלקה"
_F_AREA, _F_STATUS = 'שטח רשום (מ"ר)', "סטטוס"


def parcel_at_point(lat: float, lon: float, *, tolerance: int = 0,
                    timeout=30) -> dict | None:
    """Which registered parcel a WGS84 point falls in. National coverage.

    `tolerance` is in metres and defaults to **0** deliberately. Measured on
    two Tel Aviv addresses at 0, 1, 5, 10 and 30 m, the layer returned exactly
    one parcel every time and the query point sat inside its extent - so a
    tolerance buys nothing here and can only start pulling in neighbours near a
    boundary, which is the one error that must not happen: the whole parcel
    dossier is built on this number, and a neighbouring helka opens a confident
    file on the wrong property.

    Returns None where the layer has no parcel (unregistered land, sea, some
    road bodies) rather than guessing at the nearest one.
    """
    x, y = wgs84_to_itm(lat, lon)
    payload = {"x": x, "y": y, "mapTolerance": tolerance,
               "IsPersonalSite": False,
               "layers": [{"LayerType": 0, "LayerName": "PARCEL_ALL",
                           "LayerFilter": ""}]}
    try:
        response = _sess().post(IDENTIFY_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except (ValueError, OSError):
        return None
    for layer in data.get("data") or []:
        for result in layer.get("Result") or []:
            fields = {}
            for tab in result.get("tabs") or []:
                for field in tab.get("fields") or []:
                    fields[field.get("FieldName")] = field.get("FieldValue")
            gush, helka = fields.get(_F_GUSH), fields.get(_F_HELKA)
            if not (gush and helka):
                continue
            out = {"gush": str(gush).strip(), "helka": str(helka).strip(),
                   "status": fields.get(_F_STATUS), "source": "מפ״י (GovMap)"}
            try:
                out["area_registered_m2"] = float(str(fields.get(_F_AREA)).strip())
            except (TypeError, ValueError):
                out["area_registered_m2"] = None
            return out
    return None


def map_link(gush, helka) -> str:
    """Deep-link to the govmap viewer centred on the parcel search."""
    from urllib.parse import quote
    return f"https://www.govmap.gov.il/?q={quote(f'גוש {gush} חלקה {helka}')}&z=10"


if __name__ == "__main__":
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    gush, helka = (sys.argv + ["6941", "9"])[1:3]
    print(f"גוש {gush} חלקה {helka} ->", parcel_point(gush, helka))
