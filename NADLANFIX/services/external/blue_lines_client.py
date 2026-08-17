"""
PlanWatch - Blue Lines (קווים כחולים) client
=============================================
Talks to the PUBLIC ArcGIS REST MapServer operated by מינהל התכנון. This is
a standard, documented-shape REST/JSON GIS service (not an HTML page), so it
is not subject to the mavat.iplan.gov.il robots.txt block.

    https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/MapServer

Layers (verified against the live service metadata):

    layer 0 - "ישויות נקודתיות"          esriGeometryPoint     (label points)
    layer 1 - "קוים כחולים-תכניות מקוונות"  esriGeometryPolygon  <-- we use this

We query **layer 1**: it is the polygon layer, and it carries both `mp_id`
(needed to build a Mavat deep-link, https://mavat.iplan.gov.il/SV4/1/<mp_id>/310)
and `station_desc` (position in the approval pipeline). Polygons are what
point-in-polygon matching in sync.py needs - the point layer would be useless
for that.

Three things this client has to get right
-----------------------------------------
1. PAGINATION. The layer holds ~36,700 plans but `maxRecordCount` is 1000.
   A single unpaged query silently returns only the first 1000 (2.7% of the
   data) and sets `exceededTransferLimit: true`. We page with
   `resultOffset`/`resultRecordCount` and a stable `orderByFields=objectid ASC`.

2. `f=geojson`, not `f=json`. Esri's own JSON encodes polygons as
   `{"rings": [...]}`, which shapely cannot parse - so downstream matching
   would silently find nothing. The service advertises
   `supportedQueryFormats: "JSON, geoJSON"`, so we ask for real GeoJSON and
   get geometry shapely accepts directly.

3. FIELD NAMES. The date field is `last_update_date`, not `last_update`;
   requesting the latter makes the service answer HTTP 400 "Failed to
   execute query." Esri date fields come back as epoch **milliseconds**
   and are converted to ISO-8601 here.

See http_client.py for the TLS/WAF workarounds this host needs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import requests
from legacy.tools.http_client import DEFAULT_TIMEOUT, assert_json_response, build_session

SERVICE_URL = (
    "https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/MapServer"
)
LAYER_ID = 1  # polygon layer - see module docstring
BASE_URL = f"{SERVICE_URL}/{LAYER_ID}/query"

#: Server-enforced ceiling (layer metadata `maxRecordCount`). Asking for more
#: is silently capped, which would break offset arithmetic.
MAX_PAGE_SIZE = 1000

OUT_FIELDS = [
    # identity
    "objectid", "mp_id", "pl_id", "pl_number", "pl_name", "pl_url",
    # where
    "plan_county_name", "district_name", "jurstiction_area_name", "plan_area_name",
    # what / status
    "entity_subtype_desc", "station_desc", "internet_short_status",
    "pl_area_dunam", "pl_landuse_string", "pl_objectives",
    # when
    "last_update_date", "depositing_date", "open_date",
    "pl_last_deposit_date", "pl_date_advertise", "pl_rejection_date",
    # authorised quantities. CAREFUL - mixed units (verified via field aliases):
    #   105 = hotel rooms, m^2      (חדרי מלון מאושר מ"ר)
    #   110 = special housing, m^2  (דיור מיוחד מאושר מ"ר)
    #   120 = housing UNITS         (מגורים מאושר יח"ד)  <- the only יח"ד field
    "pq_authorised_quantity_105", "pq_authorised_quantity_110",
    "pq_authorised_quantity_120",
]

#: Esri date fields (epoch ms) -> converted to ISO strings by _epoch_ms_to_iso.
DATE_FIELDS = (
    "last_update_date", "depositing_date", "open_date",
    "pl_last_deposit_date", "pl_date_advertise", "pl_rejection_date",
)


class ArcGisError(RuntimeError):
    """The service returned a structured `{"error": ...}` payload."""


def _epoch_ms_to_iso(value) -> str | None:
    """Esri returns dates as epoch milliseconds (or null)."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _request(sess: requests.Session, params: dict, timeout) -> dict:
    """One GET against the layer's /query endpoint, with error unwrapping."""
    resp = sess.get(BASE_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    assert_json_response(resp)  # clear message if the WAF served HTML
    data = resp.json()
    if isinstance(data, dict) and "error" in data:
        raise ArcGisError(f"ArcGIS service error: {data['error']}")
    return data


def count_plans(
    where: str = "1=1",
    session: requests.Session | None = None,
    timeout=DEFAULT_TIMEOUT,
) -> int:
    """How many plans match `where` - used to drive progress reporting."""
    sess = session or build_session()
    data = _request(
        sess,
        {"f": "json", "where": where, "returnCountOnly": "true"},
        timeout,
    )
    return int(data.get("count", 0))


def updated_since_clause(since: str | datetime) -> str:
    """
    A `where` clause for incremental sync.

    `since` is a date/datetime or an ISO string. The service accepts standard
    SQL date literals, e.g. `last_update_date > DATE '2026-01-01'`.
    """
    if isinstance(since, datetime):
        since = since.date().isoformat()
    else:
        since = str(since)[:10]
    return f"last_update_date > DATE '{since}'"


def iter_plans(
    where: str = "1=1",
    session: requests.Session | None = None,
    page_size: int = MAX_PAGE_SIZE,
    timeout=DEFAULT_TIMEOUT,
    include_geometry: bool = True,
    progress=None,
    should_stop=None,
):
    """
    Yield every matching plan as a dict shaped for `db.upsert_plan()`,
    transparently paging through the whole result set.

    `progress` - optional callable(fetched_so_far, total_or_None) invoked
    after each page, so long syncs can report movement.
    `should_stop` - optional callable() -> bool, checked before each page so a
    long pull can be cancelled cleanly on a page boundary (rows already yielded
    stay valid and committed).
    """
    sess = session or build_session()
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    try:
        total = count_plans(where, session=sess, timeout=timeout)
    except (ArcGisError, requests.RequestException):
        total = None  # non-fatal: paging below does not depend on it

    fetched = 0
    offset = 0
    while True:
        if should_stop and should_stop():
            return
        params = {
            # real GeoJSON, so shapely can consume geometry directly
            "f": "geojson" if include_geometry else "json",
            "where": where,
            "outFields": ",".join(OUT_FIELDS),
            "returnGeometry": "true" if include_geometry else "false",
            "outSR": "4326",  # let ArcGIS reproject to WGS84 for us
            # a stable sort is REQUIRED for offset paging to be consistent
            "orderByFields": "objectid ASC",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        data = _request(sess, params, timeout)

        features = data.get("features") or []
        if not features:
            break

        for feature in features:
            yield _feature_to_plan(feature, geojson=include_geometry)

        fetched += len(features)
        offset += len(features)
        if progress:
            progress(fetched, total)

        # A short page means we reached the end. (Do not trust
        # `exceededTransferLimit` alone - it is set whenever more rows exist
        # beyond the window, including on the final requested page.)
        if len(features) < page_size:
            break
        if total is not None and fetched >= total:
            break


def fetch_all_plans(
    session: requests.Session | None = None,
    timeout=DEFAULT_TIMEOUT,
    where: str = "1=1",
    progress=None,
) -> list[dict]:
    """
    Eager version of `iter_plans` - returns a list.

    Note this holds all ~36,700 polygons in memory (roughly 100+ MB); prefer
    `iter_plans` for the scheduled sync, which streams page by page.
    """
    return list(
        iter_plans(where=where, session=session, timeout=timeout, progress=progress)
    )


def _feature_to_plan(feature: dict, geojson: bool = True) -> dict:
    """
    Normalise one feature into the flat dict `db.upsert_plan()` expects.

    GeoJSON puts attributes under "properties"; Esri JSON under "attributes".
    """
    attrs = feature.get("properties") if geojson else feature.get("attributes")
    attrs = attrs or {}
    geom = feature.get("geometry")

    plan = {
        "object_id": attrs.get("objectid"),
        "mp_id": attrs.get("mp_id"),
        "pl_id": attrs.get("pl_id"),
        "pl_number": attrs.get("pl_number"),
        "pl_name": attrs.get("pl_name"),
        "pl_url": attrs.get("pl_url"),
        "county_name": attrs.get("plan_county_name"),
        "district_name": attrs.get("district_name"),
        "jurisdiction_name": attrs.get("jurstiction_area_name"),  # sic: source typo
        "plan_area_name": attrs.get("plan_area_name"),
        "entity_subtype": attrs.get("entity_subtype_desc"),
        "station": attrs.get("station_desc"),
        "short_status": attrs.get("internet_short_status"),
        "area_dunam": attrs.get("pl_area_dunam"),
        "landuse": attrs.get("pl_landuse_string"),
        "objectives": attrs.get("pl_objectives"),
        "housing_units": _housing_units(attrs),
        "hotel_sqm": _float_or_none(attrs.get("pq_authorised_quantity_105")),
        "special_housing_sqm": _float_or_none(attrs.get("pq_authorised_quantity_110")),
        "geometry_json": json.dumps(geom, ensure_ascii=False) if geom else None,
    }
    for field in DATE_FIELDS:
        plan[field] = _epoch_ms_to_iso(attrs.get(field))
    # keep the canonical name the DB layer uses
    plan["last_update"] = plan.pop("last_update_date")
    return plan


def _housing_units(attrs: dict) -> int | None:
    """
    Authorised housing units (יח"ד): field 120 ONLY.

    An earlier version summed buckets 105+110+120, but the field aliases show
    105 and 110 are measured in m^2 (hotel rooms / special housing) - adding
    them to unit counts inflated totals by ~50% and produced absurdities like
    a 731-dunam plan "containing" 367,411 units (really m^2 of hotels).
    """
    value = attrs.get("pq_authorised_quantity_120")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the Blue Lines service (no DB writes)."
    )
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only plans updated after this date")
    parser.add_argument("--limit", type=int, default=3,
                        help="how many plans to print (0 = count only)")
    parser.add_argument("--no-geometry", action="store_true",
                        help="skip geometry (much faster)")
    args = parser.parse_args()

    where = updated_since_clause(args.since) if args.since else "1=1"
    sess = build_session()

    total = count_plans(where, session=sess)
    print(f"Service reports {total:,} plans matching: {where}")
    if args.limit == 0:
        return 0

    shown = 0
    for plan in iter_plans(
        where=where,
        session=sess,
        page_size=min(args.limit, MAX_PAGE_SIZE),
        include_geometry=not args.no_geometry,
    ):
        print(
            f"\n  #{plan['object_id']}  {plan['pl_number']}  [{plan['station']}]\n"
            f"    {plan['pl_name']}\n"
            f"    {plan['county_name']} / {plan['jurisdiction_name']}  "
            f"{plan['area_dunam']} dunam  units={plan['housing_units']}\n"
            f"    updated {plan['last_update']}  {plan['pl_url']}\n"
            f"    geometry: "
            f"{json.loads(plan['geometry_json'])['type'] if plan['geometry_json'] else None}"
        )
        shown += 1
        if shown >= args.limit:
            break
    return 0


if __name__ == "__main__":
    # Hebrew output dies on a cp1255 Windows console otherwise.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_cli())
