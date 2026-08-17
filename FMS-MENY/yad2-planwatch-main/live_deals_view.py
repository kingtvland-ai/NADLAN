"""Read-only live view of completed Israeli real-estate transactions.

Data is queried on demand from the public CKAN datastore published by
"מידע לעם". Nothing is inserted into PlanWatch. Gush and helka are the
primary and exact lookup key.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any


API_URL = "https://www.odata.org.il/api/3/action/datastore_search_sql"
DATASET_URL = "https://www.odata.org.il/dataset/nadlan"
RESOURCE_IDS = (
    "24eabceb-faea-4f92-9e94-df710c29cae7",
    "742a49d3-4ebe-4541-a715-5c8456cd7a65",
    "78d33b90-cb93-478a-ba60-3b519e551505",
)


def _digits(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit():
        raise ValueError(f"{field} must contain digits only")
    return str(int(text))


def _date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fetch_resource(resource_id: str, gush: str, helka: str,
                    limit: int, timeout: float) -> list[dict]:
    parcel = f"{gush}-{helka}"
    # The source encodes an optional sub-parcel as GUSH-HELKA-SUBPARCEL.
    sql = (
        f'SELECT * FROM "{resource_id}" '
        f"WHERE gush = '{parcel}' OR gush LIKE '{parcel}-%' "
        "ORDER BY dealdatetime DESC "
        f"LIMIT {int(limit)}"
    )
    url = API_URL + "?" + urllib.parse.urlencode({"sql": sql})
    request = urllib.request.Request(
        url, headers={"User-Agent": "PlanWatch/1.0 read-only parcel viewer"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "public datastore query failed")
    return payload.get("result", {}).get("records", [])


def deals_for_parcel(gush: Any, helka: Any, *, date_from: str | None = None,
                     date_to: str | None = None, min_price: float | None = None,
                     max_price: float | None = None, limit: int = 100,
                     timeout: float = 15.0) -> dict:
    """Return completed deals for an exact gush/helka without persistence."""
    gush = _digits(gush, "gush")
    helka = _digits(helka, "helka")
    date_from = _date(date_from, "date_from")
    date_to = _date(date_to, "date_to")
    limit = max(1, min(int(limit), 500))

    rows: list[dict] = []
    errors: list[str] = []
    for resource_id in RESOURCE_IDS:
        try:
            rows.extend(_fetch_resource(
                resource_id, gush, helka, limit, timeout))
        except Exception as exc:
            errors.append(f"{resource_id}: {exc}")

    normalised = []
    seen = set()
    for row in rows:
        key = row.get("keyvalue") or (
            row.get("dealdatetime"), row.get("dealamount"),
            row.get("gush"), row.get("fulladdress"))
        if key in seen:
            continue
        seen.add(key)
        deal_date = (row.get("dealdatetime") or "")[:10] or None
        price = _number(row.get("dealamount"))
        if date_from and (not deal_date or deal_date < date_from):
            continue
        if date_to and (not deal_date or deal_date > date_to):
            continue
        if min_price is not None and (price is None or price < float(min_price)):
            continue
        if max_price is not None and (price is None or price > float(max_price)):
            continue
        area = _number(row.get("area"))
        normalised.append({
            "deal_id": row.get("keyvalue"),
            "parcel_id": row.get("gush"),
            "gush": gush,
            "helka": helka,
            "deal_date": deal_date,
            "price": price,
            "asset_type": row.get("dealnaturedescription"),
            "rooms": _number(row.get("assetroomno")),
            "floor": row.get("floorno"),
            "address": row.get("fulladdress") or row.get("displayaddress")
                       or ", ".join(filter(None, (
                           row.get("city_name"), row.get("street")))),
            "city": row.get("city_name"),
            "street": row.get("street"),
            "year_built": row.get("yearbuilt") or row.get("buildingyear"),
            "area_sqm": area,
            "price_per_sqm": round(price / area) if price and area else None,
            "transaction_kind": "sale_purchase",
        })

    normalised.sort(key=lambda item: item.get("deal_date") or "", reverse=True)
    normalised = normalised[:limit]
    return {
        "available": bool(normalised) or len(errors) < len(RESOURCE_IDS),
        "gush": gush,
        "helka": helka,
        "parcel_key": f"{gush}/{helka}",
        "match_scope": "exact_parcel",
        "transaction_kind": "sale_purchase",
        "read_only": True,
        "imported_into_planwatch": False,
        "total": len(normalised),
        "rows": normalised,
        "source": "מידע לעם — מאגר עסקאות הנדל״ן",
        "source_url": DATASET_URL,
        "license": "CC-BY",
        "note": "עסקאות מכר/רכישה שבוצעו; הנתונים נקראים בזמן אמת ואינם נשמרים במערכת.",
        "warnings": errors,
    }

