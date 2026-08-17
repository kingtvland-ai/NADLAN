"""Adapter for a partner listing export whose columns differ from PlanWatch's.

The first partner file we received used these headers::

    external_id, title, property_type, city, address, rooms, floor, sqm,
    price, description

Three of them do not match ``market_client.LISTING_FIELDS``: ``city`` is
``locality``, ``sqm`` is ``area_m2``, and ``title``/``description`` have no
column at all (they are kept in ``raw_json`` instead of being dropped).
``property_type`` arrives as an English slug that this module maps onto the
Hebrew vocabulary the rest of the system already uses.

Usage:
    python partner_feed_adapter.py feed.csv --source yad2-partner
    python partner_feed_adapter.py feed.csv --source yad2-partner --dry-run
    python partner_feed_adapter.py feed.json --source broker-crm
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

import db
import market_client


#: Partner header -> PlanWatch field. Anything already named correctly passes
#: through untouched, so a feed that fixes its headers keeps working.
COLUMN_ALIASES = {
    "city": "locality",
    "town": "locality",
    "yishuv": "locality",
    "sqm": "area_m2",
    "size": "area_m2",
    "area": "area_m2",
    "square_meters": "area_m2",
    "meters": "area_m2",
    "street": "address",
    "full_address": "address",
    "id": "external_id",
    "listing_id": "external_id",
    "link": "url",
    "rooms_count": "rooms",
    "floor_number": "floor",
    "floors": "total_floors",
    "build_year": "year_built",
    "construction_year": "year_built",
    "asking_price": "price",
    "published_at": "listed_at",
    "date": "listed_at",
}

#: English slugs the partner uses -> the Hebrew vocabulary already stored in
#: `transactions.asset_type`, so both sides of the market speak one language.
PROPERTY_TYPES = {
    "apartment": "דירה",
    "flat": "דירה",
    "penthouse": "מיני פנטהאוז",
    "garden_apartment": "דירת גן",
    "roof_apartment": "דירת גג",
    "duplex": "דופלקס",
    "cottage": "קוטג' חד משפחתי",
    "semi_detached": "קוטג' דו משפחתי",
    "townhouse": "קוטג' טורי",
    "house": "בית בודד",
    "villa": "חד משפחתי (וילה)",
    "land": "קרקע למגורים",
    "office": "משרד",
    "shop": "חנות",
    "storage": "מחסנים",
    "parking": "חניה",
}

#: Columns worth keeping even though PlanWatch has no field for them. They end
#: up inside raw_json via market_client, which stores the whole original row.
PASSTHROUGH = ("title", "description", "notes", "agent", "phone")


def _clean_number(value):
    """Strip currency symbols, thousands separators and stray text."""
    if value in (None, ""):
        return None
    text = re.sub(r"[^\d.\-]", "", str(value))
    return text or None


def normalise_row(row: dict) -> dict:
    """Map one partner row onto PlanWatch's listing fields."""
    out = {}
    for key, value in row.items():
        if key is None:
            continue
        field = COLUMN_ALIASES.get(str(key).strip().lower(), str(key).strip())
        if value in (None, ""):
            continue
        if field in ("price", "area_m2", "rooms", "price_per_m2", "lat", "lon"):
            value = _clean_number(value)
        elif field in ("floor", "total_floors", "year_built"):
            value = _clean_number(value)
        elif field == "property_type":
            slug = str(value).strip().lower()
            value = PROPERTY_TYPES.get(slug, value)
        out[field] = value

    # Keep the descriptive columns so they survive into raw_json.
    for name in PASSTHROUGH:
        if row.get(name):
            out.setdefault(name, row[name])
    return out


def _read(path: Path) -> list[dict]:
    """Read CSV or JSON, refusing a file whose Hebrew has been corrupted.

    A file that lost bytes in transit (mojibake) would import silently and
    poison every locality filter, so it is rejected here rather than stored.
    """
    text = path.read_text(encoding="utf-8-sig")
    if _looks_corrupted(text):
        raise SystemExit(
            "הקובץ מכיל עברית פגומה (קידוד שבור). ייצא מחדש כ-UTF-8 "
            "ושלח כקובץ מצורף, לא כהדבקה — הדבקה מאבדת בתים.")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("listings") or data.get("items") or data.get("data")
        if not isinstance(data, list):
            raise SystemExit("JSON חייב להיות מערך או אובייקט עם listings/items/data")
        return data
    return list(csv.DictReader(text.splitlines()))


def _looks_corrupted(text: str) -> bool:
    """Detect the classic double-encoded / truncated-Hebrew signature."""
    if "�" in text:
        return True
    # 'Ã—' is what a UTF-8 Hebrew lead byte looks like after a latin-1 round
    # trip; several of them means the file was mangled, not merely unusual.
    return text.count("×") >= 5 and not re.search(r"[֐-׿]", text)


def import_file(path, source: str, dry_run: bool = False, db_path=None) -> dict:
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"קובץ לא נמצא: {path}")
    rows = [normalise_row(r) for r in _read(path)]

    usable = [r for r in rows if r.get("price") and r.get("area_m2")]
    skipped = len(rows) - len(usable)
    result = {
        "file": str(path), "source": source, "rows": len(rows),
        "importable": len(usable), "skipped_missing_price_or_area": skipped,
        "dry_run": dry_run,
    }
    if not dry_run and usable:
        conn = db.get_conn(db_path)
        try:
            result["imported"] = market_client.import_listings_records(
                conn, usable, source=source)
        finally:
            conn.close()
    else:
        result["imported"] = 0
    if skipped:
        result["note"] = ("שורות ללא מחיר או שטח נכנסות למסד אך לא יופיעו "
                          "בדאשבורד — הדירוג מחשב ₪/מ״ר.")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ייבוא פיד שותף עם שמות עמודות שונים")
    parser.add_argument("file", help="קובץ CSV או JSON מהשותף")
    parser.add_argument("--source", required=True,
                        help="שם מקור קבוע, למשל yad2-partner")
    parser.add_argument("--db", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="הצג מה ייובא בלי לכתוב")
    args = parser.parse_args()
    result = import_file(args.file, args.source, args.dry_run, args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
