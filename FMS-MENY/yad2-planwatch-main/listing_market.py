"""The two facts about a listing that come from outside the listing boards.

Where a flat sits in its district's price cycle, and what the money to buy it
costs this month. Neither is visible on any board, neither can be derived from
the other 69,055 ads, and both change the answer to "should this be bought or
sold now" without saying anything about the flat itself.

Market momentum
---------------
`listing_analytics` measures a listing against its peers *today*. That is a
snapshot, and a snapshot cannot tell a 5% discount in a district rising 1.4% a
year from the same 5% in one falling 3.2% - the second is not a discount, it is
the market arriving. The CBS district index is the only series here that
measures the direction, and on the latest reading the six districts span 4.6
points:

    צפון  +1.4    ירושלים +0.3    דרום  -0.5
    חיפה  -2.6    תל-אביב -2.6    מרכז  -3.2

The join is city -> district, taken from `plans.jurisdiction_name` ->
`plans.district_name`. That is a real mapping in this database rather than a
table typed out by hand, and it covers 238 jurisdictions. Raw string equality
matched 79.4% of listing rows; the misses were all punctuation - "תל אביב יפו"
against "תל אביב-יפו", "פתח תקווה" against "פתח תקוה", and names carrying a
second official form after a slash - so the key is normalised on both sides.

Cost of the money
-----------------
`finance_client` reads what banks are charging; this attaches the resulting
monthly payment to each asking price. It is deliberately **not** a scoring
component: the payment is a monotone function of the price, so scoring it would
weight the price twice under a second name, and `value_gap` already carries
price. It is shown because it is the number a buyer decides on, and because
"2,400,000 ₪" and "10,335 ₪ a month, needing 34,900 ₪ of household income" are
the same fact at two very different levels of usefulness.

What was measured and left out
------------------------------
Transit and school proximity are attached by `listing_amenities` and are **not
scored**, because the measurement did not support it. Against 61,478 Yad2 rows:
within neighbourhoods that straddle the 800 m line, being near a rail-grade
station is worth a median **-0.9%** and is positive in only 10 of 29 such
neighbourhoods; time on market is flat across every distance band (75, 74, 77
and 76 days). School density reads **-13%**, which is not a schools effect at
all - institutions per km² is a measure of how dense and old a place is, and
scoring it would rank urban density under a friendlier name. The lines were
built where they were built, and at this resolution their effect cannot be
separated from that. The distances are real and are reported and filterable;
they do not move a score.
"""

from __future__ import annotations

import re

import finance_client
import market_client

#: Punctuation that separates the same place from itself across two registers.
#: Maqaf, hyphen, quotes and the slash that carries a second official name
#: ("נצרת עילית / נוף הגליל").
_STRIP = re.compile(r"[־\-'\"״׳`]+")


def norm_city(value) -> str:
    """A city name reduced to what two registers agree on."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.split("/")[0]
    text = _STRIP.sub(" ", text)
    # The register spells the same town with and without a final he
    # ("פתח תקווה" / "פתח תקוה"); collapsing double vav settles it.
    text = text.replace("וו", "ו").replace("יי", "י")
    return " ".join(text.split())


def city_districts(conn) -> dict:
    """city -> district, from the planning register's own jurisdictions.

    Where a jurisdiction appears under more than one district the most
    frequent wins - a handful of plans are filed against a neighbouring
    district's committee, and one such filing should not move a whole city.
    """
    out: dict[str, str] = {}
    for row in conn.execute(
            """SELECT jurisdiction_name j, district_name d, COUNT(*) n
                 FROM plans
                WHERE jurisdiction_name IS NOT NULL AND district_name IS NOT NULL
                GROUP BY j, d ORDER BY n DESC"""):
        key = norm_city(row["j"])
        if key and key not in out:
            out[key] = row["d"]
    return out


def district_trends(conn) -> dict:
    """district -> its latest index reading, for every district CBS publishes."""
    out = {}
    for district in market_client.DISTRICT_TO_CBS:
        trend = market_client.district_trend(conn, district)
        if trend:
            out[district] = trend
    return out


def context(conn) -> dict:
    """Everything this layer needs, read once for the whole corpus."""
    mortgage = finance_client.mortgage_rate(conn)
    return {
        "districts": city_districts(conn),
        "trends": district_trends(conn),
        "national": market_client.national_trend(conn),
        "mortgage": mortgage,
        "rate": (mortgage or {}).get("rate"),
        "pti": (mortgage or {}).get("pti_actual"),
        "costs": market_client.cost_trends(conn),
    }


def annotate(rows: list[dict], ctx: dict | None = None) -> list[dict]:
    """Attach district momentum and the monthly cost of each asking price."""
    if ctx is None:
        import db
        conn = db.get_conn()
        try:
            ctx = context(conn)
        finally:
            conn.close()

    districts, trends = ctx["districts"], ctx["trends"]
    rate, pti = ctx.get("rate"), ctx.get("pti")
    national = (ctx.get("national") or {}).get("pct_year")

    for row in rows:
        district = districts.get(norm_city(row.get("city")))
        trend = trends.get(district) if district else None
        if trend:
            row["district"] = district
            row["district_pct_year"] = trend.get("pct_year")
            row["district_pct_month"] = trend.get("pct_change")
            row["district_period"] = trend.get("period")
            if trend.get("pct_year") is not None and national is not None:
                # The district against the country: a district falling 3.2%
                # while the country falls 2.0% is losing ground for its own
                # reasons, which is the part worth knowing.
                row["district_vs_national"] = round(trend["pct_year"] - national, 1)
            row["market_basis"] = (
                f"מדד מחירי דירות {district} · {trend.get('period')} · "
                f"שינוי שנתי {trend.get('pct_year'):+.1f}%"
                if trend.get("pct_year") is not None else
                f"מדד מחירי דירות {district} · {trend.get('period')}")
        else:
            # Recorded as measured-and-absent so the score can tell a city with
            # no district reading apart from one never looked up.
            row["district_pct_year"] = None
            row["market_basis"] = "אין מדד מחוזי לישוב הזה"

        if rate is not None and row.get("price"):
            money = finance_client.affordability(row["price"], rate, pti=pti)
            if money:
                row["monthly_payment"] = money["monthly_payment"]
                row["down_payment"] = money["down_payment"]
                row["income_needed"] = money["income_needed"]
                row["mortgage_rate"] = money["rate"]
    return rows


def coverage(rows) -> dict:
    return {
        "rows": len(rows),
        "with_district": sum(1 for r in rows if r.get("district_pct_year") is not None),
        "with_payment": sum(1 for r in rows if r.get("monthly_payment")),
    }
