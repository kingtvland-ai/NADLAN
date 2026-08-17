"""Asking price cross-referenced against what buyers actually paid.

The gap this fills
------------------
Every "opportunity" figure in PlanWatch so far compares an asking price to
other *asking* prices. That answers "is this cheap for the board" and not "is
this cheap for the market" - and a whole neighbourhood can be optimistically
priced at once. The only independent answer is the concluded-sales register:
285,535 real transactions, of which 79,493 are residential sales since 2020
across 54 cities with 30+ deals each.

Three problems had to be solved before that register can be compared to a
listing, and each one is why a naive join gives a wrong number:

1. **The register has no area and no room count.** Zero rows out of 285,535.
   So there is no ₪/m² on the paid side, and any comparison has to be of price
   *levels* between like-for-like property types - never a made-up ₪/m².

2. **It stops at 2024-09**, roughly two years before today. Comparing a 2026
   asking price to 2023 money understates the seller every time. Every deal is
   therefore re-priced to today with the CBS housing index, using the listing's
   own district series where one exists and the national series otherwise. The
   index runs to 2026-04 monthly, so the adjustment is real and dated.

3. **The register mixes whole buildings and combination deals into residential
   type codes** - a "מגורים" row at ₪316bn is real, just not a flat. The same
   ``RESIDENTIAL_TYPES`` and ``SINGLE_HOME_PRICE_CEILING`` filters the
   transactions view already uses are applied here, or the median moves by
   millions.

Street-level matching was measured and abandoned: only 2.8% of listing streets
resolve to a street in the register, because the two sources format street
names differently. City plus property-type family is what the data actually
supports, and pretending otherwise would produce a precise-looking number from
a sample of one.

What it returns, and what it is not
-----------------------------------
A percentile for the asking price within the adjusted local distribution, an
indicative p25-p75 band, and the gap to the median - each with the number of
comparables and the date range behind it. It is a *price-level* cross-check, not
an appraisal: it cannot know that this particular flat is 40 m² larger or
renovated. Confidence degrades explicitly with the comparable count, and below
``MIN_DEALS`` it declines to answer rather than inventing precision.
"""

from __future__ import annotations

import bisect
import re
import sqlite3
import statistics
import threading
from collections import defaultdict
from datetime import datetime, timezone

import db

#: Only look this far back. Older deals are adjustable in principle but the
#: further back you go the more the *composition* of what sold differs from
#: what is on the board now, and the index cannot correct for that.
LOOKBACK_YEARS = 5

#: Comparable concluded deals required before a verdict is offered. Below this
#: the percentile of a single asking price is noise dressed as analysis.
MIN_DEALS = 30

#: Listing property type -> the register's asset types. The two vocabularies
#: were written by different agencies; this is the join.
#: Types with no residential equivalent (מגרשים, קב' רכישה) are absent on
#: purpose - a plot has no business being compared to flat prices.
TYPE_FAMILIES = {
    "דירה": ("דירה", "דירה בבית קומות", "מגורים"),
    "דירת גן": ("דירת גן",),
    "גג/ פנטהאוז": ("דירת גג", "מיני פנטהאוז"),
    "דופלקס": ("דופלקס", "טריפלקס"),
    "טריפלקס": ("דופלקס", "טריפלקס"),
    "בית פרטי/ קוטג'": ("בית בודד", "חד משפחתי (וילה)", "קוטג' חד משפחתי"),
    "דו משפחתי": ("קוטג' דו משפחתי", "קוטג' טורי"),
}

#: Fallback family when the listing's type is unknown or unmapped: ordinary
#: flats, which are ~64% of both sides.
DEFAULT_FAMILY = ("דירה", "דירה בבית קומות")

#: District series in the CBS index, and the jurisdictions they cover. Built
#: from `plans`, which carries a district for every jurisdiction it knows.
DISTRICT_SERIES = {"ירושלים", "צפון", "חיפה", "מרכז", "תל-אביב", "דרום"}


def _squash(value) -> str:
    """Punctuation-free comparison form.

    PlanWatch says "תל אביב-יפו", the register says "תל אביב -יפו", and Yad2
    says "תל אביב יפו". A plain equality join silently drops the country's
    largest city - which is exactly the failure documented for `compare.py`.
    """
    return re.sub(r"[\s\-־'\"״׳,.]+", "", str(value or "")).strip()


_CACHE: dict = {}
_LOCK = threading.Lock()


def _index_series(conn: sqlite3.Connection) -> dict:
    """{series_name or 'national': {'YYYY-MM': level}} from the CBS index."""
    series: dict = defaultdict(dict)
    for row in conn.execute(
            """SELECT scope, series_name, period, value
                 FROM cbs_price_index
                WHERE period_type='month' AND value IS NOT NULL
                  AND scope IN ('national','district')"""):
        key = "national" if row["scope"] == "national" else row["series_name"]
        series[key][row["period"]] = float(row["value"])
    return dict(series)


def _city_districts(conn: sqlite3.Connection) -> dict:
    """{squashed jurisdiction -> district} from the plans table.

    A jurisdiction occasionally appears under two districts (a boundary body
    filed both ways). The most frequent wins, which is what the index series
    should follow.
    """
    counts: dict = defaultdict(lambda: defaultdict(int))
    for row in conn.execute(
            """SELECT jurisdiction_name, district_name, COUNT(*) n
                 FROM plans
                WHERE jurisdiction_name IS NOT NULL
                  AND district_name IS NOT NULL
                GROUP BY jurisdiction_name, district_name"""):
        counts[_squash(row["jurisdiction_name"])][row["district_name"]] += row["n"]
    return {city: max(options.items(), key=lambda kv: kv[1])[0]
            for city, options in counts.items()}


def _latest_period(levels: dict) -> str | None:
    return max(levels) if levels else None


def _adjust(price: float, deal_date: str, levels: dict,
            latest: str | None) -> float | None:
    """Re-price a past deal into today's money using an index series.

    Returns None when the deal predates the series, rather than assuming no
    change - a silent 1.0 factor would quietly present 2015 money as current.
    """
    if not levels or not latest or not deal_date:
        return None
    period = str(deal_date)[:7]
    base = levels.get(period)
    if base is None:
        # Nearest earlier month, so a gap in the series does not drop the deal.
        earlier = [p for p in levels if p <= period]
        if not earlier:
            return None
        base = levels[max(earlier)]
    if not base:
        return None
    return price * (levels[latest] / base)


def _snapshot():
    """Comparable adjusted deal prices per (city, asset type), cached.

    Cached on the scraper DB's identity: the register only changes when the
    importer runs, and rebuilding this per listing would re-read 80k rows for
    every row on screen.
    """
    from ingestion.feeds import external_listings_view as external

    path = external._path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = None

    cached = _CACHE.get("entry")
    if key is not None and cached and cached[0] == key:
        return cached[1]

    with _LOCK:
        cached = _CACHE.get("entry")
        if key is not None and cached and cached[0] == key:
            return cached[1]

        conn = db.get_conn()
        try:
            series = _index_series(conn)
            districts = _city_districts(conn)
        finally:
            conn.close()

        national = series.get("national", {})
        latest_national = _latest_period(national)

        cutoff = f"{_now().year - LOOKBACK_YEARS}-01-01"
        deals: dict = defaultdict(list)
        dates: dict = defaultdict(list)
        if path.exists():
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro",
                                 uri=True) as tx:
                tx.row_factory = sqlite3.Row
                marks = ",".join("?" for _ in external.RESIDENTIAL_TYPES)
                rows = tx.execute(
                    f"""SELECT city, asset_type, deal_date, price
                          FROM transactions
                         WHERE price > 0 AND price <= ?
                           AND asset_type IN ({marks})
                           AND deal_date >= ?""",
                    (external.SINGLE_HOME_PRICE_CEILING,
                     *external.RESIDENTIAL_TYPES, cutoff)).fetchall()
            for row in rows:
                city = _squash(row["city"])
                district = districts.get(city)
                levels = (series.get(district) if district in DISTRICT_SERIES
                          else None) or national
                latest = _latest_period(levels) or latest_national
                adjusted = _adjust(float(row["price"]), row["deal_date"],
                                   levels, latest)
                if adjusted is None:
                    continue
                deals[(city, row["asset_type"])].append(adjusted)
                dates[city].append(row["deal_date"])

        city_all: dict = defaultdict(list)
        for (city, _), prices in deals.items():
            city_all[city].extend(prices)

        snapshot = {
            "deals": dict(deals),
            # Pooled per-city sample, for the mixed-type fallback.
            "city_all": {c: sorted(v) for c, v in city_all.items()},
            # Memo for _stats(), scoped to this snapshot so it dies with it.
            "stats": {},
            "dates": {c: (min(v), max(v)) for c, v in dates.items()},
            "districts": districts,
            "index_latest": latest_national,
            "index_levels": {"national": national},
            "cutoff": cutoff,
        }
        if key is not None:
            _CACHE["entry"] = (key, snapshot)
        return snapshot


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _family(property_type: str | None) -> tuple:
    return TYPE_FAMILIES.get((property_type or "").strip(), DEFAULT_FAMILY)


def _percentile_of(value: float, sample: list) -> float:
    """Share of the SORTED sample at or below `value`, as a percentage.

    Binary search, not a scan. This is called once per listing on screen
    against a sample of thousands; the linear version made the leads endpoint
    take minutes.
    """
    if not sample:
        return 0.0
    return round(bisect.bisect_right(sample, value) / len(sample) * 100, 1)


def _stats(snapshot: dict, city: str, property_type: str | None):
    """Sorted comparable sample and its quantiles for one (city, type family).

    Takes the snapshot as an argument rather than fetching it. ``_snapshot()``
    calls ``path.stat()`` to decide whether its cache is still valid, and doing
    that once per listing *twice over* cost 17s for a 19,433-row page - the
    work was almost entirely filesystem syscalls, not arithmetic.

    Memoised per snapshot: the quantiles depend only on the register, so
    computing them per listing meant re-sorting thousands of deals for every
    row rendered. Returns (sample, median, p25, p75, type_matched).
    """
    cache = snapshot["stats"]
    family = _family(property_type)
    key = (city, family)
    if key in cache:
        return cache[key]

    sample: list = []
    for asset_type in family:
        sample.extend(snapshot["deals"].get((city, asset_type), []))
    matched = len(sample) >= MIN_DEALS
    if not matched:
        # Fall back to every residential deal in the city rather than refusing
        # outright - but record that the sample is mixed-type, because that is
        # a weaker comparison than like-for-like.
        sample = list(snapshot["city_all"].get(city, ()))

    if len(sample) < MIN_DEALS:
        entry = ((), None, None, None, matched)
    else:
        sample.sort()
        entry = (sample, statistics.median(sample),
                 sample[int(len(sample) * 0.25)],
                 sample[int(len(sample) * 0.75)], matched)
    cache[key] = entry
    return entry


def assess(*, locality, price, property_type=None, snapshot=None) -> dict:
    """Where this asking price sits among what buyers actually paid nearby.

    Always returns a dict. When the evidence is too thin, ``available`` is
    False and ``reason`` says why, because "we cannot tell" is a legitimate and
    useful answer here - far better than a percentile computed from four deals.
    """
    out = {"available": False, "source": "מידע לעם — מאגר עסקאות (רשות המסים)",
           "method": "רמת מחיר מול עסקאות שבוצעו, צמוד למדד הלמ״ס",
           "basis": "price_level"}
    if not price or not locality:
        out["reason"] = "אין מחיר או ישוב"
        return out

    snapshot = snapshot or _snapshot()
    city = _squash(locality)
    sample, median, p25, p75, matched_type = _stats(snapshot, city, property_type)

    if len(sample) < MIN_DEALS:
        out["reason"] = (f"פחות מ-{MIN_DEALS} עסקאות מגורים ב{locality} "
                         f"מאז {snapshot['cutoff'][:4]}")
        out["deals"] = len(sample)
        return out

    price = float(price)
    percentile = _percentile_of(price, sample)
    gap_pct = (price - median) / median * 100 if median else None

    # Wording an agent can repeat to a client without qualifying it further.
    if percentile <= 25:
        verdict = "מתחת לרוב העסקאות באזור"
    elif percentile >= 75:
        verdict = "מעל רוב העסקאות באזור"
    else:
        verdict = "בתוך טווח העסקאות באזור"

    first, last = snapshot["dates"].get(city, (None, None))
    out.update({
        "available": True,
        "deals": len(sample),
        "median_paid": round(median),
        "range_p25": round(p25),
        "range_p75": round(p75),
        "asking": round(price),
        "percentile": percentile,
        "gap_vs_paid_pct": round(gap_pct, 1) if gap_pct is not None else None,
        "verdict": verdict,
        "type_matched": matched_type,
        # Confidence is about the *sample*, not the maths. A like-for-like
        # sample of hundreds is a different claim from a mixed one of thirty.
        "confidence": ("high" if matched_type and len(sample) >= 150
                       else "medium" if len(sample) >= 80 else "low"),
        "deals_from": first, "deals_to": last,
        "adjusted_to": snapshot["index_latest"],
        "note": ("השוואת רמת מחיר בלבד — מאגר העסקאות אינו מפרסם שטח או חדרים, "
                 "ולכן אין ₪/מ״ר ואין התאמה לגודל הנכס. "
                 + ("סוג הנכס הותאם. " if matched_type
                    else "לא נמצאו מספיק עסקאות מאותו סוג נכס; ההשוואה היא "
                         "מול כלל עסקאות המגורים בישוב. ")
                 + f"מחירי העסקאות הוצמדו למדד מחירי הדירות עד {snapshot['index_latest']}."),
    })
    return out


def coverage() -> dict:
    """Which localities have enough concluded deals to be assessed at all."""
    snapshot = _snapshot()
    per_city: dict = defaultdict(int)
    for (city, _), prices in snapshot["deals"].items():
        per_city[city] += len(prices)
    usable = {c: n for c, n in per_city.items() if n >= MIN_DEALS}
    return {
        "cities_with_deals": len(per_city),
        "cities_assessable": len(usable),
        "deals_total": sum(per_city.values()),
        "min_deals": MIN_DEALS,
        "lookback_years": LOOKBACK_YEARS,
        "adjusted_to": snapshot["index_latest"],
        "cutoff": snapshot["cutoff"],
    }
