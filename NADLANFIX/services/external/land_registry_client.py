"""
PlanWatch - land registry (פנקסי המקרקעין) open-data client
============================================================
The Ministry of Justice publishes an open extract of the land registry on
data.gov.il: dataset `tabu_asset` - "סוג בעלות בנכסים הרשומים בפנקסי המקרקעין".
Verified live (2026-07-27): 2,864,811 rows, updated the same day, queryable by
exact גוש/חלקה. Columns: גוש, חלקה, תת חלקה, תיאור שיטה, סוג בעלות.

What this IS and IS NOT
-----------------------
This is the closest thing to "נסחי טאבו של כל ישראל" that exists as open data:
one row per registered asset (תת-חלקה) with its ownership TYPE (פרטית / מדינה /
רשות מקומית / ...). It is NOT a full נסח טאבו: owner names, ID numbers, liens
and mortgages are personal data available only as a paid per-parcel extract
(~15 ILS) from the official service, with no bulk API - by design. Use
`order_extract_link()` to send the user to the official order page.

Two access modes
----------------
* `ownership(gush, helka)` - live CKAN query for one parcel (fast, no setup).
* `import_all(conn)` - stream the ENTIRE dataset into a local `tabu_assets`
  table via the CKAN datastore dump (CSV). ~2.86M rows; afterwards
  `local_ownership()` answers instantly and offline.
"""

from __future__ import annotations

import json
from legacy.tools.http_client import build_session

RESOURCE_ID = "a1a91496-d692-4420-bc21-3487600b71a5"
SEARCH_URL = "https://data.gov.il/api/3/action/datastore_search"

#: Hebrew source columns -> local column names.
COLMAP = {"גוש": "gush", "חלקה": "helka", "תת חלקה": "tat_helka",
          "תיאור שיטה": "method", "סוג בעלות": "ownership"}

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tabu_assets (
    gush       INTEGER NOT NULL,
    helka      INTEGER NOT NULL,
    tat_helka  INTEGER,
    method     TEXT,
    ownership  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tabu_parcel ON tabu_assets (gush, helka);
"""

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def ownership(gush, helka, timeout=30) -> list[dict]:
    """
    Live query: registered assets for one parcel, straight from data.gov.il.
    Returns [{gush, helka, tat_helka, method, ownership}] (may be empty -
    not every parcel has registered assets in the published extract).
    """
    filters = json.dumps({"גוש": int(gush), "חלקה": int(helka)},
                         ensure_ascii=False)
    resp = _sess().get(SEARCH_URL, params={
        "resource_id": RESOURCE_ID, "filters": filters, "limit": 500,
    }, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"datastore_search failed: {data}")
    return [
        {COLMAP[k]: v for k, v in rec.items() if k in COLMAP}
        for rec in data["result"]["records"]
    ]


def summarize(rows: list[dict]) -> dict:
    """Compact roll-up for a UI header: counts per ownership type."""
    by_type: dict[str, int] = {}
    for r in rows:
        key = (r.get("ownership") or "לא ידוע").strip() or "לא ידוע"
        by_type[key] = by_type.get(key, 0) + 1
    return {"assets": len(rows), "by_ownership": by_type}


def local_ownership(conn, gush, helka) -> list[dict] | None:
    """
    Answer from the locally imported table, or None if no import was run yet
    (callers then fall back to the live `ownership()` query).
    """
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tabu_assets'"
    ).fetchone()
    if not have or not conn.execute(
            "SELECT 1 FROM tabu_assets LIMIT 1").fetchone():
        return None
    rows = conn.execute(
        """SELECT gush, helka, tat_helka, method, ownership
           FROM tabu_assets WHERE gush=? AND helka=? ORDER BY tat_helka""",
        (int(gush), int(helka))).fetchall()
    return [dict(r) for r in rows]


def local_count(conn) -> int:
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tabu_assets'"
    ).fetchone()
    return conn.execute("SELECT COUNT(*) FROM tabu_assets").fetchone()[0] if have else 0


#: datastore_search's server-side page cap on data.gov.il (verified live:
#: limit=32000 returns 32000 rows in ~2s; the whole set is ~90 pages).
PAGE_SIZE = 32_000


def import_all(conn, progress=None, timeout=(30, 120), should_stop=None) -> int:
    """
    Pull the full national registry extract (~2.86M rows) into `tabu_assets`
    by paging `datastore_search`. Replaces any previous import atomically
    (build into a temp table, swap at the end).

    Why not the CSV dump endpoint: /datastore/dump/ sits behind a JS-challenge
    WAF and serves an obfuscated challenge page to non-browsers (verified
    live 2026-07); the JSON search API is the documented, open path.

    `progress(rows_so_far)` is called after every page.
    `should_stop()` - optional cancel check, tested before each page. On cancel
    the partial staging table is dropped and the previous import (if any) is
    left intact, so the DB is never left half-replaced.

    Returns the row count (0 if cancelled before completing).
    """
    conn.executescript(TABLE_SQL)
    sess = _sess()

    conn.execute("DROP TABLE IF EXISTS tabu_assets_new")
    conn.execute("""CREATE TABLE tabu_assets_new (
        gush INTEGER NOT NULL, helka INTEGER NOT NULL, tat_helka INTEGER,
        method TEXT, ownership TEXT)""")

    def to_int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    total = 0
    offset = 0
    while True:
        if should_stop and should_stop():
            # Abandon the staging table; the live table is untouched.
            conn.execute("DROP TABLE IF EXISTS tabu_assets_new")
            conn.commit()
            return 0
        resp = sess.get(SEARCH_URL, params={
            "resource_id": RESOURCE_ID, "limit": PAGE_SIZE, "offset": offset,
        }, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"datastore_search failed at offset {offset}: "
                               f"{str(data)[:200]}")
        records = data["result"]["records"]
        if not records:
            break

        batch = []
        for rec in records:
            gush = to_int(rec.get("גוש"))
            helka = to_int(rec.get("חלקה"))
            if gush is None or helka is None:
                continue
            batch.append((gush, helka, to_int(rec.get("תת חלקה")),
                          (rec.get("תיאור שיטה") or "").strip() or None,
                          (rec.get("סוג בעלות") or "").strip() or None))
        conn.executemany("INSERT INTO tabu_assets_new VALUES (?,?,?,?,?)", batch)
        conn.commit()
        total += len(batch)
        offset += len(records)
        if progress:
            progress(total)
        if len(records) < PAGE_SIZE:
            break

    conn.execute("DROP TABLE IF EXISTS tabu_assets")
    conn.execute("ALTER TABLE tabu_assets_new RENAME TO tabu_assets")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tabu_parcel ON tabu_assets (gush, helka)")
    conn.commit()
    if progress:
        progress(total)
    return total


def order_extract_link(gush=None, helka=None) -> str:
    """
    The OFFICIAL paid נסח טאבו service (משרד המשפטים). There is no public
    bulk/free API for full extracts - they contain personal data (owners,
    mortgages) and are sold per parcel. The form cannot be pre-filled by URL;
    we still surface the gush/helka beside the button so the user can copy it.
    """
    return "https://www.gov.il/he/service/land_registration_extract"


if __name__ == "__main__":
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    gush, helka = (sys.argv + ["6941", "23"])[1:3]
    rows = ownership(gush, helka)
    print(f"גוש {gush} חלקה {helka}: {len(rows)} registered assets")
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=1))
    for r in rows[:8]:
        print("  ", r)
