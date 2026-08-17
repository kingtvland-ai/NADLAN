"""Who is selling, and how much are they selling at once.

The question this answers
------------------------
Two identical flats at the same price are not the same lead. One is a family
moving house; the other is the fourth unit the same owner has put on the board
this year, or one of five flats for sale in a building where something is
going on. The second is negotiable in a way the first is not, and nothing in a
price comparison can see the difference.

Phone numbers: what changed
---------------------------
This module was written when phone identity was not available and says so at
length. That is no longer the situation, and the note is kept because the
reason it changed matters.

The original measurement was taken against ONMAP's **search feed**, which
carries a number on 36 of 2,839 rows - all of them `entityType: "project"`.
The per-property record is a different endpoint, and it answers with a number
for nearly every listing. Sweeping it brought coverage to **2,829 of 2,839**,
across 648 distinct numbers, 275 of which appear on more than one listing.
The earlier conclusion was not wrong about the data it saw; it was wrong about
which endpoint held the data.

Two of the original limits still stand, and still shape this file:

* **Yad2** publishes no contact field in its feed (40 live rows scanned, zero),
  and its ad page is behind a bot wall that does not clear. Its rows get a
  number only where the same unit is advertised on ONMAP too - 240 of 61,478.
  See `listing_contact_link`.
* **Photos** would have been a fingerprint, but Yad2 re-uploads a fresh file
  per ad: across 355,934 photo URLs, **not one** appears on two listings.

So phone identity now carries real weight where it exists, and the building
signal below still carries the rest - it is the only one that covers the whole
corpus rather than the 5% of it ONMAP represents.

What does work at scale: the building
-------------------------------------
2,474 exact addresses carry more than one live listing, covering 6,180 of them;
**957 addresses carry more than one *private* listing**, covering 2,158. A
building with four flats on the board at once is a fact about that building -
a developer selling stock, an owner group exiting, or a block people are
leaving - and it is visible without knowing anyone's name.

It also enables the tightest price comparison in the whole system. Everything
else compares a flat to *similar* flats; here the comparison is to the flats
next door in the same stairwell, where location, age, and building quality are
not similar but identical.
"""

from __future__ import annotations

import statistics
from collections import defaultdict


#: Punctuation that varies between boards for the same place: Yad2 writes
#: "תל אביב יפו" where ONMAP writes "תל אביב-יפו", and street names differ on
#: hyphens, geresh and quotes. Stripping it is what lets a Yad2 flat and an
#: ONMAP flat in the same building recognise each other.
_PUNCT = str.maketrans({c: None for c in "-־'\"״׳,."})

#: ONMAP returns at most one page of an advertiser's other properties, and a
#: page is 25. Any count at this value is a floor, not a total.
PUBLISHED_PORTFOLIO_CAP = 25


def _norm(value) -> str:
    return " ".join(str(value or "").translate(_PUNCT).split()).strip().lower()


def address_key(row) -> tuple | None:
    """One building. All three parts required - `city + street` alone is a
    street, and treating a street as a building would group hundreds of
    unrelated flats into a fake 'concentration'."""
    city, street = _norm(row.get("city") or row.get("locality")), _norm(row.get("street"))
    house = _norm(row.get("house_number"))
    return (city, street, house) if city and street and house else None


#: Identifiers that name an actual *person or entity* selling. An agency is
#: deliberately **not** here: "this ad is handled by an office that has 372
#: other ads" is a fact about a brokerage, not about an owner with a portfolio,
#: and folding the two together would report 50,044 of 61,478 listings as
#: "seller has more than one property" - true of the office, meaningless as a
#: lead signal. Agency size is reported separately, as `agency_listings`.
_IDENTITY = (
    ("טלפון", lambda r: _norm(r.get("phone")) or None),
    ("מזהה מפרסם", lambda r: _norm(r.get("owner_id")) or None),
)


def seller_key(row):
    for label, extract in _IDENTITY:
        value = extract(row)
        if value:
            return label, value
    return None, None


def annotate(rows: list[dict]) -> list[dict]:
    """Attach building concentration and seller portfolio size to every row."""
    buildings: dict[tuple, list] = defaultdict(list)
    sellers: dict[tuple, list] = defaultdict(list)
    agencies: dict[str, int] = defaultdict(int)
    for row in rows:
        key = address_key(row)
        if key:
            buildings[key].append(row)
        label, value = seller_key(row)
        if value:
            sellers[(label, value)].append(row)
        agency = _norm(row.get("agency"))
        if agency:
            agencies[agency] += 1

    for key, group in buildings.items():
        # ₪/m² is the only fair ordering inside a building, where units differ
        # in size but nothing else.
        priced = sorted((r for r in group if r.get("price_per_m2")),
                        key=lambda r: float(r["price_per_m2"]))
        median = (statistics.median([float(r["price_per_m2"]) for r in priced])
                  if priced else None)
        private = sum(1 for r in group if r.get("brokerage") == "ללא תיווך")
        for row in group:
            row["address_listings"] = len(group)
            row["address_private_listings"] = private
            if median and row.get("price_per_m2"):
                # The tightest comparison available anywhere in this system:
                # not "similar flats" but the flats in the same stairwell,
                # where age, location and build quality are identical rather
                # than merely close.
                row["address_gap_pct"] = round(
                    (median - float(row["price_per_m2"])) / median * 100, 1)
            if len(priced) > 1:
                # Identity, not equality: two flats in one building can have
                # every field equal, and `list.index` would then rank both as
                # the first and call both the cheapest.
                for position, candidate in enumerate(priced, start=1):
                    if candidate is row:
                        row["address_rank"] = position
                        row["address_rank_of"] = len(priced)
                        row["address_cheapest"] = position == 1
                        break
            if len(group) > 1:
                row["building_basis"] = (
                    f"{len(group)} מודעות באותה כתובת"
                    + (f" · {private} ללא תיווך" if private else ""))

    for row in rows:
        agency = _norm(row.get("agency"))
        if agency:
            row["agency_listings"] = agencies[agency]

    for (label, value), group in sellers.items():
        for row in group:
            row["seller_listings"] = len(group)
            row["seller_scope"] = label
            if len(group) > 1:
                cities = sorted({str(r.get("city") or r.get("locality") or "")
                                 for r in group if r.get("city") or r.get("locality")})
                row["seller_basis"] = (
                    f"{len(group)} נכסים לאותו {label}"
                    + (f" · {len(cities)} ישובים" if len(cities) > 1 else ""))

    # ONMAP also answers the portfolio question, but not as completely as it
    # looks. `owner_property_count` is `len(properties_by_owner)`, and that list
    # is one page: its maximum across all 2,839 rows is exactly 25, and 1,676
    # rows sit on that value. It is a censored count, not a total - the four
    # largest advertisers report "25" while our own store holds 81, 69, 63 and
    # 48 of their listings.
    #
    # So the two counts are combined rather than one overriding the other: take
    # whichever is larger, and when the published figure is at the ceiling say
    # "25+" instead of "25", so a number that is really a truncation is never
    # printed as a measurement.
    for row in rows:
        published = row.get("owner_property_count")
        if not published:
            continue
        censored = published >= PUBLISHED_PORTFOLIO_CAP
        held = row.get("seller_listings") or 0
        if censored:
            row["seller_portfolio_censored"] = True
        if published <= held:
            # We hold more than the source would admit to; our own count is the
            # better floor and the basis line already describes it.
            continue
        row["seller_listings"] = published
        row["seller_scope"] = "מפרסם (לפי המקור)"
        shown = f"{published}+" if censored else str(published)
        row["seller_basis"] = (
            f"{shown} נכסים לאותו מפרסם — לפי ONMAP"
            + (" (הרשימה נחתכת ב-25, ייתכן שיש יותר)" if censored else "")
            + (f" · {row['contact_name']}" if row.get("contact_name") else ""))
    return rows


def coverage(rows) -> dict:
    return {
        "rows": len(rows),
        "in_multi_listing_building": sum(1 for r in rows
                                         if (r.get("address_listings") or 0) > 1),
        "private_cluster": sum(1 for r in rows
                               if (r.get("address_private_listings") or 0) > 1),
        "with_seller_identity": sum(1 for r in rows if r.get("seller_listings")),
        "seller_portfolio": sum(1 for r in rows if (r.get("seller_listings") or 0) > 1),
        "with_agency_size": sum(1 for r in rows if r.get("agency_listings")),
    }
