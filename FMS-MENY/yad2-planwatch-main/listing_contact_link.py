"""Carry a published phone number across boards, for the same property.

The gap this closes
-------------------
Yad2 holds 61,478 for-sale listings and publishes no contact field at all - not
in the feed, and its ad page is behind a bot wall that does not clear. ONMAP
holds 2,839 and now publishes a phone on 2,829 of them, because the number sits
on its per-property record rather than in its search feed.

Where the two boards carry the *same flat*, the advertiser has already
published their number against that flat - on ONMAP, under their own name. This
module reads it there and attaches it to the Yad2 row for the same property. No
new collection happens: nothing is fetched, nothing is guessed, and the only
thing added is a pointer from one row to a number the same advertiser put on
the other.

Why the key is this strict
--------------------------
The match key is city + street + house number + rooms + area. Address alone is
a *building*, and a building is exactly where this would do damage: attaching
the fourth-floor owner's phone to the second-floor owner's ad produces a lead
that is confidently wrong, which is worse than an empty column. Rooms and area
narrow a building down to a unit.

The remaining ambiguity is handled by refusing it. A key that resolves to more
than one distinct phone is dropped rather than resolved by picking one -
measured on the current stores, 240 keys match and **none** are ambiguous, so
the strictness costs nothing today and is the whole safety margin if a future
harvest makes two flats collide.

Provenance travels with the number. Every row this touches gets `phone_source`
and `phone_basis` saying where it came from, so a number that Yad2 never
published is never displayed as though Yad2 published it.
"""

from __future__ import annotations

import sqlite3

from listing_seller import _norm

#: Rooms are rounded to one decimal and area to the nearest square metre before
#: they enter the key. The boards agree on the flat but not on its precision -
#: 3.5 rooms is written `3.5` and `3.50`, and an area is `82` on one board and
#: `82.0` on the other. Comparing the raw values would miss real matches.
def unit_key(row) -> tuple | None:
    city = _norm(row.get("city") or row.get("locality"))
    street = _norm(row.get("street"))
    house = _norm(row.get("house_number"))
    rooms = row.get("rooms")
    area = row.get("sqm") if row.get("sqm") is not None else row.get("area_sqm")
    if not (city and street and house) or rooms in (None, "") or area in (None, ""):
        return None
    try:
        return (city, street, house, round(float(rooms), 1), round(float(area)))
    except (TypeError, ValueError):
        return None


def phone_index(conn: sqlite3.Connection) -> dict:
    """Map a unit to the phone ONMAP publishes for it.

    A unit whose key carries two different phones is left out entirely; see the
    module header for why picking one would be the wrong kind of helpful.
    """
    try:
        rows = conn.execute(
            """SELECT city, street, house_number, rooms, area_sqm, phone,
                      contact_name
                 FROM onmap_listings
                WHERE phone IS NOT NULL AND phone <> ''
                  AND delisted_at IS NULL""").fetchall()
    except sqlite3.Error:
        return {}

    seen: dict[tuple, dict] = {}
    ambiguous: set = set()
    for row in rows:
        key = unit_key(dict(row))
        if not key:
            continue
        phone = str(row["phone"]).strip()
        held = seen.get(key)
        if held is None:
            seen[key] = {"phone": phone,
                         "contact_name": (row["contact_name"] or "").strip()}
        elif held["phone"] != phone:
            ambiguous.add(key)
    for key in ambiguous:
        seen.pop(key, None)
    return seen


def annotate(rows: list[dict], index: dict) -> dict:
    """Stamp the borrowed phone onto rows that have none of their own.

    A row that already carries a phone keeps it. The source board's own number
    is first-hand and always wins over one matched across boards.
    """
    if not index:
        return {"linked": 0, "candidates": 0}

    linked = candidates = 0
    for row in rows:
        if row.get("phone"):
            continue
        key = unit_key(row)
        if not key:
            continue
        candidates += 1
        hit = index.get(key)
        if not hit:
            continue
        row["phone"] = hit["phone"]
        row["phone_source"] = "onmap"
        name = hit.get("contact_name")
        if name:
            row["contact_name"] = name
        row["phone_basis"] = (
            "מספר שהמפרסם פרסם לאותו נכס באתר ONMAP"
            + (f" · {name}" if name else ""))
        linked += 1
    return {"linked": linked, "candidates": candidates}
