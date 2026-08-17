"""Resolve a listing to its parcel, then hand it everything the parcel knows.

Why the parcel is the key to the whole database
-----------------------------------------------
A listing arrives as a price, an address and a point. Almost everything else
this project holds is keyed on **gush/helka** - rights-holder hints, appraisal
rulings, tender lots, construction stages. So a listing that cannot be resolved
to a parcel is cut off from all of it, and one that can is joined to the lot.

The resolution is point-in-polygon against `muni_parcels`, the municipal GIS
layer. Measured: **7,179 of 61,478 listings** land inside a parcel we hold, and
**917 of those sit on a parcel with a named rights-holder**.

The limit, stated plainly
-------------------------
`muni_parcels` covers **Tel Aviv only** - 45,206 polygons, and no other city
answered (`PROJECT-MAP` section 11 records the 21 servers tested). So this
module enriches Tel Aviv listings and leaves the rest untouched, with
`parcel_basis` saying so. That is not a bug to work around; it is the coverage
we have, and pretending otherwise would put a confident-looking empty field on
54,000 listings.

What each join is actually worth to an agent
--------------------------------------------
* **Rights-holder hints** (`parcel_owners`) come from official publications -
  overwhelmingly appraisal proceedings, where the person is named as a
  litigant. They are a *hint about who holds rights*, never a registry extract,
  and they carry the source's own confidence. An agent uses them to know who
  they are dealing with, not to prove title.
* **An appraisal ruling on the parcel** means someone fought a betterment levy
  here. That is a development-activity marker, and it is independent of any
  asking price - the only price signal in the system that is not somebody's
  opinion of what their own flat is worth.
* **Year built** comes from the municipal building layer. Pre-1980 matters
  specifically: TAMA 38 applies only to buildings permitted before the 1980
  seismic standard, so the year *is* the eligibility test. 21,583 of the
  29,783 dated buildings qualify.
* **A tender lot on the parcel** means the state sold this land, with a
  recorded winning price - a real transaction, not an asking price.
"""

from __future__ import annotations

import json
from collections import defaultdict

import db

#: Buildings permitted before the 1980 seismic standard (תקן 413), which is
#: exactly the TAMA 38 eligibility line. Not a rule of thumb - the year is the
#: statutory test.
TAMA_YEAR = 1980

_CACHE: dict = {}


def _build_tree(sql: str, keep):
    """(STRtree, geometries, meta) for one geometry table."""
    from shapely.geometry import shape
    from shapely.strtree import STRtree

    conn = db.get_conn()
    try:
        rows = conn.execute(sql).fetchall()
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
        if not geom.is_valid:
            geom = geom.buffer(0)
            if geom.is_empty:
                continue
        geometries.append(geom)
        meta.append(keep(row))
    return STRtree(geometries), geometries, meta


def _index():
    """Parcel and building layers, plus everything keyed on gush/helka."""
    if "parcels" in _CACHE:
        return _CACHE

    _CACHE["parcels"] = _build_tree(
        "SELECT gush, helka, geometry_json FROM muni_parcels "
        "WHERE geometry_json IS NOT NULL",
        lambda r: (str(r["gush"]), str(r["helka"])))

    _CACHE["buildings"] = _build_tree(
        "SELECT year_built, geometry_json FROM muni_buildings "
        "WHERE geometry_json IS NOT NULL AND year_built IS NOT NULL",
        lambda r: int(r["year_built"]))

    conn = db.get_conn()
    try:
        owners = defaultdict(list)
        for row in conn.execute(
                """SELECT gush, helka, name, role, confidence, as_of
                     FROM parcel_owners WHERE name IS NOT NULL"""):
            owners[(str(row["gush"]), str(row["helka"]))].append(dict(row))
        _CACHE["owners"] = owners

        appraisals = defaultdict(int)
        for row in conn.execute(
                "SELECT gush, helka, COUNT(*) n FROM appraisal_parcels "
                "GROUP BY gush, helka"):
            appraisals[(str(row["gush"]), str(row["helka"]))] = row["n"]
        _CACHE["appraisals"] = appraisals

        tenders = {}
        for row in conn.execute(
                """SELECT gush, helka, winning_price, price_per_m2, winner
                     FROM tender_lots WHERE gush IS NOT NULL"""):
            tenders[(str(row["gush"]), str(row["helka"]))] = dict(row)
        _CACHE["tenders"] = tenders
    finally:
        conn.close()
    return _CACHE


def annotate(rows: list[dict]) -> list[dict]:
    from shapely.geometry import Point

    try:
        index = _index()
    except Exception as exc:
        for row in rows:
            row["parcel_basis"] = f"שכבת החלקות לא נטענה: {type(exc).__name__}"
        return rows

    parcel_tree, parcel_geoms, parcel_meta = index["parcels"]
    build_tree, build_geoms, build_meta = index["buildings"]

    for row in rows:
        lat, lon = row.get("lat"), row.get("lon")
        if lat is None or lon is None:
            continue
        try:
            point = Point(float(lon), float(lat))
        except (TypeError, ValueError):
            continue

        for idx in parcel_tree.query(point):
            if not parcel_geoms[idx].covers(point):
                continue
            gush, helka = parcel_meta[idx]
            row["gush"], row["helka"] = gush, helka
            row["parcel_basis"] = f"גוש {gush} חלקה {helka} · GIS עירוני"

            hints = index["owners"].get((gush, helka)) or []
            if hints:
                # Highest-confidence first: an agent reads the first line.
                order = {"high": 0, "medium": 1, "low": 2}
                hints = sorted(hints, key=lambda h: order.get(h["confidence"], 3))
                row["owner_count"] = len(hints)
                row["owner_names"] = [h["name"] for h in hints[:4]]
                row["owner_confidence"] = hints[0]["confidence"]
                # Never "owner": these come from published proceedings, not the
                # land registry. The wording is the claim.
                row["owner_basis"] = (
                    f"{len(hints)} רמזי בעלי זכויות · מקור: {hints[0]['role']} · "
                    f"ודאות {hints[0]['confidence']} · {hints[0]['as_of'] or ''}")

            count = index["appraisals"].get((gush, helka))
            if count:
                row["appraisal_count"] = count
                row["appraisal_basis"] = (
                    f"{count} הכרעות שמאות על החלקה — סימן לפעילות השבחה")

            lot = index["tenders"].get((gush, helka))
            if lot and lot.get("winning_price"):
                row["tender_winning_price"] = lot["winning_price"]
                row["tender_winner"] = lot.get("winner")
                row["tender_basis"] = (
                    f"רמ״י מכרה כאן ב-{int(lot['winning_price']):,} ₪"
                    + (f" · {lot['winner']}" if lot.get("winner") else ""))
            break

        for idx in build_tree.query(point):
            if build_geoms[idx].covers(point):
                year = build_meta[idx]
                row["building_year"] = year
                row["building_pre_1980"] = year < TAMA_YEAR
                row["building_basis_year"] = (
                    f"נבנה {year}"
                    + (" · לפני תקן 413 — כשיר לתמ״א 38" if year < TAMA_YEAR else ""))
                break

    if not any(row.get("gush") for row in rows):
        for row in rows:
            row.setdefault("parcel_basis", "אין שכבת חלקות לישוב הזה")
    return rows


def coverage(rows) -> dict:
    return {
        "rows": len(rows),
        "resolved_to_parcel": sum(1 for r in rows if r.get("gush")),
        "with_owner_hint": sum(1 for r in rows if r.get("owner_count")),
        "with_appraisal": sum(1 for r in rows if r.get("appraisal_count")),
        "with_building_year": sum(1 for r in rows if r.get("building_year")),
        "pre_1980": sum(1 for r in rows if r.get("building_pre_1980")),
        "with_tender": sum(1 for r in rows if r.get("tender_winning_price")),
    }
