"""Remove image URL metadata from both property catalogs."""

from pathlib import Path
import sqlite3

import db
from ingestion.feeds.property_catalog_sync import DEFAULT_SOURCE_DB


def cleanup(path: Path, table: str) -> int:
    conn = sqlite3.connect(path, timeout=120)
    try:
        before = conn.total_changes
        conn.execute(
            f"UPDATE {table} SET image_count=0, images_json=NULL "
            "WHERE image_count<>0 OR images_json IS NOT NULL"
        )
        changed = conn.total_changes - before
        conn.commit()
        return changed
    finally:
        conn.close()


if __name__ == "__main__":
    scraper = cleanup(Path(DEFAULT_SOURCE_DB), "listing_source_catalog")
    planwatch = cleanup(Path(db.DB_PATH), "property_catalog")
    print(f"scraper_rows_cleaned={scraper}")
    print(f"planwatch_rows_cleaned={planwatch}")
