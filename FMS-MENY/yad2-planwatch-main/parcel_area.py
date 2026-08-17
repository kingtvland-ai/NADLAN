"""What is on the market, and what has sold, around one parcel.

Why this belongs on the parcel screen
-------------------------------------
The parcel view already answers "what is planned here", "who has a registered
interest", "what did a tribunal rule" and "what did the state get for land next
door". It could not answer the two questions anybody actually opens it with:
**what is for sale around here, and what did the neighbours get for theirs.**

Both answers were already in the database, on two screens the parcel view never
talked to - 69,055 sale listings with coordinates, and 285,444 concluded deals
from the official CC-BY dataset. This module is the join, and it is a join of
two very different shapes, which is the whole difficulty:

* **Listings carry coordinates.** So "around here" is a real radius, measured
  from the parcel's own centroid.
* **Transactions carry no coordinates at all** - the official dataset publishes
  city, street and house number and no point. So "around here" for a sale is a
  *street*, and failing that a *town*. The two are not the same claim and are
  never presented as though they were: every transaction row says which of the
  two matched it.

Two things this deliberately does not do
-----------------------------------------
It does not run the scoring pass. `dashboard._annotated_cached` is a ~100 s
whole-corpus operation on a cold cache, and a parcel lookup that inherited that
cost would be unusable - so the listings here come straight from the stores by
bounding box, carrying what the board published and no derived score. The sale
tab is where scored rows live.

It does not compute a price per m² for transactions. The official dataset has
**no area and no room count on any of its 357,593 rows**, and dividing a price
by an area it does not have is the one number that would look authoritative and
be invented. Prices and dates only - which is exactly what
`external_listings_view.transactions` already refuses to go beyond.
"""

from __future__ import annotations

import statistics

from amenity_client import LAT_M, LON_M, metres

#: Default radius for "listings around this parcel". 500 m is a walk, and in
#: Israeli urban density it typically returns tens of listings rather than
#: hundreds - enough to see a price level, few enough to read.
DEFAULT_RADIUS_M = 500

#: Never return more than this, however dense the area. The parcel view shows
#: them in a table, and a thousand-row table is not an answer.
MAX_ROWS = 60

#: How recent a concluded sale has to be to describe today's market. Older
#: deals are real and are counted in the summary, but the rows shown are the
#: recent ones - a 2015 price on screen next to a 2026 asking price invites
#: exactly the comparison that should not be made.
RECENT_DEAL_ROWS = 40


def _bbox(lat: float, lon: float, radius_m: float):
    """Degree bounds that certainly contain the radius.

    A bounding box first, exact distance second - the same two-stage filter
    `sync._point_hits_plan` uses on plan polygons, for the same reason: SQLite
    can use an index on a range and cannot use one on a distance formula.
    """
    return (lat - radius_m / LAT_M, lat + radius_m / LAT_M,
            lon - radius_m / LON_M, lon + radius_m / LON_M)


def listings_near(conn, lat, lon, *, radius_m=DEFAULT_RADIUS_M,
                  limit=MAX_ROWS) -> dict:
    """Sale listings within `radius_m` of a point, from both live stores.

    Yad2 and ONMAP are unioned because a parcel does not care which board an ad
    sits on. They are *not* de-duplicated against each other here: the combined
    sale view does that over the whole corpus with a fingerprint, and repeating
    a cut-down version of it on 40 rows would produce a different answer on the
    same data - two screens disagreeing is worse than one showing a duplicate
    that is labelled with its source.
    """
    lat_min, lat_max, lon_min, lon_max = _bbox(lat, lon, radius_m)
    rows = []
    queries = (
        ("Yad2", """SELECT token AS id, price, rooms, sqm, property_type, city,
                           neighborhood, street, house_number, floor, lat, lon,
                           image, ad_type, agency, first_seen_at, image_date
                      FROM yad2_listings
                     WHERE delisted_at IS NULL AND lat BETWEEN ? AND ?
                       AND lon BETWEEN ? AND ? AND price > 0"""),
        ("ONMAP", """SELECT id, price, rooms, area_sqm AS sqm, property_type,
                            city, neighborhood, street, house_number, floor,
                            lat, lon, image, NULL AS ad_type, NULL AS agency,
                            first_seen_at, created_at AS image_date
                       FROM onmap_listings
                      WHERE delisted_at IS NULL AND lat BETWEEN ? AND ?
                        AND lon BETWEEN ? AND ? AND price > 0"""),
    )
    for source, sql in queries:
        try:
            cursor = conn.execute(sql, (lat_min, lat_max, lon_min, lon_max))
        except Exception:
            # A store that has never been harvested is an absent source, not a
            # failed parcel lookup.
            continue
        for row in cursor:
            item = dict(row)
            distance = metres(lat, lon, item["lat"], item["lon"])
            if distance > radius_m:
                continue
            item["distance_m"] = round(distance)
            item["source"] = source
            if item.get("sqm") and item.get("price"):
                try:
                    item["price_per_m2"] = round(float(item["price"])
                                                 / float(item["sqm"]))
                except (TypeError, ValueError, ZeroDivisionError):
                    item["price_per_m2"] = None
            rows.append(item)

    rows.sort(key=lambda r: r["distance_m"])
    ppm = [r["price_per_m2"] for r in rows if r.get("price_per_m2")]
    prices = [r["price"] for r in rows if r.get("price")]
    return {
        "radius_m": radius_m,
        "total": len(rows),
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
        "median_price": round(statistics.median(prices)) if prices else None,
        "median_ppm": round(statistics.median(ppm)) if ppm else None,
        "by_source": {s: sum(1 for r in rows if r["source"] == s)
                      for s in ("Yad2", "ONMAP")},
    }


def transactions_near(conn, *, locality=None, street=None,
                      limit=RECENT_DEAL_ROWS) -> dict:
    """Concluded sales around the parcel, by street where possible.

    Two passes, and the difference between them is reported rather than
    smoothed over. A street match is a real neighbourhood claim; a town match
    for "תל אביב-יפו" spans a city of half a million people and is a price
    level, not a comparison. `scope` on every row says which one it is.
    """
    out = {"available": False, "street_rows": [], "locality_rows": [],
           "locality": locality, "street": street}
    if not locality and not street:
        out["note"] = "לא זוהה ישוב לחלקה — אין מפתח לחיפוש עסקאות"
        return out
    try:
        import external_listings_view as external
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def pull(scope_label, *, facets, **kwargs):
        try:
            found = external.transactions(limit=limit, offset=0,
                                          include_facets=facets, **kwargs)
        except Exception as exc:
            out.setdefault("error", f"{type(exc).__name__}: {exc}")
            return [], None
        rows = _dedupe(found.get("rows") or [])
        for row in rows:
            row["scope"] = scope_label
        return rows, found.get("total")

    # Sequential, and measured that way. `transactions` has no index it can use
    # for these filters - it is a full scan of 285,535 rows - and running the
    # two pulls in a ThreadPoolExecutor made the endpoint *slower*, 12.6 s to
    # 18 s: both threads scan the same SQLite file, so they contend for the
    # page cache instead of overlapping, and the row-building loop holds the
    # GIL anyway. Two scans that share one disk do not parallelise.
    #
    # The saving that does work is skipping the facets, which cost a second
    # full scan each (0.89 s a page against 2.32 s with them). The street total
    # is kept because "591 deals on this street" is what tells a reader how
    # much sits behind the forty rows shown; the town total is dropped, because
    # "18,038 deals in Tel Aviv" is a fact about Tel Aviv.
    results = {}
    if street and locality:
        results["street"] = pull("רחוב", facets=True, locality=locality,
                                 street=street)
    if locality:
        results["locality"] = pull("ישוב", facets=False, locality=locality)

    if "street" in results:
        rows, total = results["street"]
        out["street_rows"] = rows
        out["street_total"] = total
        out["street_newest"] = _newest(rows)
        out["available"] = out["available"] or bool(rows)

    if "locality" in results:
        rows, total = results["locality"]
        out["locality_rows"] = rows
        out["locality_total"] = total
        out["locality_newest"] = _newest(rows)
        out["available"] = out["available"] or bool(rows)
        prices = [r["price"] for r in rows if r.get("price")]
        out["median_price"] = round(statistics.median(prices)) if prices else None
        # The dataset carries no area on any row, so a price per m² is not
        # available here and is not approximated. See the module docstring.
        out["note"] = ("מאגר העסקאות אינו כולל שטח או חדרים — מוצגים מחירים "
                       "ותאריכים בלבד, ללא ₪ למ״ר")

    # Coverage is per-street and wildly uneven upstream: the dataset runs to
    # 2024 overall and to 2024 for Tel Aviv, yet its newest deal on דיזנגוף is
    # from 2014. A twelve-year-old price sitting under a heading next to
    # today's asking prices is the single most misleading thing this card could
    # do, so the age of what is actually on screen is stated on the card.
    for scope in ("street", "locality"):
        newest = out.get(f"{scope}_newest")
        if newest and newest[:4].isdigit():
            age = _current_year() - int(newest[:4])
            if age >= 3:
                out[f"{scope}_stale"] = (
                    f"העסקה האחרונה כאן היא מ-{newest[:4]} — כיסוי המאגר "
                    f"אינו אחיד בין רחובות; אל תשווה ישירות למחיר מבוקש היום")
    return out


def _dedupe(rows: list[dict]) -> list[dict]:
    """Drop rows the source repeats verbatim.

    The upstream dataset ships the same deal twice - identical date, street,
    house number and price - which is why its own row count disagrees with what
    the scraper imported (see external_listings_view). Left in, the repeats
    double-weight whichever deals happen to be duplicated and shift the median.
    """
    seen, out = set(), []
    for row in rows:
        key = (row.get("deal_date"), row.get("street"),
               row.get("house_number"), row.get("price"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _newest(rows) -> str | None:
    dates = [str(r.get("deal_date")) for r in rows if r.get("deal_date")]
    return max(dates) if dates else None


def _current_year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


#: Street-type words GovMap prefixes and the transactions dataset never uses.
#:
#: Verified against all 3,790 distinct street names in the dataset: **zero**
#: begin with any of these. The match is `street LIKE '%...%'`, so a needle
#: longer than the stored value can never hit - "שדרות רוטשילד" found nothing
#: while 892 Rothschild deals sat in the table. Stripping is therefore safe in
#: this direction and only in this direction: these words are removed from the
#: *query*, never from the data.
STREET_PREFIXES = ("שדרות", "שד'", "שד", "רחוב", "רח'", "דרך", "סמטת", "סמטה",
                   "שביל", "כיכר", "ככר", "מעלה", "משעול")


def street_of(label) -> str | None:
    """The street name out of a GovMap address label.

    Labels come back as "דיזנגוף 100, תל אביב-יפו" or
    "שדרות רוטשילד 20, תל אביב-יפו": street type, name, house number, comma,
    town. All three of the parts that are not the name have to go, because the
    transactions table stores the bare name - so "דיזנגוף 100" matches nothing
    for having a number on it, and "שדרות רוטשילד" matches nothing for having a
    street type on it.
    """
    text = str(label or "").split(",")[0].strip()
    if not text:
        return None
    parts = text.split()
    while parts and parts[-1].isdigit():
        parts.pop()
    # Only when something is left: a street genuinely called "הדרך" must not be
    # stripped down to nothing.
    if len(parts) > 1 and parts[0] in STREET_PREFIXES:
        parts = parts[1:]
    return " ".join(parts) or None


def resolve_address(query: str, conn=None) -> dict:
    """Address -> gush/helka + a point. National coverage.

    Two steps, because neither alone is enough:

    1. **GovMap's search** turns free text into a point. Measured: for
       "דם המכבים 50" it returns the address with coordinates and **no
       gush/helka at all** - its ADDRESS layer simply does not carry them.
       Treating that as the whole answer is why the first version of this said
       "no parcel found" for a perfectly valid address in Modiin.
    2. **`govmap_client.parcel_at_point`** then asks the national cadastre
       which parcel that point is in. This is the Survey of Israel's
       `PARCEL_ALL` layer through GovMap's own identify endpoint - the one its
       map calls when a user clicks - and it covers the whole country.
       Verified: דם המכבים 50 → **גוש 5642 חלקה 63**, which is the parcel on
       the appraisal for that address.

    The municipal polygons stay as a **second opinion**, not a fallback. They
    are only loaded for Tel Aviv and Ashdod, and where both sources answer they
    do not always agree: for דיזנגוף 100 the national layer says 7091/**7**
    (503 m²) and Tel Aviv's own layer says 7091/**25** (472 m²); for
    שדרות רוטשילד 20 it is 7245/**24** (560 m²) against 7245/**40** (3,283 m²,
    which is a whole block rather than a building plot). The gush agrees in
    every case; the helka does not, because the two registers number and
    aggregate parcels differently.

    That disagreement is **reported, not resolved**. Everything in the parcel
    dossier hangs off this number, so quietly picking one would open a
    confident file on possibly the wrong property. Both are offered, the
    national one first, each labelled with where it came from.

    Candidates are likewise returned rather than reduced to one: "הרצל 5"
    exists in half a dozen towns.
    """
    out = {"query": query, "candidates": []}
    text = (query or "").strip()
    if not text:
        return out
    try:
        import govmap_client
        hits = govmap_client.search(text)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    own_conn = conn is None
    if own_conn:
        import db
        conn = db.get_conn()
    try:
        import municipal_client
        for hit in hits:
            base = {"label": hit.get("label"), "layer": hit.get("layer"),
                    "lat": hit.get("lat"), "lon": hit.get("lon")}
            lat, lon = base["lat"], base["lon"]

            # The search itself sometimes carries a parcel (a gush/helka query
            # rather than an address). That needs no lookup.
            if hit.get("gush") and hit.get("helka"):
                out["candidates"].append(dict(
                    base, gush=str(hit["gush"]), helka=str(hit["helka"]),
                    parcel_source="חיפוש GovMap", usable=True))
                continue

            found = []
            if lat is not None:
                national = govmap_client.parcel_at_point(lat, lon)
                if national:
                    found.append(dict(
                        base, gush=national["gush"], helka=national["helka"],
                        parcel_source="מפ״י — קדסטר ארצי",
                        parcel_area_m2=national.get("area_registered_m2"),
                        parcel_status=national.get("status"), usable=True))
                try:
                    muni = municipal_client.parcel_at_point(conn, lat, lon)
                except Exception:
                    muni = None
                # Only worth offering when it says something different; an
                # identical second opinion is noise in a picker.
                if muni and not any(c["gush"] == str(muni["gush"])
                                    and c["helka"] == str(muni["helka"])
                                    for c in found):
                    found.append(dict(
                        base, gush=str(muni["gush"]), helka=str(muni["helka"]),
                        parcel_source=f"שכבת GIS {muni['city']}",
                        parcel_area_m2=muni.get("area_registered_m2"),
                        usable=True))
            if found:
                if len(found) > 1:
                    for candidate in found:
                        candidate["note"] = ("שני מרשמים מספרים את החלקה אחרת — "
                                             "בדוק איזה מהם מתאים למסמך שבידך")
                out["candidates"].extend(found)
            else:
                # A settlement centroid, or a point the cadastre has no parcel
                # for. The screen has to be able to say which, rather than
                # opening an empty dossier.
                out["candidates"].append(dict(
                    base, gush=None, helka=None, parcel_source=None,
                    usable=False,
                    why=("נמצאה כתובת אבל הקדסטר לא מחזיר חלקה לנקודה הזאת"
                         if lat is not None else "אין קואורדינטות לתוצאה")))
    finally:
        if own_conn:
            conn.close()

    # Parcel-bearing hits first: they are the ones the parcel view can act on.
    out["candidates"].sort(key=lambda c: not c["usable"])
    return out
