"""The price of the money, not the price of the flat.

Why a listing tool needs the interest rate
------------------------------------------
Every number in `listing_analytics` is a comparison between asking prices. That
is the right way to answer "is this cheap for the area", and it is silent about
the question a buyer actually asks in the room: *can I carry it*. Two flats at
the same 2,400,000 ₪ are not the same purchase at 2.5% and at 4.8% - the
monthly payment on a 25-year 75% mortgage moves from 8,065 ₪ to 10,335 ₪, a
28% difference in the only figure that decides whether a household signs.

None of the three listing boards carries this, and neither did this system. The
asking price was treated as if money were free. It is not, and the Bank of
Israel publishes exactly what it costs.

What is pulled, and from where
------------------------------
Two publishers, three things:

* **Bank of Israel policy rate** - `boi.org.il/PublicApi/GetInterest`. A tiny
  JSON document with the current rate and the next decision date. Unambiguous,
  no interpretation needed, so it is read live rather than stored.
* **Bank of Israel mortgage statistics** - the `BIR_MRTG_99` SDMX dataflow
  ("ריביות וביצועים לדיור"), which carries the *actual average rate banks
  charged* on new housing loans, split by indexation and by fixed/variable.
  This is what a borrower is offered, and it sits 1.3 points above the policy
  rate - using the policy rate as a mortgage rate would understate every
  payment in the system by about a fifth.
* **CBS construction input cost index** (series 200010) and **CPI** (130010),
  imported through `market_client.CBS_SERIES` alongside the apartment price
  indices that were already there. These are what make a price from a year ago
  comparable to one from today.

The SDMX endpoint, and the one URL shape that answers
------------------------------------------------------
BOI's FusionEdge server accepts SDMX 2.1 REST, but only one of the documented
data paths returns anything - the rest answer the site's Hebrew WAF error page
with HTTP 404, which is not an SDMX 404 and does not mean the flow is missing:

    /sdmx/v2/data/dataflow/BOI.STATISTICS/{flow}          200  <- this one
    /sdmx/v2/data/dataflow/BOI.STATISTICS/{flow}/1.0/all  404
    /sdmx/v1/rest/data/BOI.STATISTICS,{flow},1.0/all      404

`format=csv` works on the path that answers; `format=jsondata` 404s on all of
them. So this module speaks flat SDMX-CSV, which is the tabular form anyway and
needs no SDMX library to read.

Which of 122 series is "the mortgage rate"
-------------------------------------------
The dataflow is dimensioned, not named: 122 series distinguished by
`INDEXATION_TYPE` x `IR_FV_TYPE` x `DATA_TYPE` x `BS_ITEM` and four more. Most
are balances in shekels; only 53 rows a month are rates at all (`UNIT_MEASURE`
= PT). The headline this module reports is stated as a filter rather than
hidden in a series code, so it can be argued with:

    INDEXATION_TYPE = NI       unindexed - the majority of new Israeli lending
    DATA_TYPE       = R        the rate itself, not a margin over prime (RM/RB)
    BS_ITEM         = A2C      new loans this month, not the standing book

`BS_ITEM=A2B2` on the same filter reads 6.81% against A2C's 4.79%: one of them
is the rate on the outstanding book, accumulated over years including the 2021
lows and the 2023 highs, and the other is what a bank quoted this month. A2C
tracks the policy rate at a plausible spread, so A2C is the new-lending series.
This is inference from the numbers, not a documented mapping - which is why
**every rate series is stored**, with its dimensions intact. If the reading is
wrong, the fix is a different filter over data already on disk, not a re-import.

What this does not do
---------------------
It does not know this buyer. LTV, term, PTI and whether the household has a
first-home exemption are all personal, and the numbers here are a market
average dressed in the module's defaults (`DEFAULT_LTV`, `DEFAULT_TERM_YEARS`).
A payment shown next to a listing is "what this costs at today's average
terms", which is a comparison between listings - not a quote, and not advice.
"""

from __future__ import annotations

import csv
import io
import json
from legacy.tools.http_client import build_session

#: The one BOI data path that answers. See the module docstring.
BOI_SDMX = "https://edge.boi.gov.il/FusionEdgeServer/sdmx/v2/data/dataflow/BOI.STATISTICS"
BOI_PUBLIC = "https://boi.org.il/PublicApi/GetInterest"

#: Flows imported. `BR` is the policy rate as a daily series back to 1994 -
#: the history the PublicApi endpoint does not give. `BIR_MRTG_99` is the
#: housing-loan statistics.
FLOWS = {
    "BR": "ריבית בנק ישראל",
    "BIR_MRTG_99": "ריביות וביצועים לדיור",
}

#: Only rates are kept. The same dataflow carries loan balances in the
#: billions, and mixing a shekel balance into a table of percentages is how a
#: 9,555,130 lands in a field something later reads as an interest rate.
RATE_UNITS = {"PT"}

#: The filter that defines "the mortgage rate" for everything downstream.
#: Spelled out rather than resolved to a series code so the choice is visible
#: and arguable - see the module docstring for why these three values.
HEADLINE_MORTGAGE = {"indexation": "NI", "data_type": "R", "bs_item": "A2C",
                     "fix_var": "F"}

#: Legs read alongside the headline, as (label, indexation, fix_var, data_type).
#:
#: The unindexed **fixed** rate is the headline because it is the number a bank
#: quotes and the only one that is a rate outright. The variable leg is not
#: published as a rate at all - BOI gives it as `RB` (the base the track sits
#: on) and `RM` (the bank's margin over it), so `RB` is what gets read and it
#: cross-checks: 5.64% against a prime of 5.00% (policy 3.50 + 1.50) plus a
#: measured 0.53 margin. The CPI-indexed leg reads ~1.5 points lower for the
#: obvious reason - the index does the rest of the work, and its 3.25% is not
#: comparable to the 4.74% above without an inflation forecast, which is why
#: nothing downstream computes a payment from it.
MORTGAGE_LEGS = (
    ("fixed_unindexed", "NI", "F", "R"),
    ("variable_unindexed", "NI", "V", "RB"),
    ("fixed_indexed", "CPI", "F", "R"),
)

#: The measured payment-to-income ratio on new housing loans (`DATA_TYPE=PTI`).
#: Read from the same flow rather than assumed, so "the income this listing
#: needs" is scaled by what borrowers are actually being approved at.
PTI_FILTER = {"data_type": "PTI", "bs_item": "A2C"}

#: Terms the payment figures assume. Israeli banks cap LTV at 75% for a
#: first home and 70% for a replacement; 75% and 25 years is the standard
#: illustration, and every figure derived from it says so.
DEFAULT_LTV = 0.75
DEFAULT_TERM_YEARS = 25

#: Share of gross monthly income a lender will let the payment reach. The BOI's
#: own PTI series runs ~24-30% for new loans, and 33% is the supervisory ceiling
#: banks price against. Used only to invert a payment into "the income this
#: listing needs", which is a scale for the reader, not an approval.
MAX_PTI = 0.33

TABLES = """
CREATE TABLE IF NOT EXISTS boi_rates (
    flow        TEXT NOT NULL,     -- BR | BIR_MRTG_99
    series_code TEXT NOT NULL,
    freq        TEXT,              -- D | M
    indexation  TEXT,              -- NI (unindexed) | CPI | IFC_DFC | _T
    fix_var     TEXT,              -- F fixed | V variable | A all
    data_type   TEXT,              -- R rate | RM margin | RB base | PTI
    bs_item     TEXT,              -- A2C new loans | A2B2 outstanding book
    pti_band    TEXT,
    unit        TEXT,              -- PT (percent)
    period      TEXT NOT NULL,     -- YYYY-MM-DD (daily) or YYYY-MM (monthly)
    value       REAL,
    PRIMARY KEY (flow, series_code, period)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_boi_rates_period ON boi_rates(flow, period);
CREATE INDEX IF NOT EXISTS idx_boi_rates_pick
    ON boi_rates(flow, indexation, data_type, bs_item, period);
"""

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


# ---------------------------------------------------------------- ingestion

def _num(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def fetch_flow(flow: str, since: str | None = None, timeout=(20, 240)) -> list[dict]:
    """One BOI dataflow as rows, rates only.

    `since` is an SDMX period bound (`2015-01`). Left None for `BR`, whose
    whole daily history since 1994 is ~8,000 rows and is worth having: a
    payment shown against "the rate five years ago" needs the rate five years
    ago.
    """
    params = {"format": "csv"}
    if since:
        params["c[TIME_PERIOD]"] = f"ge:{since}"
    response = _sess().get(f"{BOI_SDMX}/{flow}", params=params, timeout=timeout)
    response.raise_for_status()
    # An HTML body with HTTP 200 is the WAF, not data. Checked explicitly
    # because csv.DictReader will happily parse a Hebrew error page into one
    # nonsense row rather than fail.
    if not response.text.lstrip().startswith("SERIES_CODE"):
        raise RuntimeError(f"BOI returned a non-CSV body for {flow} "
                           f"({response.status_code}, {len(response.content)} bytes)")

    out = []
    for row in csv.DictReader(io.StringIO(response.text)):
        if row.get("UNIT_MEASURE") not in RATE_UNITS:
            continue
        value = _num(row.get("OBS_VALUE"))
        if value is None:
            continue
        out.append({
            "flow": flow,
            "series_code": row.get("SERIES_CODE"),
            "freq": row.get("FREQ"),
            "indexation": row.get("INDEXATION_TYPE"),
            "fix_var": row.get("IR_FV_TYPE"),
            "data_type": row.get("DATA_TYPE"),
            "bs_item": row.get("BS_ITEM"),
            "pti_band": row.get("PTI"),
            "unit": row.get("UNIT_MEASURE"),
            "period": row.get("TIME_PERIOD"),
            "value": value,
        })
    return out


COLUMNS = ("flow", "series_code", "freq", "indexation", "fix_var", "data_type",
           "bs_item", "pti_band", "unit", "period", "value")


def import_rates(conn, progress=None, should_stop=None, timeout=(20, 240)) -> int:
    """Refresh every flow in FLOWS.

    Upsert rather than replace: BOI revises recent months in place, and a
    dropped-and-rebuilt table would lose the daily policy-rate history the
    moment one request timed out.
    """
    ensure_schema(conn)
    total = 0
    for flow in FLOWS:
        if should_stop and should_stop():
            break
        # The mortgage flow is windowed because its full history is large and
        # nothing here looks further back than the rate cycle; the policy rate
        # is small enough to take whole.
        since = "2015-01" if flow != "BR" else None
        rows = fetch_flow(flow, since=since, timeout=timeout)
        conn.executemany(
            f"INSERT INTO boi_rates ({','.join(COLUMNS)}) "
            f"VALUES ({','.join('?' * len(COLUMNS))}) "
            f"ON CONFLICT(flow, series_code, period) DO UPDATE SET "
            f"value=excluded.value",
            [tuple(row[c] for c in COLUMNS) for row in rows])
        conn.commit()
        total += len(rows)
        if progress:
            progress(f"boi:{flow}", len(rows))
    return total


def policy_rate_live(timeout=(10, 30)) -> dict | None:
    """The Bank of Israel rate right now, from the bank's own public endpoint.

    Read live and not stored: it is one small document, it changes eight times
    a year on dates the same document announces, and a stale copy of *this*
    number would silently mis-price every listing on the screen.
    """
    try:
        payload = _sess().get(BOI_PUBLIC, timeout=timeout).json()
    except (ValueError, OSError):
        return None
    rate = payload.get("currentInterest")
    if rate is None:
        return None
    return {"rate": float(rate),
            "next_decision": payload.get("nextInterestDate"),
            "published_at": payload.get("lastPublishedDate"),
            "source": "בנק ישראל"}


# ------------------------------------------------------------------ reading

def _latest(conn, where: str, params: tuple) -> dict | None:
    row = conn.execute(
        f"SELECT period, value, series_code, fix_var FROM boi_rates "
        f"WHERE {where} ORDER BY period DESC LIMIT 1", params).fetchone()
    if not row:
        return None
    return {"period": row["period"], "rate": round(float(row["value"]), 2),
            "series": row["series_code"], "fix_var": row["fix_var"]}


def _latest_leg(conn, indexation, fix_var, data_type) -> dict | None:
    """The freshest observation for one mortgage track.

    Ordered by period first, with the `P01` band only as a tie-break, because
    BOI retires bands without notice: the `fix_var='A'` aggregate and the
    `P01` variable series both stop at 2024-01 while `P07` runs to last month.
    Selecting a band by name returned a two-year-old number as "today's rate".
    """
    row = conn.execute(
        "SELECT period, value, series_code, pti_band FROM boi_rates "
        "WHERE flow = 'BIR_MRTG_99' AND bs_item = ? AND indexation = ? "
        "  AND fix_var = ? AND data_type = ? AND value > 0 "
        "ORDER BY period DESC, CASE pti_band WHEN 'P01' THEN 0 ELSE 1 END "
        "LIMIT 1",
        (HEADLINE_MORTGAGE["bs_item"], indexation, fix_var, data_type)).fetchone()
    if not row:
        return None
    return {"period": row["period"], "rate": round(float(row["value"]), 2),
            "series": row["series_code"], "band": row["pti_band"]}


def mortgage_rate(conn) -> dict | None:
    """The rate a bank is charging on a new housing loan, by track.

    The headline is the unindexed fixed rate: it is the number quoted to a
    borrower and the only leg BOI publishes as a rate outright. The other
    tracks come back beside it rather than averaged into it - an unindexed
    4.74% and a CPI-indexed 3.25% are not two measurements of one thing, and
    their mean describes no loan anybody can actually take.
    """
    legs = {}
    for label, indexation, fix_var, data_type in MORTGAGE_LEGS:
        got = _latest_leg(conn, indexation, fix_var, data_type)
        if got:
            legs[label] = got
    headline = legs.get("fixed_unindexed")
    if not headline:
        return None
    pti = conn.execute(
        "SELECT period, value FROM boi_rates WHERE flow = 'BIR_MRTG_99' "
        "AND data_type = ? AND bs_item = ? AND value > 0 "
        "ORDER BY period DESC LIMIT 1",
        (PTI_FILTER["data_type"], PTI_FILTER["bs_item"])).fetchone()
    return {
        "rate": headline["rate"],
        "period": headline["period"],
        "legs": {k: {"rate": v["rate"], "period": v["period"]}
                 for k, v in legs.items()},
        # The measured payment-to-income ratio on new loans, so "the income
        # this listing needs" is scaled by what borrowers are approved at
        # rather than by the supervisory ceiling.
        "pti_actual": round(float(pti["value"]), 1) if pti else None,
        "basis": "ריבית ממוצעת על משכנתאות חדשות, לא צמודה, קבועה (בנק ישראל)",
        "filter": dict(HEADLINE_MORTGAGE),
    }


def policy_rate_series(conn, since: str = "2020-01-01") -> list[dict]:
    """Monthly policy-rate path, for a chart rather than a single number."""
    rows = conn.execute(
        "SELECT substr(period, 1, 7) month, AVG(value) rate FROM boi_rates "
        "WHERE flow = 'BR' AND period >= ? GROUP BY month ORDER BY month",
        (since,)).fetchall()
    return [{"month": r["month"], "rate": round(float(r["rate"]), 2)} for r in rows]


def counts(conn) -> dict:
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT series_code) series, "
            "MAX(period) last FROM boi_rates").fetchone()
    except Exception:
        return {"observations": 0, "series": 0, "last_period": None}
    return {"observations": row["n"], "series": row["series"],
            "last_period": row["last"]}


# ------------------------------------------------------------------ the maths

def monthly_payment(principal: float, annual_rate_pct: float,
                    years: int = DEFAULT_TERM_YEARS) -> float | None:
    """Level annuity payment - the standard mortgage formula (שפיצר).

        P * r / (1 - (1 + r)^-n),  r = monthly rate, n = months

    A zero rate is not a degenerate case to guard against on principle: BOI
    genuinely reports 0 for tracks with no new lending in a month, and dividing
    by the discount factor would blow up. It falls back to straight-line, which
    is what a 0% loan actually is.
    """
    try:
        principal = float(principal)
        rate = float(annual_rate_pct) / 100.0 / 12.0
        months = int(years) * 12
    except (TypeError, ValueError):
        return None
    if principal <= 0 or months <= 0:
        return None
    if rate <= 0:
        return round(principal / months, 2)
    factor = (1 + rate) ** -months
    return round(principal * rate / (1 - factor), 2)


def affordability(price, annual_rate_pct, *, ltv: float = DEFAULT_LTV,
                  years: int = DEFAULT_TERM_YEARS, pti: float | None = None) -> dict | None:
    """What one listing costs per month at today's average terms.

    Everything here is arithmetic on two inputs - the asking price and the
    market rate - and the assumptions are returned with the answer so a reader
    can see they are assumptions.
    """
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0 or annual_rate_pct is None:
        return None
    loan = price * ltv
    payment = monthly_payment(loan, annual_rate_pct, years)
    if payment is None:
        return None
    # The measured ratio when the caller has it, the supervisory ceiling when
    # it does not. Both are stated back, because the two answer different
    # questions - "what households like this are carrying" and "the most a
    # bank will let them carry".
    burden = (pti / 100.0) if pti else MAX_PTI
    return {
        "price": round(price),
        "down_payment": round(price - loan),
        "loan": round(loan),
        "monthly_payment": round(payment),
        # Total paid over the term minus the principal: the cost of the money,
        # which is the number the monthly figure hides.
        "total_interest": round(payment * years * 12 - loan),
        "income_needed": round(payment / burden),
        "pti_used": round(burden * 100, 1),
        "rate": round(float(annual_rate_pct), 2),
        "ltv": ltv,
        "years": years,
    }


def snapshot(conn) -> dict:
    """Everything the finance layer knows, in one document for the UI."""
    mortgage = mortgage_rate(conn)
    policy = policy_rate_live()
    if policy is None:
        # The live endpoint is the better source, but a stored daily series
        # means an outage degrades to "yesterday's rate", not to nothing.
        stored = _latest(conn, "flow = 'BR'", ())
        if stored:
            policy = {"rate": stored["rate"], "as_of": stored["period"],
                      "source": "בנק ישראל (מאגר מקומי)"}
    out = {"policy": policy, "mortgage": mortgage, "counts": counts(conn),
           "terms": {"ltv": DEFAULT_LTV, "years": DEFAULT_TERM_YEARS,
                     "max_pti": MAX_PTI}}
    if mortgage and policy:
        # The spread is the bank's, and it is the part a borrower can shop for.
        out["spread"] = round(mortgage["rate"] - policy["rate"], 2)
    if mortgage:
        # One worked example at the national median asking price, so the page
        # can show what the rate *means* without waiting for a listing.
        out["example"] = affordability(2_000_000, mortgage["rate"],
                                       pti=mortgage.get("pti_actual"))
    return out


if __name__ == "__main__":                       # pragma: no cover - CLI
    import db

    conn = db.get_conn()
    try:
        n = import_rates(conn, progress=lambda k, c: print(f"  {k}: {c}"))
        print(f"imported {n} rate observations")
        print(json.dumps(snapshot(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
