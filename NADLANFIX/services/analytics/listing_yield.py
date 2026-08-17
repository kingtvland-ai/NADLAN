"""What a sale listing would return if it were let.

The one comparison in this system that is not price against price
------------------------------------------------------------------
`listing_analytics` asks whether an asking price is low relative to other
asking prices. That is a closed system: if a whole city is expensive, every
flat in it reads as fairly priced, because the yardstick is made of the same
opinions being measured. Rent breaks the circle. It is the price of *using* the
flat, agreed monthly by two parties, and it does not move with what buyers hope
the place will be worth in five years.

The gap between the two is the yield, and it varies far more than either side
alone. Measured on the harvest below: Tel Aviv lets at 114-118 ₪/m² a month
against Beer Sheva's 38.5 and Akko's 33.8 - a 3.5x spread in rent, against a
much wider spread in sale prices. That difference *is* the finding.

The benchmark is city-level, and that is a limit, not a preference
------------------------------------------------------------------
`listing_analytics` benchmarks against neighbourhood-and-rooms peers because
the sale corpus has 61,478 rows to spend. The rent board does not: ONMAP's rent
feed stops paginating at `$skip≈960` where the sale feed runs to 3,000, so four
orderings union to **959 usable rentals**. At that size only **16 cities** hold
ten or more, and only 16 *neighbourhoods* hold eight - so a neighbourhood
benchmark would be built from three or four ads and would be noise with a
decimal point. City it is, and every yield says which city sample it came from
and how deep that sample was.

Size is modelled, not bucketed - and the exponent is not the sale one
---------------------------------------------------------------------
Rent per m² falls with size for the same reason price does, and **not at the
same rate**. Fitted over 953 rentals, the rent elasticity is **-0.23** against
the sale side's **-0.34**. That difference is not a detail to be smoothed over:
it means yield *rises* with floor area as an arithmetic consequence, and using
the sale exponent on the rent side would bake a fake size trend straight into
the yield. So the rent elasticity is fitted from rent data, on the same
`fit_elasticity` the sale side uses.

What a yield here is and is not
--------------------------------
It is **gross**. Nothing is deducted: not the 10% management, not vacancy
between tenants, not the maintenance an old building needs to stay lettable,
not the ~0.3% purchase tax band an investment second home attracts, not the
committee fees. Net yield in Israel typically lands 1 to 1.5 points below the
gross figure and the deductions differ per property, which is precisely why
they are not invented here. The number is a screening ratio for comparing
listings against each other, and it is labelled gross everywhere it appears.

It is also an **expected** rent, not this flat's rent. Nobody has let this
particular unit; the figure says what flats of this size in this city are
being advertised at. Condition, floor, parking, renovation and furniture all
move a real rent and none of them are here.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from services.analytics import listing_analytics
from services.analytics.listing_market import norm_city

#: Rentals a city needs before its own benchmark is trusted. Below ten, the
#: median is three or four ads and the yield it produces would swing by a
#: quarter on one furnished penthouse.
MIN_CITY_RENTS = 10

#: Rentals needed nationally before the size elasticity is fitted from rent at
#: all. Under this the national sale exponent would be the only alternative,
#: and it is the wrong one - see the module docstring.
MIN_FIT_RENTS = 100

#: Fallback exponent if the rent sample is ever too thin to fit. Measured at
#: -0.23 on 953 rentals; frozen here so a thin harvest degrades to the last
#: known good measurement rather than to the sale side's -0.34.
DEFAULT_RENT_ELASTICITY = -0.23

#: Property families a residential rent benchmark can speak for. The rent
#: sample is **81% plain apartments** (780 of 959) with 8 villas and 20
#: cottages in it, so it describes dwellings and nothing else.
#:
#: The exclusions are not fussiness. A plot has no rent: land does not let by
#: the month, and 274 מגרשים were being handed an "expected rent" derived from
#: flats and a yield built on it. Storage, parking and agricultural holdings
#: are the same error in smaller numbers. This is the rule
#: `listing_analytics._LEVELS` already enforces on the sale side - no falling
#: between property types - applied to the rent side for the same reason.
#: **Both vocabularies, deliberately.** Yad2 publishes Hebrew type names and
#: ONMAP publishes English slugs for the same things, and `property_family`
#: normalises whitespace without translating - by design, since merging types
#: is the error it exists to prevent. A Hebrew-only set therefore refused a
#: yield to **all 2,839 ONMAP rows** and did it silently, as an empty column
#: that looked like missing data rather than a rule firing. Any new source
#: with a third vocabulary must be added here.
RENTABLE_FAMILIES = {
    # Yad2
    "דירה", "דירת גן", "גג/ פנטהאוז", "דופלקס", "טריפלקס", "סטודיו/ לופט",
    "בית פרטי/ קוטג'", "דו משפחתי", "יחידת דיור",
    # ONMAP
    "apartment", "garden_apartment", "penthouse", "rooftop_apartment",
    "mini_penthouse", "duplex", "triplex", "studio", "loft", "cottage",
    "villa", "housing_unit",
}

#: Gross yields outside this band are not findings, they are broken rows: a
#: 15% gross yield in Israel means the sale price is wrong, the area is wrong,
#: or the flat is a part-share. Reported with a flag, and kept out of scoring
#: for the same reason `listing_analytics` guards its peer gap.
MIN_PLAUSIBLE_YIELD, MAX_PLAUSIBLE_YIELD = 0.8, 12.0

#: Gross yield scored as a full 100. The national median on this corpus is
#: ~2.9% and the 90th percentile ~4.4%; 6% is a genuinely strong Israeli
#: residential yield and nothing above it is more interesting for being higher.
YIELD_FULL_PCT = 6.0

#: ...and the floor, below which a listing is being bought for capital growth
#: rather than income. Tel Aviv sits here routinely, which is a fact about
#: Tel Aviv and not a defect.
YIELD_ZERO_PCT = 1.5


class RentIndex:
    """City rent benchmarks, size-adjusted, built once per corpus."""

    __slots__ = ("cities", "beta", "national", "sample")

    def __init__(self, rentals: list[dict]):
        points, by_city = [], defaultdict(list)
        for row in rentals:
            area = listing_analytics.plausible_area(row.get("area_sqm"))
            price = row.get("price")
            if not area or not price:
                continue
            try:
                per_m2 = float(price) / area
            except (TypeError, ValueError):
                continue
            points.append((area, per_m2))
            # Normalised on the way in. The board spells the same city both
            # "תל אביב יפו" and "תל אביב-יפו", which split its 130 rentals into
            # two samples of 47 and 83 - and would have benchmarked half the
            # city's listings against the thinner half.
            by_city[norm_city(row.get("city"))].append((area, per_m2))

        self.sample = len(points)
        fitted = (listing_analytics.fit_elasticity(points)
                  if len(points) >= MIN_FIT_RENTS else None)
        self.beta = fitted if fitted is not None else DEFAULT_RENT_ELASTICITY

        # Every rent restated as ₪/m²/month at the reference size, so cities
        # with different typical flat sizes are comparable at all.
        #
        # The observed area range is kept alongside each median. Extrapolating
        # a benchmark fitted on 40-140 m² flats out to a 500 m² house is how
        # Karmiel's ten rentals produced a confident "13,352 ₪/month" for a
        # villa - arithmetically consistent with the fit and outside anything
        # the sample has ever seen.
        self.cities = {}
        national = []
        for city, rows in by_city.items():
            adjusted = [self._to_reference(area, per_m2) for area, per_m2 in rows]
            national.extend(adjusted)
            if city and len(adjusted) >= MIN_CITY_RENTS:
                areas = sorted(area for area, _ in rows)
                self.cities[city] = {
                    "ppm": statistics.median(adjusted),
                    "sample": len(adjusted),
                    "min_area": areas[0],
                    "max_area": areas[-1],
                }
        self.national = statistics.median(national) if national else None

    def _to_reference(self, area, per_m2):
        return per_m2 * (area / listing_analytics.REFERENCE_AREA) ** (-self.beta)

    def expected_rent(self, city, area) -> dict | None:
        """What a flat of this size in this city is advertised at, with its basis.

        Returns None rather than falling back to the national median. A national
        rent benchmark applied to a town with no rent sample would state a
        confident number for the one thing this source cannot see, and the
        national median is dominated by the dense centre - it would tell every
        periphery listing it yields badly, which is the opposite of true.

        `supported` says whether this listing's size sits inside the range the
        city's rentals actually cover. Outside it the figure is still returned,
        because a reader comparing it to the ad learns something either way -
        but `annotate` will not let it into the score.
        """
        area = listing_analytics.plausible_area(area)
        if not area:
            return None
        entry = self.cities.get(norm_city(city))
        if not entry:
            return None
        # Undo the reference-size adjustment for this listing's actual size.
        per_m2 = entry["ppm"] * (area / listing_analytics.REFERENCE_AREA) ** self.beta
        supported = entry["min_area"] <= area <= entry["max_area"]
        return {
            "rent": per_m2 * area,
            "sample": entry["sample"],
            "supported": supported,
            "basis": f"{entry['sample']} השכרות ב{city}",
            "range": (entry["min_area"], entry["max_area"]),
        }


def build_index() -> RentIndex | None:
    try:
        from ingestion.feeds import onmap_rent
        rentals = onmap_rent.rows()
    except Exception:
        return None
    return RentIndex(rentals) if rentals else None


def annotate(rows: list[dict], index: RentIndex | None = None) -> list[dict]:
    """Attach an expected rent and a gross yield to every priced sale listing."""
    if index is None:
        index = build_index()
    if index is None:
        for row in rows:
            row["yield_basis"] = "אין מדגם השכרות"
        return rows

    for row in rows:
        price = row.get("price")
        family = listing_analytics.property_family(row.get("property_type"))
        # A plot does not let by the month. Refused before anything is
        # computed, rather than computed and then flagged, because there is no
        # figure here to show a reader - not a bad one, none.
        if family not in RENTABLE_FAMILIES:
            row["gross_yield_pct"] = None
            row["yield_basis"] = (
                "המקור לא מפרסם סוג נכס — אי אפשר לדעת אם זו דירה או מגרש"
                if family == "לא צוין"
                else f"אין שכ״ד להשוואה לסוג הנכס ({family})")
            continue
        estimate = index.expected_rent(row.get("city"), row.get("sqm")
                                       if row.get("sqm") is not None
                                       else row.get("area_sqm"))
        if not estimate or not price:
            row["gross_yield_pct"] = None
            row["yield_basis"] = ("אין מדגם השכרות לעיר הזאת" if not estimate
                                  else "אין מחיר להשוואה")
            continue
        rent = estimate["rent"]
        try:
            yield_pct = rent * 12 / float(price) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        row["expected_rent"] = round(rent)
        row["rent_sample"] = estimate["sample"]
        row["yield_supported"] = estimate["supported"]
        row["gross_yield_pct"] = round(yield_pct, 2)
        row["yield_basis"] = (f"תשואה ברוטו · שכ״ד צפוי {round(rent):,} ₪ · "
                              f"{estimate['basis']}")
        if not estimate["supported"]:
            low, high = estimate["range"]
            # Shown, and kept out of the score. Same treatment as
            # `peer_larger_count` on the sale side, for the same reason: a
            # comparison the sample cannot support is information about the
            # sample, not about the listing.
            row["yield_basis"] += (f" · מחוץ לטווח השטחים במדגם "
                                   f"({low:.0f}–{high:.0f} מ״ר)")
            row["yield_extrapolated"] = True
        if not (MIN_PLAUSIBLE_YIELD <= yield_pct <= MAX_PLAUSIBLE_YIELD):
            row["yield_flag"] = ("תשואה חריגה — בדוק מחיר, שטח או חלק בנכס "
                                 "לפני שמסתמכים עליה")
    return rows


def score(row):
    """Gross yield as a 0-100 component, or None where it cannot be measured.

    Flagged and extrapolated rows return None rather than a high score: a 40%
    gross yield is a broken record, and a yield for a 500 m² house drawn from
    a sample of 40-140 m² flats is a guess wearing a percentage sign. The whole
    point of both guards is that neither may lead an income ranking.
    """
    value = row.get("gross_yield_pct")
    if value is None or "yield_flag" in row or row.get("yield_extrapolated"):
        return None
    span = YIELD_FULL_PCT - YIELD_ZERO_PCT
    return max(0.0, min(100.0, (float(value) - YIELD_ZERO_PCT) / span * 100))


def coverage(rows) -> dict:
    reported = [r for r in rows if r.get("gross_yield_pct") is not None]
    scorable = [r for r in reported if score(r) is not None]
    values = [r["gross_yield_pct"] for r in scorable]
    return {
        "rows": len(rows),
        "with_yield": len(reported),
        "scored": len(scorable),
        "extrapolated": sum(1 for r in reported if r.get("yield_extrapolated")),
        "flagged": sum(1 for r in reported if "yield_flag" in r),
        "median_yield": round(statistics.median(values), 2) if values else None,
    }
