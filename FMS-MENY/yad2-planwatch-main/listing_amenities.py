"""Distance from every listing to the transit and schools around it.

This is the join `amenity_client` exists for: 34,166 stops and 28,312 schools
against 69,055 listings, each of which already carries a real coordinate. The
work is done here rather than in the client because it is a whole-corpus pass
that belongs in the same cache entry as the rest of the scoring - the same
reason `listing_planning` is a separate module from `blue_lines_client`.

The two measures, and why they are shaped differently
-----------------------------------------------------
**Rail-grade distance** is a distance, capped. Rail, light rail, BRT and
terminals are scarce - 1,049 points for the whole country - so most addresses
have none nearby, and that is the finding. Beyond `RAIL_MAX_M` the answer is
None, not a large number: "the nearest station is 41 km away" is arithmetically
true and reads on a screen as a measurement of accessibility rather than as its
absence.

**Ordinary stop density** is a count in a radius, not a distance. 33,117 bus
stops cover urban Israel so thoroughly that "distance to the nearest bus stop"
is under 300 m almost everywhere and separates nothing. How many are reachable
does separate: one stop is a route, six is a junction where routes meet.

Both are straight lines, and both are counted against the listing's own point.
See `amenity_client` for what that overstates.
"""

from __future__ import annotations

import amenity_client
import db

#: Past this, there is no rail-grade station here. 2 km is roughly a 25-minute
#: walk - the outer edge of what anybody treats as "near the station", and well
#: past the 800 m that transit planning normally calls a catchment.
RAIL_MAX_M = 2000

#: Radius for the ordinary-stop count. 500 m is the standard bus catchment and
#: about a six-minute walk.
BUS_RADIUS_M = 500

#: Radius for schools. Wider than the bus catchment because a school run is a
#: daily walk people accept at a greater distance than a bus stop, and because
#: kindergartens and schools are counted together.
SCHOOL_RADIUS_M = 1000


def annotate(rows: list[dict]) -> list[dict]:
    """Attach transit and school proximity to every listing with a point."""
    conn = db.get_conn()
    try:
        counts = amenity_client.counts(conn)
        if not counts.get("transit"):
            for row in rows:
                row["amenity_basis"] = "שכבת הנגישות לא נטענה"
            return rows
        rail = amenity_client.transit_index(conn)
        buses = amenity_client.transit_index(conn, tiers=("bus",))
        schools = amenity_client.school_index(conn)
    finally:
        conn.close()

    for row in rows:
        lat, lon = row.get("lat"), row.get("lon")
        if lat is None or lon is None:
            row["amenity_basis"] = "אין קואורדינטות למודעה"
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue

        nearest = rail.nearest(lat, lon, RAIL_MAX_M)
        if nearest:
            point, distance = nearest
            row["rail_distance_m"] = round(distance)
            row["rail_tier"] = point[2]
            row["rail_station"] = point[4]
        else:
            # Explicitly recorded, so a scored row can tell "measured, nothing
            # within 2 km" apart from "never measured".
            row["rail_distance_m"] = None
            row["rail_tier"] = None
        row["bus_stops_500m"] = buses.count_within(lat, lon, BUS_RADIUS_M)
        row["schools_1km"] = schools.count_within(lat, lon, SCHOOL_RADIUS_M)
        row["amenity_measured"] = True

        parts = []
        if row.get("rail_distance_m") is not None:
            label = {"rail": "רכבת", "light_rail": "רכבת קלה",
                     "brt": "מטרונית", "hub": "מסוף"}.get(row["rail_tier"], "תחנה")
            parts.append(f"{label} במרחק {row['rail_distance_m']:,} מ׳")
        else:
            parts.append(f"אין תחנת רכבת/רק״ל ברדיוס {RAIL_MAX_M // 1000} ק״מ")
        parts.append(f"{row['bus_stops_500m']} תחנות אוטובוס ב-{BUS_RADIUS_M} מ׳")
        parts.append(f"{row['schools_1km']} מוסדות חינוך בק״מ")
        row["amenity_basis"] = " · ".join(parts)
    return rows


def coverage(rows) -> dict:
    measured = [r for r in rows if r.get("amenity_measured")]
    return {
        "rows": len(rows),
        "measured": len(measured),
        "near_rail": sum(1 for r in measured if r.get("rail_distance_m") is not None),
        "no_bus": sum(1 for r in measured if not r.get("bus_stops_500m")),
    }
