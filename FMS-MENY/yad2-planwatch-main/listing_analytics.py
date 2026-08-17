"""Is this asking price good for *this* area - and is it a buy or a sell?

Why the existing benchmark was not enough
----------------------------------------
Every sale row was scored against one number: the median ₪/m² of its
neighbourhood, or of its city when the neighbourhood was thin. That comparison
has a bias big enough to invert the answer, because **₪/m² falls steeply with
size**. Measured over 56,768 plausible Yad2 rows on 2026-08-09:

    under 60 m²   30,896 ₪/m²        120-150 m²   20,075 ₪/m²
    60-80 m²      22,928 ₪/m²        150-200 m²   19,267 ₪/m²
    80-100 m²     22,940 ₪/m²        over 200 m²  14,188 ₪/m²
    100-120 m²    21,804 ₪/m²

A 2.2x spread. A neighbourhood median is dominated by ordinary flats, so a
250 m² house measured against it looks 40% underpriced for being large, and a
45 m² studio looks overpriced for being small. The old score handed the top of
the list to whichever listings happened to be biggest.

Size bands were the first attempt and were not enough. Bucketing a
*continuous* relationship leaves a slope inside every bucket: with bands, the
700-1000 m2 group still read ~18% below its own peers, and an unbounded top
band quietly held a 4,500,000 m2 record next to a 320 m2 house.

So size is modelled instead of bucketed. `log(price per m2)` is regressed on
`log(area)` to get an elasticity - **-0.34 nationally**, meaning doubling the
floor area moves ₪/m² by -21% - and every listing is restated as what it would
cost per m2 at 100 m2. That adjusted figure is what gets compared. The
elasticity is fitted per city where the sample allows, because it genuinely
differs: -0.07 in Tel Aviv-Yafo against -0.31 in Netanya.

The comparison itself is then made against **peers**: same neighbourhood, same
room count, falling back one step at a time until the sample is deep enough to
mean something, and always reporting which level was used and how many
listings backed it.

What is measured, and what is deliberately absent
-------------------------------------------------
Present: the gap to peer pricing in percent and in shekels, where the asking
price sits in the local distribution, how long the ad has been up, whether the
seller has already cut, and how much competing supply sits in the same peer
group.

Absent on purpose: **absorption**. Whether listings in an area actually sell
needs `delisted_at`, and today every stored row has `delisted_at IS NULL` -
there has been one harvest, so nothing has yet been observed leaving the board.
A sell-side score that pretended to know demand would be inventing its most
important input. It is omitted, its weight is redistributed, and `confidence`
falls accordingly.

Three rules carried over from `opportunity.py`, for the same reasons
--------------------------------------------------------------------
1. A score always ships with its `components`. A number nobody can audit is
   worse than no number.
2. A component with no data is **omitted and the weights renormalised** - never
   scored zero. Otherwise "this area has no comparable listings" reads as
   "this place is worthless", and the thin, old neighbourhoods the tool exists
   to surface would be buried.
3. Rank by `score x confidence`, never by `score`. A perfect score from one
   input is not a better lead than a good score from six.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone

#: Outside this, the number is not a home's floor area - it is a land parcel, a
#: units mix-up or a typo. Such rows are shown (nothing is hidden) but they are
#: neither used to build a benchmark nor given a gap against one: comparing
#: 7,000,000 m2 to any median produces a spectacular fake bargain, which is
#: precisely how a broken row reaches the top of a buy list.
MIN_PLAUSIBLE_AREA = 15
MAX_PLAUSIBLE_AREA = 1000

#: Every listing is restated as what it would cost per m2 at this size, so a
#: studio and a villa can be compared at all. 100 m2 is near the median of the
#: stock, which keeps the adjustment small for most rows.
REFERENCE_AREA = 100.0

#: Rows a city needs before its own size elasticity is trusted; below it the
#: national figure is used. Under a few hundred points the fit is driven by the
#: handful of largest properties.
MIN_CITY_FIT = 200

#: How far a peer's size may differ and still be a peer: half to double this
#: listing's area. Wide enough to keep a usable sample in a thin neighbourhood,
#: narrow enough that a 45 m² studio is never benchmarked against a villa.
SIZE_WINDOW = 2.0

#: Past this distance from like-for-like peers, the row is far more likely to
#: be broken than cheap. A genuine 60%-under-market flat does not sit unsold on
#: a public board; what does sit there is a part-share ("1/4 חלק"), a floor area
#: entered as a plot area, or a mislabelled type. Such rows are still shown with
#: their gap - hiding them would hide real listings too - but the buy score
#: omits the value component, so a broken row cannot lead the list. This is the
#: same 50% rule `_annotate_live_rows` already applies to the older discount,
#: tightened slightly because this gap is like-for-like and so narrower.
IMPLAUSIBLE_GAP_PCT = 45.0

#: Peers needed before a comparison is offered at all. Below this the median is
#: one or two ads, and "20% below the area" is noise with a decimal point.
MIN_PEERS = 5

#: A listing older than this is not "on the market a while", it is a data
#: artefact - a photo URL date that never updated, or an ad nobody withdrew.
MAX_PLAUSIBLE_DOM_DAYS = 730


def plausible_area(area) -> float | None:
    """The listing's floor area, or None when the number cannot be one."""
    try:
        value = float(area)
    except (TypeError, ValueError):
        return None
    return value if MIN_PLAUSIBLE_AREA <= value <= MAX_PLAUSIBLE_AREA else None


def _rooms_key(rooms):
    """Rooms rounded to the half, which is how they are advertised."""
    try:
        return round(float(rooms) * 2) / 2
    except (TypeError, ValueError):
        return None


def _norm(value) -> str:
    return str(value or "").strip().lower()


def fit_elasticity(points) -> float | None:
    """Least squares slope of log(price per m2) on log(area).

    Size bands were tried first and are not enough: bucketing a *continuous*
    relationship leaves a slope inside every bucket, and the widest ones stayed
    badly skewed (700-1000 m2 read ~18% below its own peers). A single
    elasticity describes the whole curve instead of stepping through it.
    """
    if len(points) < 2:
        return None
    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    variance = sum((x - mean_x) ** 2 for x, _ in points)
    if not variance:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / variance
    # A positive slope would mean bigger homes cost *more* per m2, which no
    # market does; it means the sample is too odd to fit. Clamped to a range
    # wide enough for every city measured (-0.07 Tel Aviv .. -0.34 national).
    return max(-0.75, min(-0.02, slope))


def property_family(value) -> str:
    """The listing's type, normalised, and never merged with another type.

    Type belongs in the key, not in the fallback chain. The medians are not
    close: flats sit at 21,746 ₪/m², private houses at 13,300 and building
    plots at 6,191. Left out of the key, the plots dragged the benchmark down
    and then read as spectacular bargains against it - the top of the buy list
    was 900 m² "homes" at 390,000 ₪, which are land, not homes.

    So every level below carries the type and none of them falls back across
    it. A plot is compared to plots, a cottage to cottages, and a type with too
    few peers gets no comparison at all - which is the honest answer, and the
    same rule `valuation.TYPE_FAMILIES` already applies for the same reason.
    """
    return re.sub(r"\s+", " ", str(value or "").strip()) or "לא צוין"


#: Peer groups from most to least specific. Size is *not* one of them - that is
#: handled by the elasticity adjustment plus a size window - but the property
#: type is in every one of them.
_LEVELS = (
    ("שכונה · סוג · חדרים",
     lambda r: ("nbhd_type_rooms", _norm(r.get("city")), _norm(r.get("neighborhood")),
                property_family(r.get("property_type")), _rooms_key(r.get("rooms")))),
    ("שכונה · סוג",
     lambda r: ("nbhd_type", _norm(r.get("city")), _norm(r.get("neighborhood")),
                property_family(r.get("property_type")))),
    ("עיר · סוג · חדרים",
     lambda r: ("city_type_rooms", _norm(r.get("city")),
                property_family(r.get("property_type")), _rooms_key(r.get("rooms")))),
    ("עיר · סוג",
     lambda r: ("city_type", _norm(r.get("city")),
                property_family(r.get("property_type")))),
)


def _complete(key) -> bool:
    """A key with a missing part groups unrelated listings together."""
    return all(part not in (None, "", ()) for part in key[1:])


def _usable(row) -> bool:
    """Fit to be part of a benchmark: real area, real price, not flagged."""
    return bool(plausible_area(row.get("sqm")) and row.get("price_per_m2")
                and row.get("data_quality") in (None, "תקין"))


class PeerIndex:
    """Size-adjusted ₪/m² distributions for every peer group.

    Built from *all* rows, not the filtered page: narrowing to "flats under
    2M in Holon" and then computing the benchmark from the survivors would move
    the benchmark with the filter, and every gap on screen would change meaning
    as the user typed.
    """

    def __init__(self, rows):
        # One elasticity per city where the sample supports it, national
        # otherwise. It genuinely differs: -0.07 in Tel Aviv-Yafo against
        # -0.31 in Netanya, so a single national curve would over-correct the
        # cities where size matters least.
        national_points, city_points = [], defaultdict(list)
        for row in rows:
            if not _usable(row):
                continue
            point = (math.log(plausible_area(row["sqm"])),
                     math.log(float(row["price_per_m2"])))
            national_points.append(point)
            city_points[_norm(row.get("city"))].append(point)
        self.national = fit_elasticity(national_points) or -0.30
        self.city_elasticity = {
            city: fit_elasticity(points)
            for city, points in city_points.items() if len(points) >= MIN_CITY_FIT}
        self.city_elasticity = {c: b for c, b in self.city_elasticity.items() if b}

        # Each group keeps (area, adjusted ₪/m²) sorted by area, so a listing
        # can be compared against peers of roughly its own size *within* the
        # group. One log-linear elasticity straightens the middle of the curve
        # but not its ends - measured, the 60-200 m² stock came out perfectly
        # neutral while 500-1000 m² still read +12.5%. The curve is steeper for
        # small homes than for large ones, and a single slope cannot be both.
        self.groups: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
        for row in rows:
            if not _usable(row):
                continue
            adjusted = self.adjust(row)
            if adjusted is None:
                continue
            area = plausible_area(row["sqm"])
            for _, build in _LEVELS:
                key = build(row)
                if _complete(key):
                    self.groups[key].append((area, adjusted))
        for values in self.groups.values():
            values.sort()

    def elasticity(self, city) -> float:
        return self.city_elasticity.get(_norm(city), self.national)

    def adjust(self, row) -> float | None:
        """This listing's ₪/m² restated at REFERENCE_AREA.

        Removes the size effect so the number can be compared to any other
        home in the area regardless of how big it is.
        """
        area = plausible_area(row.get("sqm"))
        ppm = row.get("price_per_m2")
        if not area or not ppm:
            return None
        beta = self.elasticity(row.get("city"))
        return float(ppm) * (area / REFERENCE_AREA) ** (-beta)

    def lookup(self, row):
        """(label, sample) for the most specific group deep enough to use.

        `sample` is narrowed to peers within SIZE_WINDOW of this listing's area
        whenever that window still holds MIN_PEERS; otherwise the whole group
        is used and the label says so, because a comparison against homes of a
        very different size is still worth making - it just deserves to be
        described accurately.
        """
        area = plausible_area(row.get("sqm"))
        for label, build in _LEVELS:
            key = build(row)
            if not _complete(key):
                continue
            entries = self.groups.get(key)
            if not entries or len(entries) < MIN_PEERS:
                continue
            if area:
                low = bisect_left(entries, (area / SIZE_WINDOW, float("-inf")))
                high = bisect_right(entries, (area * SIZE_WINDOW, float("inf")))
                window = entries[low:high]
                if len(window) >= MIN_PEERS:
                    larger = sum(1 for a, _ in window if a > area)
                    return (f"{label} · גודל דומה",
                            sorted(v for _, v in window), larger)
            return label, sorted(v for _, v in entries), None
        return None, None, None


def _percentile_of(value: float, sample: list) -> float:
    """Where `value` sits in an already-sorted sample, 0-100."""
    if not sample:
        return 50.0
    below = sum(1 for item in sample if item < value)
    equal = sum(1 for item in sample if item == value)
    return round((below + equal / 2) / len(sample) * 100, 1)


def _days_since(value) -> int | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat,):
        try:
            moment = parse(text)
        except (ValueError, TypeError):
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - moment).days
        return days if 0 <= days <= MAX_PLAUSIBLE_DOM_DAYS else None
    return None


def days_on_market(row) -> tuple[int | None, str]:
    """How long the ad has been up, and what that number is based on.

    The basis matters and is returned with the number. ONMAP publishes a real
    `created_at`. Yad2 publishes no date at all, so the earliest photo URL date
    is used as a lower bound - the ad is *at least* this old. Falling back to
    `first_seen_at` measures how long *we* have been watching, which for a
    single harvest is days regardless of how long the ad has run.
    """
    days = _days_since(row.get("created_at"))
    if days is not None:
        return days, "published"
    days = _days_since(row.get("image_date"))
    if days is not None:
        return days, "photo"
    return _days_since(row.get("first_seen_at")), "seen"


def price_change(row, history: dict, deflate=None) -> dict | None:
    """The seller's own price movement, from what the board said before.

    `deflate` turns the nominal move into a real one. It is called with the
    month of the first observation and returns the multiplier that restates it
    in today's money - see `market_client.deflator`, which chains the CBS
    monthly changes because the index is rebased seventeen times over its life
    and its levels cannot be divided.

    The correction is small and it is not cosmetic. A seller who has held the
    same asking price for eighteen months has, in real terms, cut it by about
    2.4%, and `price_cut` is scored on exactly this number: nominally that
    seller looks immovable, which is the opposite of what the figure shows.
    """
    observations = history.get(str(row.get("token") or "")) or []
    prices = [p for _, p in observations if p]
    if len(prices) < 2:
        return None
    first, last = float(prices[0]), float(prices[-1])
    if not first:
        return None
    out = {"from": first, "to": last,
           "nominal_pct": round((last - first) / first * 100, 1)}
    factor = None
    if deflate is not None:
        when = observations[0][0]
        factor = deflate(str(when)[:7]) if when else None
    if factor:
        # The first asking price, restated in the money of today, is what the
        # current asking price has to be compared against.
        restated = first * factor
        out["pct"] = round((last - restated) / restated * 100, 1)
        out["real"] = True
    else:
        out["pct"] = out["nominal_pct"]
        out["real"] = False
    if out["pct"] == 0 and out["nominal_pct"] == 0:
        return None
    return out


#: Peers at which a comparison is considered fully evidenced. Below it the
#: verdict still stands, it just carries less weight in the ranking.
EVIDENCE_PEERS = 25


def _evidence(peer_count) -> float:
    """How much a comparison's depth is worth, 0.4 to 1.0.

    Never zero: a five-peer comparison is thin, not worthless, and zeroing it
    would drop exactly the sparse neighbourhoods this tool exists to surface.
    """
    if not peer_count:
        return 1.0
    return round(0.4 + 0.6 * min(1.0, peer_count / EVIDENCE_PEERS), 2)


#: Added units *per dunam* at which planning upside is scored full. Density, not
#: the headline unit count: a 95,075-unit city master plan spread over 34,743
#: dunam changes nothing about one flat, while 25 units per dunam rebuilds the
#: block it covers. See listing_planning for the measured distribution.
PLANNING_FULL_DENSITY = 25.0


def _planning_score(row):
    """Approved housing on this listing's own ground, 0-100, or None.

    Two things multiplied: how many units the plans covering this point add,
    and how settled the most advanced of them is. Both matter and neither
    substitutes for the other - 4,000 units at "threshold conditions" is a
    press release, and an approved plan adding four units is a loft conversion.

    None when the point was never tested (no coordinates, or the planning layer
    did not load), so the weight is redistributed instead of scoring the
    listing as if nothing is planned there. `plans_count == 0` *is* an answer,
    and scores 0.
    """
    if row.get("plans_count") is None:
        return None
    if not row.get("plans_count"):
        return 0.0
    density = row.get("plan_density")
    if density is None:
        # Planning is present but its magnitude cannot be measured - the
        # register did not record a usable area. Scored as "something here",
        # not as "nothing" and not as "a lot".
        scale = 30.0
    else:
        scale = _clamp(density / PLANNING_FULL_DENSITY * 100)
    stage = row.get("plan_stage_rank")
    return round(scale * ((stage if stage is not None else 30) / 100), 1)


#: Listings in one building at which selling pressure is scored full. Measured:
#: 1,788 addresses carry exactly two, and only 33 carry six or more - so five
#: concurrent listings in one building is already the far tail.
BUILDING_PRESSURE_FULL = 5


def _building_pressure(row):
    """How many owners are trying to leave this building at once.

    None when the address could not be resolved to a building at all (no house
    number), so the weight is redistributed rather than scoring the listing as
    if it stood alone - which is a different claim, and one we cannot make.
    """
    total = row.get("address_listings")
    if not total:
        return None
    if total <= 1:
        return 0.0
    private = row.get("address_private_listings") or 0
    # Private listings count double: an agency with four units in one tower is
    # marketing inventory, four owners selling without a broker is four people
    # who want out.
    weighted = (total - private) + private * 2
    return _clamp((weighted - 1) / (BUILDING_PRESSURE_FULL - 1) * 100)


#: What each stated reason is worth. A receiver is selling under a duty to
#: realise the asset and an estate is usually several heirs who want it over
#: with; "flexible on price" is the seller inviting an offer in writing. These
#: are not equivalent and are not scored as if they were.
MOTIVATION_WEIGHT = {
    "כינוס נכסים": 100.0,
    "עיזבון או ירושה": 85.0,
    "מחיר ירד": 75.0,
    "מחיר גמיש": 70.0,
    "דחוף": 65.0,
    "רילוקיישן או מעבר": 60.0,
    "פינוי מיידי": 45.0,
    # A seller who is not tied to a date is easier to deal with; it is not a
    # statement about price, and is weighted as the mild convenience it is.
    "פינוי גמיש": 25.0,
}


def _stated_motivation(row):
    """Score the reasons the seller wrote in the ad, or None if there is no ad
    text to read. `None`, not 0: an ad we never fetched the text of has not
    said it is unmotivated."""
    flags = row.get("motivation_flags")
    if flags is None:
        return None
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except (ValueError, TypeError):
            return None
    if not flags:
        return 0.0
    return max(MOTIVATION_WEIGHT.get(f, 40.0) for f in flags)


#: Above this many listings to one advertiser, the advertiser is running a
#: business. Chosen from the data, not from taste: 648 distinct phones carry
#: 2,829 ONMAP listings, and the largest single number is on 81 of them. A
#: person selling a flat and a desk selling eighty are not the same lead, and
#: the whole point of this component is to tell them apart.
PROFESSIONAL_PORTFOLIO = 10

#: The band where a portfolio actually says something. Two to four properties is
#: an owner with a position - an investor trimming, an inheritance being split,
#: a landlord exiting - and all three are negotiable in a way a single-flat
#: seller is not.
PRIVATE_PORTFOLIO_LOW, PRIVATE_PORTFOLIO_HIGH = 2, 4


def _portfolio_seller(row):
    """How much this advertiser's other listings say about their motivation.

    Deliberately **not** monotonic. "More properties = better lead" is the
    obvious shape and it is wrong at both ends: one property is an ordinary
    sale, and eighty is an agency's inventory page, which is the least
    negotiable listing on the board. The signal peaks in the middle and falls
    away above `PROFESSIONAL_PORTFOLIO`.

    Returns None where the advertiser is unknown, which on Yad2 is almost
    always - `_blend` then leaves this component out and renormalises rather
    than scoring the row zero for a fact nobody published.
    """
    count = row.get("seller_listings")
    if not count:
        return None
    # A censored count is a floor, and this one is only ever censored at 25 -
    # far into professional territory either way. See listing_seller.
    if row.get("seller_portfolio_censored") or count >= PROFESSIONAL_PORTFOLIO:
        return 5.0
    if count == 1:
        return 15.0
    if PRIVATE_PORTFOLIO_LOW <= count <= PRIVATE_PORTFOLIO_HIGH:
        return 100.0
    # 5-9: a large private holding, or a small agency. Real but ambiguous.
    return 55.0


#: Annual district move, in percent, that saturates the momentum component.
#: The six districts currently span +1.4% (צפון) to -3.2% (מרכז) against a
#: national -2.0%, so a +-5% band puts the whole live range inside the scale
#: without either end sitting on the clamp - which would flatten the four
#: districts in between into one number.
MOMENTUM_FULL_PCT = 5.0


def _market_momentum(row, *, for_buyer: bool):
    """Which way this listing's district is moving, and for whose benefit.

    The same fact read from both sides, like `scarcity` and `buyer_leverage`: a
    district falling 3.2% a year is a buyer walking into a weakening market and
    a seller who should have listed last year. So the buy and sell forms are
    exact inversions rather than two separately-argued scales.

    A district-level figure cannot separate two flats in the same district -
    it only re-ranks across districts - which is why the weight it carries is
    small. What it does do is stop a 5% discount in a falling district being
    read as the same find as a 5% discount in a rising one.

    None where the city could not be mapped to a district (5.2% of rows, all
    small localities inside regional councils that never appear as a planning
    jurisdiction), so the weight is redistributed rather than the row being
    scored as if its market were flat.
    """
    pct = row.get("district_pct_year")
    if pct is None:
        return None
    try:
        shift = float(pct) / MOMENTUM_FULL_PCT * 50.0
    except (TypeError, ValueError):
        return None
    return _clamp(50 - shift if for_buyer else 50 + shift)


def _yield_score(row):
    """Gross rental yield as a component, delegated to `listing_yield`.

    Imported lazily and guarded: the yield layer needs a rent harvest, and a
    deployment that has never run one must still score everything else rather
    than failing the whole pass.
    """
    if row.get("gross_yield_pct") is None:
        return None
    try:
        import listing_yield
        return listing_yield.score(row)
    except Exception:
        return None


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _blend(components: dict, weights: dict) -> tuple[float, float, dict]:
    """Weighted score over the components that have data.

    Returns (score, confidence, components). Confidence is the share of total
    weight that was actually covered - which is what makes it safe to omit a
    component instead of scoring it zero.
    """
    used = {k: v for k, v in components.items() if v is not None}
    covered = sum(weights[k] for k in used)
    if not covered:
        return 0.0, 0.0, {}
    score = sum(used[k] * weights[k] for k in used) / covered
    return round(score, 1), round(covered / sum(weights.values()), 2), used


#: Buy side. The gap to size-matched peers carries the most weight because it
#: is the only component measured against a like-for-like yardstick; the rest
#: describe how negotiable the seller is likely to be.
BUY_WEIGHTS = {"value_gap": 0.27, "distribution": 0.10, "time_on_market": 0.11,
               # Gross rental yield. Measured against this corpus before it was
               # given a weight: it correlates +0.52 with `value_gap` overall
               # and +0.69 *within* a city, so inside one town it is largely
               # the same statement - which is why `value_gap` gives up 0.03 to
               # make room for it rather than both being paid in full for the
               # shared part. What it adds is the axis `value_gap` cannot have:
               # peers are drawn within a city, so every city's median peer gap
               # is ~0.0% by construction, while city median yields run from
               # 2.41% in גבעתיים to 4.21% in אשדוד. See listing_yield.
               "gross_yield": 0.10,
               "price_cut": 0.11, "direct_seller": 0.07, "planning_upside": 0.13,
               # The building tells you things the neighbourhood cannot: what
               # the flats in the same stairwell are asking, and whether several
               # owners are trying to leave at once. See listing_seller.
               "building_gap": 0.09, "building_pressure": 0.07,
               # Whether the building itself is a redevelopment candidate.
               # See listing_parcel: the 1980 line is the TAMA 38 test, not a
               # rule of thumb.
               "renewal_candidate": 0.06,
               # The seller's own words. Every other motivation signal here is
               # inferred; this one is stated. See onmap_feed.MOTIVATION_PATTERNS.
               "stated_motivation": 0.06,
               # Kept small on purpose. `_blend` reports confidence as the share
               # of total weight a row could answer, so a component only ~5% of
               # rows can answer costs every other row confidence just by
               # existing. It earns 0.06 because where it *is* known it is one
               # of the few facts here about the person rather than the flat.
               "portfolio_seller": 0.06,
               "buyer_leverage": 0.08,
               # Which way the district is moving. Small on purpose: it is
               # constant within a district and so can only re-rank across
               # them. See `_market_momentum`.
               "market_momentum": 0.05}

#: Sell side answers a different question - "is this a good place and moment to
#: put a flat on the market" - so it rewards the opposite of a bargain: local
#: prices that hold up, and little competing supply.
SELL_WEIGHTS = {"price_strength": 0.28, "scarcity": 0.21, "neighborhood_premium": 0.20,
                "planning_upside": 0.12,
                # Neighbours competing for the same buyer, in the same lobby.
                "building_competition": 0.11,
                # Timing is a seller's decision in a way it is not a buyer's -
                # a buyer picks a flat, a seller picks a month - so the same
                # district signal is worth more here than on the buy side.
                "market_momentum": 0.08}


def assess(row, peers: PeerIndex, *, history=None, city_medians=None,
           deflate=None) -> dict:
    """Every derived measure for one listing, with its basis attached."""
    history = history or {}
    city_medians = city_medians or {}
    out = {}

    ppm = row.get("price_per_m2")
    price = row.get("price")
    # A row whose own area is not a floor area gets no gap. It was already kept
    # out of every benchmark; letting it be *measured* against one would hand it
    # a huge fake discount and float it straight to the top of the buy list.
    area = plausible_area(row.get("sqm"))
    if area is None:
        out["value_basis"] = "שטח לא סביר — אין השוואה"
    label, sample, larger_peers = (peers.lookup(row) if area is not None
                                   else (None, None, None))
    # Both sides of every comparison are size-adjusted. Measuring this
    # listing's *raw* ₪/m² against a median of *adjusted* peer values compares
    # two different units, and does it in a way that grows with size: it
    # reported 500-1000 m² homes as 50% below their peers, a worse skew than
    # the bias the adjustment exists to remove.
    own = peers.adjust(row) if area is not None else None
    if label and own:
        peer_ppm = statistics.median(sample)
        out["peer_scope"] = label
        out["peer_count"] = len(sample)
        # Reported back at the listing's own size, because "23,000 ₪/m² for
        # homes like this" is a number a person can check against the ad, and
        # the 100 m²-equivalent is an internal device.
        beta = peers.elasticity(row.get("city"))
        out["peer_ppm"] = round(peer_ppm * (area / REFERENCE_AREA) ** beta)
        out["size_elasticity"] = round(beta, 3)
        # Positive = cheaper than peers. Expressed twice on purpose: a percent
        # is comparable across the country, shekels are what a buyer negotiates.
        out["peer_gap_pct"] = round((peer_ppm - own) / peer_ppm * 100, 1)
        expected = out["peer_ppm"] * area
        out["expected_price"] = round(expected)
        out["peer_gap_ils"] = round(expected - float(price)) if price else None
        out["ppm_percentile"] = _percentile_of(own, sample)
        # Competing supply in the same peer group - the seller's alternatives
        # from a buyer's side, and the buyer's alternatives from a seller's.
        out["supply_count"] = len(sample)
        out["value_basis"] = f"{len(sample)} נכסים דומים · {label}"
        # A home with almost nothing larger to compare against will always read
        # as cheap: its peer window can only reach downwards, and smaller homes
        # cost more per m². This is the boundary artefact that made 500-1000 m²
        # properties look 8% underpriced as a class. Where the comparison is
        # this one-sided the gap is still reported - it is real information -
        # but the buy score omits it rather than ranking on it.
        out["peer_larger_count"] = larger_peers
        if larger_peers is not None and larger_peers < 2:
            out["value_basis"] += " · אין נכסים גדולים יותר להשוואה"
        if abs(out["peer_gap_pct"]) > IMPLAUSIBLE_GAP_PCT:
            out["value_flag"] = ("פער חריג מול נכסים דומים — בדוק חלק בנכס, "
                                 "שטח או סיווג לפני פנייה")

    # Floor area per room against the local norm. Measured nationally at
    # p10 = 22, p50 = 29, p90 = 59 m² per room. A flat well under its area's
    # norm is chopped into small rooms - which is why its ₪/m² can look normal
    # while the flat itself is worth less - and one well above has generous
    # rooms. Neither is a price signal on its own; both explain one.
    area, rooms = plausible_area(row.get("sqm")), row.get("rooms")
    try:
        per_room = area / float(rooms) if area and rooms and float(rooms) > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        per_room = None
    if per_room:
        out["m2_per_room"] = round(per_room, 1)
        norm = (city_medians or {}).get("_per_room")
        if norm:
            out["layout_vs_norm_pct"] = round((per_room - norm) / norm * 100)

    dom, basis = days_on_market(row)
    out["dom_days"] = dom
    out["dom_basis"] = basis

    change = price_change(row, history, deflate=deflate)
    if change:
        out["price_change_pct"] = change["pct"]
        # Both are kept. The real figure is what `price_cut` scores; the
        # nominal one is what the seller thinks they did, and a reader
        # comparing the screen to the ad needs to find it.
        out["price_change_nominal_pct"] = change["nominal_pct"]
        out["price_change_real"] = change["real"]

    city_ppm = city_medians.get(_norm(row.get("city")))
    if city_ppm and ppm:
        out["city_ppm"] = round(city_ppm)
        out["city_gap_pct"] = round((city_ppm - float(ppm)) / city_ppm * 100, 1)

    # ---- buy side ----------------------------------------------------------
    gap = out.get("peer_gap_pct")
    buy = {
        # 0% below peers -> 50; 25% below -> 100; 25% above -> 0. Linear, and
        # clamped so one absurd row cannot dominate the ranking.
        "value_gap": (_clamp(50 + (gap * 2))
                      if gap is not None and "value_flag" not in out
                      and (out.get("peer_larger_count") is None
                           or out["peer_larger_count"] >= 2) else None),
        # Cheapest decile of its peer group scores highest.
        "distribution": (_clamp(100 - out["ppm_percentile"])
                         if out.get("ppm_percentile") is not None else None),
        # Six months on the board is a seller who has run out of other options.
        "time_on_market": _clamp(dom / 180 * 100) if dom is not None else None,
        # A cut already made is the strongest published evidence of motivation.
        "price_cut": (_clamp(abs(out["price_change_pct"]) * 10)
                      if out.get("price_change_pct", 0) < 0 else
                      (0.0 if "price_change_pct" in out else None)),
        # Stated by the source, not inferred from a missing agency field.
        "direct_seller": (100.0 if row.get("brokerage") == "ללא תיווך"
                          else 0.0 if row.get("brokerage") == "תיווך" else None),
        # The one signal no listing board has: what is planned on this ground.
        # See listing_planning.
        "planning_upside": _planning_score(row),
        # Cheap against the flats in the same building. This is the tightest
        # comparison in the system - age, location and build quality are not
        # similar, they are identical - so it is worth its own component rather
        # than being folded into the neighbourhood gap.
        # Same implausibility guard as the peer gap, for the same reason: a
        # flat reading 99.8% below its own building's median is a broken record
        # - a part-share, or an area entered in the wrong unit - and without
        # this it led the "cheap for its building" ranking outright.
        "building_gap": (_clamp(50 + row["address_gap_pct"] * 2)
                         if row.get("address_gap_pct") is not None
                         and abs(row["address_gap_pct"]) <= IMPLAUSIBLE_GAP_PCT
                         and (row.get("address_listings") or 0) > 1 else None),
        # Several owners leaving one building at once is leverage. Weighted by
        # how many of them are selling without a broker: four agency listings
        # in a tower is inventory, four private ones is people getting out.
        "building_pressure": _building_pressure(row),
        # A pre-1980 building is TAMA 38 eligible, which is a right attached to
        # the flat that its asking price may not reflect. None where the year
        # is unknown - which is most of the country, since the municipal
        # building layer is Tel Aviv only.
        "renewal_candidate": (None if row.get("building_year") is None
                              else 100.0 if row.get("building_pre_1980") else 20.0),
        "stated_motivation": _stated_motivation(row),
        # Who is on the other side of the table, and what else they are holding.
        # Only answerable since the ONMAP contact sweep; see _portfolio_seller
        # for why the scale is a band rather than a ramp.
        "portfolio_seller": _portfolio_seller(row),
        # The mirror of the sell side's `scarcity`, and the only component here
        # that describes the *market* rather than the listing or its building.
        # A buyer looking at forty comparable ads can walk away from any of
        # them, and the seller knows it. Same scale as `scarcity`, inverted.
        "buyer_leverage": (_clamp((out["supply_count"] - MIN_PEERS) * 2.5)
                           if out.get("supply_count") is not None else None),
        # The only component here measured outside the listing boards: the
        # CBS district price index. See listing_market for the city->district
        # join and for the two candidates - transit and schools - that were
        # measured against this same corpus and did not earn a weight.
        "market_momentum": _market_momentum(row, for_buyer=True),
        # What it would return if let. The only comparison here that is not
        # price against price - see listing_yield. None where the city has no
        # rent sample (52% of rows) or where the yield came out implausible,
        # so the weight is redistributed rather than a town being scored as if
        # it yielded nothing.
        "gross_yield": _yield_score(row),
    }
    score, confidence, used = _blend(buy, BUY_WEIGHTS)
    # Peer depth belongs in the confidence, not just the component coverage. A
    # 30%-below-peers verdict drawn from five comparable ads and one drawn from
    # a hundred are not the same claim, and ranking them together puts the
    # thinnest evidence at the top - the failure `opportunity.confidence` was
    # introduced to stop. Full weight from EVIDENCE_PEERS peers upward.
    confidence = round(confidence * _evidence(out.get("peer_count")), 2)
    out["buy_score"] = score
    out["buy_confidence"] = confidence
    out["buy_components"] = used
    out["buy_rank"] = round(score * confidence, 1)

    # ---- sell side ---------------------------------------------------------
    sell = {
        # Asking above peers and holding = the area supports the price. Guarded
        # by the same implausibility rule as the buy side, and for the mirror
        # reason: without it the sell list was led by a 16 m² listing reading
        # 139% *above* its peers, which is a broken row, not a strong market.
        "price_strength": (_clamp(50 - (gap * 2))
                           if gap is not None and "value_flag" not in out
                           else None),
        # Thin competing supply is a seller's advantage. 40+ comparable ads in
        # the same band is a crowded shelf; under 10 is scarcity.
        "scarcity": (_clamp(100 - (out["supply_count"] - MIN_PEERS) * 2.5)
                     if out.get("supply_count") is not None else None),
        # A neighbourhood priced above its city is where buyers are competing.
        "neighborhood_premium": (_clamp(50 - out["city_gap_pct"] * 2)
                                 if out.get("city_gap_pct") is not None else None),
        # Approved density on the ground supports an asking price as much as it
        # rewards a buyer - it is the same fact read from the other side.
        "planning_upside": _planning_score(row),
        # For a seller the same concentration is the opposite of leverage: the
        # buyer walking in has three other flats to see without leaving the
        # lobby. Inverted, and only where a building group actually exists.
        "building_competition": (_clamp(100 - (row["address_listings"] - 1) * 22)
                                 if row.get("address_listings") else None),
        # The mirror of the buy side's reading of the same index: a rising
        # district is the seller's tailwind and the buyer's cost.
        "market_momentum": _market_momentum(row, for_buyer=False),
    }
    score, confidence, used = _blend(sell, SELL_WEIGHTS)
    confidence = round(confidence * _evidence(out.get("peer_count")), 2)
    out["sell_score"] = score
    out["sell_confidence"] = confidence
    out["sell_components"] = used
    out["sell_rank"] = round(score * confidence, 1)
    return out


def city_medians(rows) -> dict:
    values = defaultdict(list)
    per_room = []
    for row in rows:
        ppm = row.get("price_per_m2")
        if ppm and row.get("data_quality") in (None, "תקין"):
            values[_norm(row.get("city"))].append(float(ppm))
        area, rooms = plausible_area(row.get("sqm")), row.get("rooms")
        try:
            if area and rooms and float(rooms) > 0:
                per_room.append(area / float(rooms))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    out = {city: statistics.median(v) for city, v in values.items() if v}
    # Stored under a key no city can collide with: `_norm` lowercases and
    # strips, so a leading underscore is unreachable from a city name.
    if per_room:
        out["_per_room"] = statistics.median(per_room)
    return out


def annotate(rows: list[dict], *, history=None, deflate=None) -> list[dict]:
    """Attach every measure to every row. One peer index for the whole set."""
    peers = PeerIndex(rows)
    medians = city_medians(rows)
    for row in rows:
        row.update(assess(row, peers, history=history, city_medians=medians,
                          deflate=deflate))
    return rows


def cpi_deflator(conn, years_back: int = 12):
    """A `month -> multiplier` callable for `price_change`.

    Built **eagerly** and closed over a plain dict, not over the connection.
    The caller's connection is closed as soon as the annotation pass starts and
    the callable is used long after that; a lazily-querying closure would have
    raised `ProgrammingError` on the first row with price history, or worse,
    kept a connection open for the life of the cache entry.

    One pass over the monthly series builds every month's ratio to the latest,
    which costs one query for a table of a few hundred rows.
    """
    import market_client

    # A fresh database (no sync ever run - e.g. a first deploy with no
    # persistent disk) has no cbs_price_index table at all: nothing else on
    # this path called market_client.ensure_schema() first, unlike
    # onmap_feed.rows() and similar readers that ensure their own schema
    # before querying. Without this, a plain Yad2/ONMAP listings request -
    # nothing CPI-related about it on its face - raised OperationalError and
    # took the whole /api/combined-sale-listings response down over an
    # empty, optional benchmark table.
    market_client.ensure_schema(conn)

    series_id = market_client.COST_SERIES["cpi"]
    rows = conn.execute(
        "SELECT period, pct_change FROM cbs_price_index WHERE series_id=? "
        "AND period_type='month' AND period >= ? ORDER BY period DESC",
        (series_id, f"{datetime.now(timezone.utc).year - years_back}-01")).fetchall()

    factors: dict[str, float] = {}
    running = 1.0
    for row in rows:
        # Walking backwards: the multiplier for a month is the product of every
        # change *after* it, which is exactly what has accumulated so far.
        factors[row["period"]] = round(running, 6)
        if row["pct_change"] is None:
            break
        running *= 1 + float(row["pct_change"]) / 100.0

    # Missing months return None, not 1.0 - an adjustment that could not be
    # made must stay distinguishable from one that came out at zero.
    return factors.get


def price_history_index(conn, table: str, key: str) -> dict:
    """{listing id: [(observed_at, price), ...]} oldest first."""
    out = defaultdict(list)
    for row in conn.execute(
            f"SELECT {key} AS k, price, observed_at FROM {table} "
            f"ORDER BY {key}, observed_at"):
        out[str(row["k"])].append((row["observed_at"], row["price"]))
    return out
