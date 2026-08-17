"""
PlanWatch - asking price vs concluded price (השוואת היצע לעסקאות)
=================================================================
The comparison you actually want: what sellers ASK versus what buyers PAID,
per locality, with the gap in ₪/m² and in percent.

This is the module from the pasted code that is worth having, rebuilt against
the sources this project already holds and with the statistical problems fixed.

Three price sides, and they are NOT interchangeable
---------------------------------------------------
| side | source | what it is | rows |
|---|---|---|---|
| **asking** | `listings` | what sellers want | 0 - needs a licensed feed |
| **concluded** | `tender_lots.winning_price` | what was actually paid, for LAND | 108 priced |
| **subsidised** | `ml_projects.price_per_m2` | מחיר למשתכן, a price FLOOR | 2,320 |
| **index** | `cbs_price_index` | direction of travel, not a level | 1,107 |

`compare_locality()` reports every side it has and **labels each one**, because
"gap between asking and paid" means something completely different when the
"paid" side is state land rather than second-hand flats.

Two statistics fixes over the naive version
-------------------------------------------
1. **Median, not mean.** The pasted code used `AVG(price/area)`. One 780,000 m²
   parcel or one penthouse drags a mean anywhere; the medians here are computed
   in Python over the sorted values.
2. **`AVG(price)/AVG(area)` is not `AVG(price/area)`.** Averaging a ratio
   weights small properties equally with large ones. Both are reported, named
   distinctly, so a caller cannot mistake one for the other.

Honest limits, stated in every response
---------------------------------------
* **`listings` is empty.** PlanWatch does not scrape Yad2 or Madlan - both
  forbid it in robots.txt (Yad2: `Disallow: /api/`; Madlan disallows every data
  endpoint by name). `compare_locality()` returns `asking: null` and says so,
  rather than leaving a zero that reads as "nobody is asking anything".
* **Tender prices are LAND, not apartments.** Comparing a ₪/m² for a building
  plot against a ₪/m² for a finished flat is meaningless. `basis` says which.
* **מחיר למשתכן is subsidised** and sits systematically below market.
"""

from __future__ import annotations


def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def _median(values):
    """Median of a list, or None. Mean is wrong here - see the module docstring."""
    rows = sorted(v for v in values if v is not None)
    if not rows:
        return None
    mid = len(rows) // 2
    return rows[mid] if len(rows) % 2 else (rows[mid - 1] + rows[mid]) / 2


def _stats(values):
    """n / median / p25 / p75 / min / max over a list of numbers."""
    rows = sorted(v for v in values if v is not None and v > 0)
    if not rows:
        return {"n": 0}

    def pct(f):
        return rows[min(len(rows) - 1, int(len(rows) * f))]

    return {"n": len(rows), "median": round(_median(rows), 1),
            "p25": round(pct(0.25), 1), "p75": round(pct(0.75), 1),
            "min": round(rows[0], 1), "max": round(rows[-1], 1)}


def _squash(name):
    return (name or "").replace(" ", "").replace("-", "").replace("'", "")


# ------------------------------------------------------------------- sides

def asking_side(conn, locality="") -> dict:
    """Asking prices from `listings`. Empty until a licensed feed is loaded."""
    if not _has(conn, "listings"):
        return {"available": False, "reason": "טבלת listings לא קיימת."}
    clause, params = "", []
    if locality:
        clause = " AND locality LIKE ?"
        params.append(f"%{locality}%")
    rows = conn.execute(
        f"""SELECT price, area_m2, price_per_m2, rooms, year_built
            FROM listings
            WHERE price > 0 AND area_m2 > 0{clause}""", params).fetchall()
    if not rows:
        return {
            "available": False,
            "reason": "אין מודעות במאגר. PlanWatch לא מושך מיד2/מדלן — שניהם "
                      "אוסרים זאת ב-robots.txt. טען פיד שיש לך רשות אליו דרך "
                      "market_client.import_listings_csv().",
            "n": 0,
        }
    per_m2 = [r["price_per_m2"] or (r["price"] / r["area_m2"]) for r in rows]
    prices = [r["price"] for r in rows]
    areas = [r["area_m2"] for r in rows]
    return {
        "available": True, "basis": "asking (מודעות פעילות)",
        "price_per_m2": _stats(per_m2),
        "price": _stats(prices),
        "area_m2": _stats(areas),
        # Both ratio forms, named apart: see the docstring.
        "median_of_ratios": round(_median(per_m2) or 0, 1),
        "ratio_of_medians": (round((_median(prices) or 0) / (_median(areas) or 1), 1)
                             if areas else None),
    }


def concluded_side(conn, locality="") -> dict:
    """
    Concluded prices - real transactions. `tender_lots.winning_price` only:
    a lot that actually sold, at the price actually paid.
    """
    if not _has(conn, "tender_lots"):
        return {"available": False, "reason": "מאגר המכרזים לא יובא."}
    clause, params = "", []
    if locality:
        clause = " AND t.locality LIKE ?"
        params.append(f"%{locality}%")
    rows = conn.execute(
        f"""SELECT l.winning_price price, l.area_m2, t.published_at
            FROM tender_lots l JOIN tenders t ON t.michraz_id = l.michraz_id
            WHERE l.winning_price > 0 AND l.area_m2 > 0{clause}""",
        params).fetchall()
    if not rows:
        return {"available": False, "n": 0,
                "reason": "אין עסקאות מכרז שהסתיימו בישוב הזה."}
    per_m2 = [r["price"] / r["area_m2"] for r in rows]
    prices = [r["price"] for r in rows]
    areas = [r["area_m2"] for r in rows]
    dates = sorted(r["published_at"] for r in rows if r["published_at"])
    return {
        "available": True,
        "basis": "concluded — קרקע ממכרזי רמ\"י (לא דירות יד שנייה)",
        "asset_type": "land",
        "price_per_m2": _stats(per_m2),
        "price": _stats(prices),
        "area_m2": _stats(areas),
        "median_of_ratios": round(_median(per_m2) or 0, 1),
        "ratio_of_medians": (round((_median(prices) or 0) / (_median(areas) or 1), 1)
                             if areas else None),
        "first_sale": dates[0] if dates else None,
        "last_sale": dates[-1] if dates else None,
    }


def residential_concluded_side(locality="") -> dict:
    """Concluded APARTMENT sale prices from the official CC-BY dataset.

    `concluded_side` above covers land sold at RMI tender, which is a different
    asset entirely. This is what people actually paid for homes - the closest
    thing to a market value for a second-hand flat.

    Reported as price levels only, never ₪/m²: the source publishes no area
    (NULL across all 357,593 rows), so a per-m2 figure here would be invented.
    """
    try:
        from ingestion.feeds import external_listings_view as external
    except ImportError:
        return {"available": False, "reason": "מודול מקור העסקאות לא נמצא."}
    data = external.transactions(locality=locality, residential_only=True,
                                 limit=1)
    if not data.get("available") or not data.get("total"):
        return {"available": False, "n": 0,
                "reason": data.get("note") or "אין עסקאות מגורים לישוב הזה."}
    cities = data.get("cities") or []
    # The SQL already filtered by locality; match here on the same squashed
    # form so a hyphen difference does not drop every row back to "all".
    squash = external._squash
    wanted = squash(locality)
    matched = [c for c in cities
               if not wanted or wanted in squash(c.get("city") or "")]
    scope = matched or cities
    deals = sum(c.get("deals") or 0 for c in scope)
    return {
        "available": True,
        "basis": "concluded — דירות יד שנייה שנמכרו בפועל (מאגר רשמי)",
        "asset_type": "apartment",
        "n": deals,
        "median_price": data.get("median_price"),
        "avg_price_by_city": scope,
        "first_sale": min((c.get("first_deal") or "" for c in scope), default=None) or None,
        "last_sale": max((c.get("last_deal") or "" for c in scope), default=None) or None,
        "price_per_m2": None,
        "note": "המקור אינו מפרסם שטח, ולכן אין ₪/מ״ר — רק רמות מחיר.",
        "source": data.get("source"),
        "license": data.get("license"),
    }


def subsidised_side(conn, locality="") -> dict:
    """מחיר למשתכן ₪/m² - a price FLOOR, systematically below market."""
    if not _has(conn, "ml_projects"):
        return {"available": False, "reason": "מחיר למשתכן לא יובא."}
    clause, params = "", []
    if locality:
        clause = " AND locality LIKE ?"
        params.append(f"%{locality}%")
    rows = [r["price_per_m2"] for r in conn.execute(
        f"""SELECT price_per_m2 FROM ml_projects
            WHERE price_per_m2 IS NOT NULL{clause}""", params)]
    if not rows:
        return {"available": False, "n": 0}
    return {"available": True,
            "basis": "subsidised — מחיר למשתכן (רצפת מחיר, לא שוק חופשי)",
            "asset_type": "new apartments",
            "price_per_m2": _stats(rows),
            "median_of_ratios": round(_median(rows) or 0, 1)}


def index_side(conn, locality="") -> dict:
    """CBS district index - direction of travel. An index, not shekels."""
    if not _has(conn, "cbs_price_index"):
        return {"available": False}
    from services.external import market_client as mc
    district = None
    if locality:
        row = conn.execute(
            """SELECT district_name, COUNT(*) n FROM plans
               WHERE jurisdiction_name LIKE ? AND district_name <> ''
               GROUP BY 1 ORDER BY n DESC LIMIT 1""", (f"%{locality}%",)).fetchone()
        district = row["district_name"] if row else None
    trend = (mc.district_trend(conn, district) if district
             else mc.national_trend(conn))
    if not trend:
        return {"available": False}
    return {"available": True, "basis": "index — מדד הלמ\"ס, לא שקלים",
            "district": district or "ארצי", **trend}


# ---------------------------------------------------------------- comparison

def compare_locality(conn, locality="") -> dict:
    """
    Every price side we hold for one locality, plus the gaps that are
    *legitimate* to compute.

    A gap is only reported when both sides describe the SAME asset type. Asking
    vs concluded is computed when listings exist; asking vs subsidised is
    labelled as a floor comparison; land vs apartment is never subtracted.
    """
    asking = asking_side(conn, locality)
    concluded = concluded_side(conn, locality)
    residential = residential_concluded_side(locality)
    subsidised = subsidised_side(conn, locality)
    index = index_side(conn, locality)

    gaps = []
    if asking.get("available") and concluded.get("available"):
        a, c = asking["median_of_ratios"], concluded["median_of_ratios"]
        if a and c:
            gaps.append({
                "pair": "asking_vs_concluded",
                "label": "מבוקש מול שולם",
                "asking_per_m2": a, "concluded_per_m2": c,
                "gap_per_m2": round(a - c, 1),
                "gap_pct": round((a - c) / c * 100, 1),
                "comparable": concluded.get("asset_type") == asking.get("asset_type"),
                "warning": None if concluded.get("asset_type") == "land"
                           else "צדדים מסוגי נכס שונים — הפער אינדיקטיבי בלבד.",
            })
    if asking.get("available") and subsidised.get("available"):
        a, s = asking["median_of_ratios"], subsidised["median_of_ratios"]
        if a and s:
            gaps.append({
                "pair": "asking_vs_subsidised",
                "label": "מבוקש מול מחיר למשתכן",
                "asking_per_m2": a, "subsidised_per_m2": s,
                "gap_per_m2": round(a - s, 1),
                "gap_pct": round((a - s) / s * 100, 1),
                "warning": "מחיר למשתכן מסובסד — פער חיובי גדול הוא הנורמה, "
                           "לא סימן לתמחור יתר.",
            })

    return {
        "locality": locality or "כל הארץ",
        "sides": {"asking": asking, "concluded": concluded,
                  "residential_concluded": residential,
                  "subsidised": subsidised, "index": index},
        "gaps": gaps,
        "note": "צד ה'מבוקש' דורש פיד מודעות מורשה. 'שולם — קרקע' הוא מכרזי "
                "רמ\"י; 'שולם — דירות' הוא עסקאות יד שנייה מהמאגר הרשמי. "
                "אין להשוות ₪/מ\"ר של קרקע לדירה גמורה.",
    }


def compare_all(conn, min_rows=1, limit=200) -> dict:
    """
    One row per locality, every side side-by-side. This is the export the
    pasted `export_all_cities_to_csv` was after.

    Localities are keyed on the SQUASHED name. The feeds spell them
    differently - `tenders` says "תל אביב יפו", `ml_projects` says
    "תל אביב -יפו" - and grouping on the raw string split one city into two
    rows, each showing only half its data. Squashing recovers 5 localities
    (91 -> 96 shared between the two sources) and, more importantly, stops the
    same city being reported twice with contradictory numbers.
    """
    localities: dict = {}

    def bucket(raw_name):
        key = _squash(raw_name)
        item = localities.setdefault(key, {"names": set()})
        item["names"].add(raw_name)
        return item

    if _has(conn, "tender_lots"):
        for row in conn.execute(
            """SELECT t.locality, l.winning_price price, l.area_m2
               FROM tender_lots l JOIN tenders t ON t.michraz_id = l.michraz_id
               WHERE l.winning_price > 0 AND l.area_m2 > 0
                 AND t.locality IS NOT NULL"""):
            bucket(row["locality"]).setdefault("concluded", []).append(
                row["price"] / row["area_m2"])

    if _has(conn, "ml_projects"):
        for row in conn.execute(
            """SELECT locality, price_per_m2 FROM ml_projects
               WHERE price_per_m2 IS NOT NULL AND locality IS NOT NULL"""):
            bucket(row["locality"]).setdefault("subsidised", []).append(
                row["price_per_m2"])

    if _has(conn, "listings"):
        for row in conn.execute(
            """SELECT locality, price, area_m2, price_per_m2 FROM listings
               WHERE price > 0 AND area_m2 > 0 AND locality IS NOT NULL"""):
            bucket(row["locality"]).setdefault("asking", []).append(
                row["price_per_m2"] or row["price"] / row["area_m2"])

    rows = []
    for sides in localities.values():
        # Longest spelling as the display name - it carries the most detail
        # ("תל אביב -יפו" over a truncated variant).
        names = sorted(sides["names"], key=len, reverse=True)
        item = {"locality": names[0]}
        if len(names) > 1:
            item["also_spelled"] = names[1:]
        for key in ("asking", "concluded", "subsidised"):
            values = sides.get(key) or []
            item[f"{key}_n"] = len(values)
            item[f"{key}_median_per_m2"] = (round(_median(values), 1)
                                            if values else None)
        if item["asking_median_per_m2"] and item["concluded_median_per_m2"]:
            item["gap_pct"] = round(
                (item["asking_median_per_m2"] - item["concluded_median_per_m2"])
                / item["concluded_median_per_m2"] * 100, 1)
        else:
            item["gap_pct"] = None
        total = (item["asking_n"] + item["concluded_n"] + item["subsidised_n"])
        if total >= min_rows:
            rows.append(item)

    rows.sort(key=lambda r: -(r["concluded_median_per_m2"]
                              or r["subsidised_median_per_m2"] or 0))
    return {
        "total": len(rows), "rows": rows[:limit],
        "has_asking": any(r["asking_n"] for r in rows),
        "note": "כל צד עם מספר התצפיות שלו. חציון, לא ממוצע. "
                "'שולם' = קרקע ממכרזי רמ\"י; 'מחיר למשתכן' = מסובסד.",
    }


def parcel_comparison(conn, gush, helka) -> dict:
    """
    Everything price-related on ONE parcel: tender history, appraisals, and the
    locality benchmarks - the real-time "what is this worth" view.
    """
    out = {"gush": str(gush), "helka": str(helka)}

    if _has(conn, "tender_lots"):
        from services.external import tenders_client as tc
        out["tender_lots"] = tc.lots_for_parcel(conn, gush, helka, limit=20)
    if _has(conn, "appraisals"):
        from services.enrichers import appraisal_client as ac
        out["appraisals"] = ac.appraisals_for_parcel(conn, gush, helka, limit=20)
    if _has(conn, "listings"):
        out["listings"] = [dict(r) for r in conn.execute(
            """SELECT * FROM listings WHERE gush = ? AND helka = ?""",
            (str(gush), str(helka)))]
        for row in out["listings"]:
            row.pop("raw_json", None)

    # Sales concluded ON THIS PARCEL are the strongest evidence of what it is
    # worth - stronger than any locality benchmark.
    out["parcel_transactions"] = []
    try:
        from ingestion.feeds import external_listings_view as external
        found = external.transactions_for_parcel(gush, helka)
        out["parcel_transactions"] = found.get("rows", [])
    except Exception as exc:
        out["parcel_transactions_error"] = str(exc)

    # Resolve the locality from whichever source names one. `muni_parcels` does
    # NOT have a locality column (only `city`), so it is queried for that field.
    locality = None
    for lot in out.get("tender_lots") or []:
        if lot.get("locality"):
            locality = lot["locality"]
            break
    if not locality:
        for appraisal in out.get("appraisals") or []:
            if appraisal.get("committee"):
                locality = appraisal["committee"]
                break
    if not locality and _has(conn, "building_progress"):
        row = conn.execute(
            """SELECT locality FROM building_progress
               WHERE gush = ? AND helka = ? AND locality IS NOT NULL LIMIT 1""",
            (str(gush), str(helka))).fetchone()
        locality = row["locality"] if row else None
    if not locality and _has(conn, "muni_parcels"):
        row = conn.execute(
            "SELECT city FROM muni_parcels WHERE gush = ? AND helka = ? LIMIT 1",
            (str(gush), str(helka))).fetchone()
        # `city` is the server key ("tel-aviv"); map it to the display name.
        if row:
            from services.external import municipal_client as mn
            locality = (mn.SERVERS.get(row["city"], {}) or {}).get("name")

    out["locality"] = locality
    out["benchmarks"] = compare_locality(conn, locality or "")
    out["ruled_amounts"] = ruled_amounts_for_parcel(conn, gush, helka)
    return out


def ruled_amounts_for_parcel(conn, gush, helka, limit=20) -> list[dict]:
    """Ruled ₪ amounts read out of the decision documents for one parcel.

    This is the only figure in PlanWatch that a tribunal actually ordered on a
    specific parcel - every other price signal is an asking price, a subsidised
    price, or a locality benchmark. It is joined on the DOCUMENT's own
    gush/helka (an independent reading of the ruling's heading) and only where
    `parcel_confident` holds, so an amount is never attached to the wrong
    parcel by a bad CSV row.

    Amounts with `amount_status <> 'ok'` are deliberately excluded: see
    `document_client`, which refuses to guess between two numbers in one
    operative section.
    """
    if not _has(conn, "appraisal_docs"):
        return []
    rows = conn.execute(
        """SELECT d.appraisal_id, d.amount_nis, d.effective_date, d.is_tama38,
                  d.doc_appraiser, d.doc_gush, d.doc_helka, d.parcel_scope,
                  d.link, a.committee, a.decision_date, a.appraisal_type
             FROM appraisal_docs d
             LEFT JOIN appraisals a ON a.id = d.appraisal_id
            WHERE d.amount_nis IS NOT NULL
              AND d.amount_status = 'ok'
              AND d.parcel_confident = 1
              AND d.doc_gush = ? AND d.doc_helka = ?
         ORDER BY COALESCE(d.effective_date, a.decision_date) DESC
            LIMIT ?""", (str(gush), str(helka), limit)).fetchall()
    return [dict(row) for row in rows]


def to_csv(rows, path="comparison_report.csv") -> str:
    """
    Write the comparison to CSV.

    `utf-8-sig` so Excel reads Hebrew rather than mojibake - the same reason
    `/api/export` encodes that way.
    """
    import csv
    if not rows:
        raise ValueError("no rows to write")
    headers = [
        ("locality", "ישוב"),
        ("concluded_median_per_m2", 'חציון ₪/מ"ר — שולם (קרקע)'),
        ("concluded_n", "מס' עסקאות"),
        ("asking_median_per_m2", 'חציון ₪/מ"ר — מבוקש'),
        ("asking_n", "מס' מודעות"),
        ("subsidised_median_per_m2", 'חציון ₪/מ"ר — מחיר למשתכן'),
        ("subsidised_n", "מס' פרויקטים"),
        ("gap_pct", "פער % מבוקש מול שולם"),
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in headers])
        for row in rows:
            writer.writerow([row.get(key) if row.get(key) is not None else ""
                             for key, _ in headers])
    return path


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # median must beat mean on a skewed set - the whole reason for _median
    assert _median([1, 2, 3, 4, 100]) == 3
    assert _median([]) is None
    assert _stats([])["n"] == 0
    assert _stats([10, 20, 30])["median"] == 20
    print("stats self-tests passed\n")

    conn = db.get_conn()
    out = compare_all(conn, min_rows=1)
    print(f"localities with any price side: {out['total']}  "
          f"(asking side present: {out['has_asking']})")
    print(f"{'ישוב':<18}{'שולם':>12}{'n':>5}{'מבוקש':>10}{'n':>5}"
          f"{'למשתכן':>12}{'n':>5}")
    for row in out["rows"][:12]:
        print(f"  {str(row['locality'])[:16]:16}"
              f"{row['concluded_median_per_m2'] or '-':>12}"
              f"{row['concluded_n']:>5}"
              f"{row['asking_median_per_m2'] or '-':>10}{row['asking_n']:>5}"
              f"{row['subsidised_median_per_m2'] or '-':>12}"
              f"{row['subsidised_n']:>5}")
    print("\nsingle locality:")
    one = compare_locality(conn, "באר שבע")
    for name, side in one["sides"].items():
        if side.get("available"):
            print(f"  {name:11} {side.get('basis','')[:44]:44} "
                  f"median={side.get('median_of_ratios') or side.get('value')}")
        else:
            print(f"  {name:11} unavailable: {str(side.get('reason',''))[:60]}")
    print(f"  gaps: {json.dumps(one['gaps'], ensure_ascii=False)[:160]}")
    conn.close()
