"""Controlled checks of the lead maths, independent of live data.

Run: ``python verify_lead_intel.py`` - exits non-zero on any failure.

These use synthetic histories rather than the store on purpose. The live feed
currently has a five-day observation window, so it cannot exercise "cut twice
over six months" at all; every threshold in ``lead_intel`` would otherwise go
unverified until enough calendar time had passed to break it in front of a
user. The named cases also pin the *product* decisions - that an observed date
is a floor and never presented as a true age, that a 1% move is noise rather
than a price cut, and that a ₪10/m² row is a broken record rather than a
bargain.
"""
import sys
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
import lead_intel as li

fails = []


def check(name, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name, "|", got, "" if ok else f"(want {want})")
    if not ok:
        fails.append(name)


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# --- market_age: published beats observed, and observed is a floor -----------
a = li.market_age(listed_at=ago(100), first_seen=ago(5))
check("published age wins over first_seen", (a["days_on_market"], a["age_basis"]),
      (100, "published"))
check("published is not a minimum", a["age_is_minimum"], False)

a = li.market_age(first_seen=ago(40))
check("observed falls back", (a["days_on_market"], a["age_basis"]), (40, "observed"))
check("observed IS flagged a minimum", a["age_is_minimum"], True)

a = li.market_age()
check("no dates -> unknown", a["age_basis"], "unknown")

check("age_band 10d", li.age_band(10), "טרי")
check("age_band 60d", li.age_band(60), "מתבשל")
check("age_band 120d", li.age_band(120), "תקוע")
check("age_band 400d", li.age_band(400), "תקוע מאוד")
check("age_band None", li.age_band(None), "לא ידוע")

# --- price_movement ---------------------------------------------------------
m = li.price_movement([(2_000_000, ago(60))])
check("single observation = no change", m["price_changes"], 0)

m = li.price_movement([(2_000_000, ago(60)), (1_800_000, ago(10))])
check("one 10% cut counted", m["price_changes"], 1)
check("total change pct", m["total_change_pct"], -10.0)
check("days since change", m["days_since_change"], 10)
check("direction", m["direction"], "ירידת מחיר")

m = li.price_movement([(2_000_000, ago(90)), (1_900_000, ago(50)),
                       (1_700_000, ago(5))])
check("two cuts counted", m["price_changes"], 2)
check("total from first to last", m["total_change_pct"], -15.0)
check("largest single cut", m["largest_cut_pct"], -10.5)

m = li.price_movement([(1_800_000, ago(30)), (2_000_000, ago(2))])
check("price rise detected", m["direction"], "עליית מחיר")

# out-of-order input must still read chronologically
m = li.price_movement([(1_800_000, ago(10)), (2_000_000, ago(60))])
check("unordered history sorted", m["total_change_pct"], -10.0)

# --- scoring ----------------------------------------------------------------
fresh = li.market_age(first_seen=ago(3))
none_move = li.price_movement([(2_000_000, ago(3))])
s = li.score_lead(age=fresh, movement=none_move, is_private=False)
check("fresh agency ad scores low", s["lead_score"] < 15, True)

old = li.market_age(listed_at=ago(200))
cut = li.price_movement([(3_000_000, ago(200)), (2_800_000, ago(90)),
                         (2_550_000, ago(10))])
s = li.score_lead(age=old, movement=cut, discount_pct=12, is_private=True, rooms=5)
check("motivated seller scores high", s["lead_score"] > 70, True)
check("every signal is explained",
      all(sig.get("text") for sig in s["signals"]), True)
print("   signals:", [sig["signal"] for sig in s["signals"]])

k = li.classify_lead(age=old, movement=cut, discount_pct=12, is_private=True,
                     rooms=5)
check("long + cut = pressured seller", k["lead_type"], "לחוץ למכור")

k = li.classify_lead(age=li.market_age(first_seen=ago(5)),
                     movement=none_move, is_private=True)
check("new listing = fresh lead", k["lead_type"], "ליד טרי")

k = li.classify_lead(age=li.market_age(listed_at=ago(60)),
                     movement=li.price_movement([(3_000_000, ago(60)),
                                                 (2_820_000, ago(5))]),
                     rooms=5.0)
check("big home + cut = selling to buy", k["lead_type"], "מוכר כדי לקנות")

k = li.classify_lead(age=li.market_age(listed_at=ago(60)),
                     movement=none_move, discount_pct=15)
check("underpriced + time = investment", k["lead_type"], "הזדמנות השקעה")

# a 1% "cut" is noise, not a decision
k = li.classify_lead(age=old, movement=li.price_movement(
    [(3_000_000, ago(200)), (2_970_000, ago(5))]))
check("1% move is not a cut", k["lead_type"], "תקוע בשוק")

# --- plausibility -----------------------------------------------------------
check("10 ILS/m2 rejected",
      bool(li.implausible({"price": 500_000, "area_m2": 50, "price_per_m2": 10})), True)
check("1 m2 area rejected",
      bool(li.implausible({"area_m2": 1.0})), True)
check("normal flat accepted",
      li.implausible({"price": 2_000_000, "area_m2": 90, "price_per_m2": 22_222}), None)
check("missing fields accepted", li.implausible({}), None)

# --- population-level rules, via the real scorer -----------------------------
# These pin two decisions that are invisible in the unit maths but decide what
# an agent sees first: a row with no comparables must not report a 0% gap, and
# a gap wide enough to mean "part-share sale" must be flagged, not celebrated.
rows = li._score_all([
    # alone in its city: nothing to compare against
    {"listing_key": "t:1", "source": "T", "external_id": "1", "url": None,
     "locality": "עיר בודדה", "neighborhood": None, "street": "א",
     "price": 2_000_000, "rooms": 4, "area_m2": 90, "price_per_m2": 22_222,
     "agency": None, "listed_at": None, "first_seen": ago(10),
     "last_seen": ago(1), "history": []},
    # four normal rows plus one priced at a fifth of them
    *[{"listing_key": f"t:{i}", "source": "T", "external_id": str(i),
       "url": None, "locality": "עיר", "neighborhood": "שכונה", "street": "ב",
       "price": 2_000_000, "rooms": 4, "area_m2": 80, "price_per_m2": 25_000,
       "agency": "משרד", "listed_at": None, "first_seen": ago(10),
       "last_seen": ago(1), "history": []} for i in range(2, 6)],
    {"listing_key": "t:9", "source": "T", "external_id": "9", "url": None,
     "locality": "עיר", "neighborhood": "שכונה", "street": "ג",
     "price": 1_000_000, "rooms": 4, "area_m2": 200, "price_per_m2": 5_000,
     "agency": None, "listed_at": None, "first_seen": ago(10),
     "last_seen": ago(1), "history": []},
], {})
by_key = {r["listing_key"]: r for r in rows}

check("alone in its city -> no gap claimed", by_key["t:1"]["discount_pct"], None)
# The row counts itself, as it does in external_listings_view - so the count is
# 1, not 0. What matters is that a sample of 1 yields no gap: a median over one
# row is that row, and reporting the resulting 0% would read as "priced exactly
# at market" when it means "nothing to compare against".
check("sample of one is reported honestly",
      by_key["t:1"]["comparable_count"], 1)
check("five comparables DO produce a gap",
      by_key["t:9"]["discount_pct"] is not None, True)
check("80% gap flagged, not celebrated",
      "חריג" in by_key["t:9"]["data_quality"], True)
check("normal row stays clean", by_key["t:2"]["data_quality"], "תקין")

# --- brokerage: three states, and silence is never "private" -----------------
# The regression this pins cost two wrong implementations. "ללא תיווך" is a
# call list; claiming it for rows whose source never published the field sends
# an agent to phone owners who have an exclusive agreement.
check("no payload -> unknown", li._legacy_brokerage(None), "unknown")
check("empty payload -> unknown", li._legacy_brokerage("{}"), "unknown")
check("card HTML with no broker info -> unknown",
      li._legacy_brokerage('<div class="card" data-id="1">3 חדרים</div>'),
      "unknown")
check("explicit broker -> agency",
      li._legacy_brokerage('<div>תיווך מעלות</div>'), "agency")
check("explicit private -> private",
      li._legacy_brokerage('<div>מודעה פרטי, ללא תיווך</div>'), "private")
# "ללא תיווך" CONTAINS "תיווך". Matching the agency pattern first classified an
# ad that says "no broker" as brokered - exactly inverted.
check('"ללא תיווך" is not read as brokered',
      li._legacy_brokerage("<div>ללא תיווך</div>"), "private")
check('"אין תיווך" likewise',
      li._legacy_brokerage("<div>אין תיווך</div>"), "private")
check("denial plus a named agency resolves to agency",
      li._legacy_brokerage("<div>ללא תיווך — סוכנות רימקס</div>"), "agency")

rows = li._score_all([
    {"listing_key": "b:1", "source": "Yad2", "external_id": "1", "url": None,
     "locality": "עיר", "neighborhood": None, "street": "א", "price": 2_000_000,
     "rooms": 4, "area_m2": 90, "price_per_m2": 22_222, "agency": "רימקס",
     "brokerage": "agency", "listed_at": None, "first_seen": ago(10),
     "last_seen": ago(1), "history": []},
    {"listing_key": "b:2", "source": "ad", "external_id": "2", "url": None,
     "locality": "עיר", "neighborhood": None, "street": "ב", "price": 2_000_000,
     "rooms": 4, "area_m2": 90, "price_per_m2": 22_222, "agency": None,
     "brokerage": "unknown", "listed_at": None, "first_seen": ago(60),
     "last_seen": ago(1), "history": []},
], {})
by_key = {r["listing_key"]: r for r in rows}
check("stated agency reads as verified",
      by_key["b:1"]["brokerage_confidence"].startswith("מאומת"), True)
check("unknown is labelled unknown", by_key["b:2"]["brokerage"], "לא ידוע")
check("unknown does NOT earn the private-seller signal",
      any(s["signal"] == "private_seller" for s in by_key["b:2"]["signals"]),
      False)

# --- image-derived dates: the field that made this feature possible ----------
import yad2_feed as yf

check("image date from the URL path",
      yf.image_date("https://img.yad2.co.il/Pic/202604/19/2_5/o/y2_9_x_20260419.jpg"),
      "2026-04-19")
check("image date from a timestamped filename",
      yf.image_date("https://img.yad2.co.il/Pic/202605/29/2_5/o/y2_1pa_1_20260529160631.jpeg"),
      "2026-05-29")
# A digit run that is not a date must not become a listing age.
check("impossible month rejected",
      yf.image_date("https://img.yad2.co.il/Pic/202699/45/x/y2_a.jpg"), None)
check("no URL -> no date", yf.image_date(None), None)

# Priority order, and the fact that only `published` is exact.
a = li.market_age(listed_at=ago(100), image_date=ago(500), first_seen=ago(2))
check("seller's date outranks the image", a["age_basis"], "published")
a = li.market_age(image_date=ago(500), first_seen=ago(2))
check("image date outranks first_seen", (a["age_basis"], a["days_on_market"]),
      ("image", 500))
check("image-derived age is a minimum", a["age_is_minimum"], True)
a = li.market_age(first_seen=ago(2))
check("first_seen is the last resort", a["age_basis"], "observed")

# --- valuation: never invent a verdict from a thin sample -------------------
import valuation as val

r = val.assess(locality="ישוב שלא קיים בכלל", price=2_000_000)
check("unknown locality declines", r["available"], False)
r = val.assess(locality="תל אביב יפו", price=None)
check("no price declines", r["available"], False)
r = val.assess(locality="תל אביב יפו", price=3_000_000, property_type="דירה")
if r["available"]:
    check("verdict carries its sample size", r["deals"] >= val.MIN_DEALS, True)
    check("verdict carries its adjustment date", bool(r["adjusted_to"]), True)
    check("percentile is a percentage", 0 <= r["percentile"] <= 100, True)
    check("band is ordered", r["range_p25"] <= r["median_paid"] <= r["range_p75"], True)
else:
    print("SKIP valuation checks — no transaction register available")

# --- the two market views disagreeing ----------------------------------------
# A listing cheap against local *asking* prices but expensive against concluded
# deals must not be sold to a client as a bargain. 1,729 live rows hit this.
rows = li._score_all([
    *[{"listing_key": f"d:{i}", "source": "T", "external_id": str(i),
       "locality": "תל אביב יפו", "neighborhood": "שכונה", "street": "א",
       "price": 9_000_000, "rooms": 4, "area_m2": 100, "price_per_m2": 90_000,
       "agency": "משרד", "brokerage": "agency", "property_type": "דירה",
       "first_seen": ago(200), "history": []} for i in range(1, 6)],
    # Same area, priced well under those asks - but still high vs real deals.
    {"listing_key": "d:9", "source": "T", "external_id": "9",
     "locality": "תל אביב יפו", "neighborhood": "שכונה", "street": "ב",
     "price": 6_000_000, "rooms": 4, "area_m2": 100, "price_per_m2": 60_000,
     "agency": "משרד", "brokerage": "agency", "property_type": "דירה",
     "first_seen": ago(200), "history": []},
], {})
row = {r["listing_key"]: r for r in rows}["d:9"]
if row["paid_available"]:
    check("cheap-vs-asking but high-vs-paid is reclassified",
          row["lead_type"], "זול מול המבוקש, יקר מול העסקאות")
    check("...and the contradiction is spelled out",
          any(s["signal"] == "asking_above_paid" for s in row["signals"]), True)
    check("...and it cannot outrank a genuine find",
          row["lead_score"] <= 40.0, True)
else:
    print("SKIP contradiction checks — no transaction register available")

# --- private sellers, stated by the source rather than inferred --------------
# Only reachable since the harvest moved to Yad2's real search feed; the
# promoted-ads carousel labelled every row "commercial".
rows = li._score_all([
    {"listing_key": "p:1", "source": "Yad2", "external_id": "1",
     "locality": "עיר", "street": "א", "price": 2_000_000, "rooms": 4,
     "area_m2": 90, "price_per_m2": 22_222, "agency": None,
     "brokerage": "private", "brokerage_verified": True,
     "first_seen": ago(40), "history": []},
    {"listing_key": "p:2", "source": "ad", "external_id": "2",
     "locality": "עיר", "street": "ב", "price": 2_000_000, "rooms": 4,
     "area_m2": 90, "price_per_m2": 22_222, "agency": None,
     "brokerage": "private", "brokerage_verified": False,
     "first_seen": ago(40), "history": []},
], {})
by_key = {r["listing_key"]: r for r in rows}
check("source-stated private reads as verified",
      by_key["p:1"]["brokerage_confidence"].startswith("מאומת"), True)
check("inferred private still reads as derived",
      by_key["p:2"]["brokerage_confidence"], "נגזר מהמקור")
check("both are still 'ללא תיווך'",
      (by_key["p:1"]["brokerage"], by_key["p:2"]["brokerage"]),
      ("ללא תיווך", "ללא תיווך"))
check("a private seller earns the private-seller signal",
      any(s["signal"] == "private_seller" for s in by_key["p:1"]["signals"]),
      True)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"))
sys.exit(1 if fails else 0)
