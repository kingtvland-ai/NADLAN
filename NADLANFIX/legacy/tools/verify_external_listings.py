"""Operational check for PlanWatch's address-and-price listing contract."""

import json
from ingestion.feeds import external_listings_view as view


def main():
    status = view.status()
    result = view.listings(limit=max(int(status.get("records", 0)), 1))
    rows = result.get("rows", [])
    report = {
        "available": status.get("available", False),
        "visible": result.get("total", 0),
        "missing_address_or_price": sum(
            not row.get("address") or not row.get("price") for row in rows
        ),
        "without_link": sum(not row.get("url") for row in rows),
    }
    print(json.dumps(report, ensure_ascii=False))
    if not report["available"] or report["missing_address_or_price"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
