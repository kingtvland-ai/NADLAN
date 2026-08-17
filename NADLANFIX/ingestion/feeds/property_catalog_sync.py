"""Copy the complete year-aware scraper catalog into PlanWatch's own DB."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import db


DEFAULT_SOURCE_DB = (
    Path(__file__).resolve().parent.parent
    / "real_estate_scraper" / "real_estate_scraper" / "data" / "real_estate.db"
)


def sync_catalog(source_db=DEFAULT_SOURCE_DB, target_db=None, years=(2025, 2026)):
    source_db = Path(source_db).resolve()
    if not source_db.exists():
        raise FileNotFoundError(source_db)
    conn = db.get_conn(target_db)
    try:
        conn.execute("ATTACH DATABASE ? AS scraper", (str(source_db),))
        marks = ",".join("?" for _ in years)
        before = conn.total_changes
        conn.execute(f"""
            INSERT INTO property_catalog
            (source, external_id, listing_year, listing_url, source_lastmod,
             image_count, images_json, imported_at)
            SELECT y.source, y.external_id, y.year, c.listing_url,
                   CASE WHEN substr(c.source_lastmod, 1, 4)=CAST(y.year AS TEXT)
                        THEN c.source_lastmod ELSE y.last_lastmod END,
                   0, NULL, datetime('now')
              FROM scraper.listing_source_years y
              JOIN scraper.listing_source_catalog c
                ON c.source=y.source AND c.external_id=y.external_id
             WHERE y.year IN ({marks})
            ON CONFLICT(source, external_id, listing_year) DO UPDATE SET
                listing_url=excluded.listing_url,
                source_lastmod=excluded.source_lastmod,
                image_count=0,
                images_json=NULL,
                imported_at=excluded.imported_at
        """, tuple(int(year) for year in years))
        conn.commit()
        counts = {
            int(year): conn.execute(
                "SELECT COUNT(*) FROM property_catalog WHERE listing_year=?",
                (int(year),),
            ).fetchone()[0]
            for year in years
        }
        duplicate_groups = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT source, external_id, listing_year, COUNT(*) n
                FROM property_catalog
                GROUP BY source, external_id, listing_year HAVING n > 1
            )
        """).fetchone()[0]
        return {
            "rows_touched": conn.total_changes - before,
            "counts": counts,
            "duplicate_groups": duplicate_groups,
            "source_db": str(source_db),
            "target_db": str(Path(target_db or db.DB_PATH).resolve()),
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync property catalog into PlanWatch")
    parser.add_argument("--source-db", default=str(DEFAULT_SOURCE_DB))
    parser.add_argument("--target-db", default=None)
    parser.add_argument("--years", type=int, nargs="+", default=[2025, 2026])
    args = parser.parse_args()
    result = sync_catalog(args.source_db, args.target_db, args.years)
    print(f"rows_touched={result['rows_touched']}")
    for year, count in result["counts"].items():
        print(f"year={year} records={count}")
    print(f"duplicate_groups={result['duplicate_groups']}")
    print(f"target={result['target_db']}")


if __name__ == "__main__":
    main()
