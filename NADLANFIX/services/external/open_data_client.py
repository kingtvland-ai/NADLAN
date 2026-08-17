"""
PlanWatch - data.gov.il open-data client
=========================================
Extra national datasets that enrich the planning picture. All are public CKAN
datastores on data.gov.il - no key, documented API. Verified live 2026-07:

    localities   רשימת ישובים בישראל          1,310 rows   (נפה, לשכה, מועצה)
    contractors  פנקס הקבלנים הרשומים        24,270 rows   (ענף, סיווג, קבוצה)
    appraisers   מאגר שמאי מקרקעין            3,104 rows   (רשיון, עיר)

Land registry lives in its own module (land_registry_client) because it is far
bigger and gets a full local import; these three are small enough to cache in
one table each and refresh on demand.
"""

from __future__ import annotations

import json
from legacy.tools.http_client import build_session

SEARCH_URL = "https://data.gov.il/api/3/action/datastore_search"

#: dataset key -> (resource_id, local table, {source column: local column})
DATASETS = {
    "localities": (
        "5c78e9fa-c2e2-4771-93ff-7f400a12f7ba", "od_localities",
        {"סמל_ישוב": "code", "שם_ישוב": "name", "שם_ישוב_לועזי": "name_en",
         "שם_נפה": "nafa", "לשכה": "bureau", "שם_מועצה": "council"},
    ),
    "contractors": (
        "4eb61bd6-18cf-4e7c-9f9c-e166dfa0a2d8", "od_contractors",
        {"MISPAR_KABLAN": "license", "SHEM_YESHUT": "name",
         "SHEM_YISHUV": "city", "TEUR_ANAF": "branch",
         "KVUTZA": "group_", "SIVUG": "grade", "MISPAR_TEL": "phone",
         "EMAIL": "email", "TAARICH_KABLAN": "since"},
    ),
    "appraisers": (
        "8540534a-eccd-4568-a677-652d589ed172", "od_appraisers",
        {"שם שמאי": "name", "מספר רשיון": "license", "עיר": "city"},
    ),
}

PAGE_SIZE = 32_000

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def _clean(value):
    """Source rows are padded with spaces and use "" for missing values."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def table_sql(table: str, columns: list[str]) -> str:
    cols = ", ".join(f"{c} TEXT" for c in columns)
    return f"CREATE TABLE IF NOT EXISTS {table} ({cols})"


def refresh(conn, key: str, progress=None, should_stop=None,
            timeout=(30, 120)) -> int:
    """
    Re-download one dataset into its local table (staged, then swapped).
    Returns the row count, or 0 if cancelled.
    """
    if key not in DATASETS:
        raise ValueError(f"unknown dataset {key!r}; have {sorted(DATASETS)}")
    resource_id, table, colmap = DATASETS[key]
    columns = list(colmap.values())

    conn.execute(f"DROP TABLE IF EXISTS {table}_new")
    conn.execute(table_sql(f"{table}_new", columns))
    placeholders = ",".join("?" for _ in columns)

    total, offset = 0, 0
    while True:
        if should_stop and should_stop():
            conn.execute(f"DROP TABLE IF EXISTS {table}_new")
            conn.commit()
            return 0
        resp = _sess().get(SEARCH_URL, params={
            "resource_id": resource_id, "limit": PAGE_SIZE, "offset": offset,
        }, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(f"{key}: datastore_search failed at {offset}")
        records = payload["result"]["records"]
        if not records:
            break

        batch = [tuple(_clean(rec.get(src)) for src in colmap) for rec in records]
        conn.executemany(
            f"INSERT INTO {table}_new VALUES ({placeholders})", batch)
        conn.commit()
        total += len(batch)
        offset += len(records)
        if progress:
            progress(key, total)
        if len(records) < PAGE_SIZE:
            break

    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
    if key == "localities":
        conn.execute("CREATE INDEX IF NOT EXISTS idx_od_loc_name ON od_localities (name)")
    elif key == "contractors":
        conn.execute("CREATE INDEX IF NOT EXISTS idx_od_con_city ON od_contractors (city)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_od_con_name ON od_contractors (name)")
    elif key == "appraisers":
        conn.execute("CREATE INDEX IF NOT EXISTS idx_od_app_city ON od_appraisers (city)")
    conn.commit()
    if progress:
        progress(key, total)
    return total


def refresh_all(conn, progress=None, should_stop=None) -> dict:
    out = {}
    for key in DATASETS:
        if should_stop and should_stop():
            break
        out[key] = refresh(conn, key, progress=progress, should_stop=should_stop)
    return out


def counts(conn) -> dict:
    """Local row count per dataset (0 when never imported)."""
    out = {}
    for key, (_rid, table, _cm) in DATASETS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        out[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] \
            if exists else 0
    return out


#: Source names are inconsistently spaced around hyphens ("תל אביב -יפו" vs the
#: "תל אביב-יפו" a user types), so comparisons run on a squashed form.
_SQUASH = "REPLACE(REPLACE(REPLACE({},' ',''),'-',''),'''','')"


def squash(name: str) -> str:
    return (name or "").replace(" ", "").replace("-", "").replace("'", "")


def locality(conn, name: str) -> dict | None:
    """
    District / bureau / regional-council context for a locality name.
    Matches ignoring spaces and hyphens, then falls back to a contains match,
    so "תל אביב-יפו" finds the stored "תל אביב -יפו".
    """
    if not counts(conn).get("localities"):
        return None
    target = squash(name)
    if not target:
        return None
    col = _SQUASH.format("name")
    row = conn.execute(
        f"""SELECT name, name_en, nafa, bureau, council, code FROM od_localities
            WHERE {col} = ?
            ORDER BY LENGTH(name) LIMIT 1""", (target,)).fetchone()
    if row is None:
        row = conn.execute(
            f"""SELECT name, name_en, nafa, bureau, council, code FROM od_localities
                WHERE {col} LIKE ? ORDER BY LENGTH(name) LIMIT 1""",
            (f"%{target}%",)).fetchone()
    return dict(row) if row else None


def contractors_in(conn, city: str, limit: int = 25) -> list[dict]:
    """Registered contractors based in a locality - who builds here."""
    if not counts(conn).get("contractors"):
        return []
    col = _SQUASH.format("city")
    rows = conn.execute(
        f"""SELECT name, license, branch, grade, group_, phone, email, city
            FROM od_contractors WHERE {col} LIKE ?
            ORDER BY CAST(grade AS INTEGER) DESC, name LIMIT ?""",
        (f"%{squash(city)}%", limit)).fetchall()
    return [dict(r) for r in rows]


def appraisers_in(conn, city: str, limit: int = 25) -> list[dict]:
    if not counts(conn).get("appraisers"):
        return []
    col = _SQUASH.format("city")
    rows = conn.execute(
        f"""SELECT name, license, city FROM od_appraisers
            WHERE {col} LIKE ? ORDER BY name LIMIT ?""",
        (f"%{squash(city)}%", limit)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    conn = db.get_conn()
    which = sys.argv[1:] or list(DATASETS)
    for key in which:
        n = refresh(conn, key, progress=lambda k, t: print(f"  {k}: {t:,}", end="\r"))
        print(f"\n{key}: {n:,} rows")
    print(json.dumps(counts(conn), ensure_ascii=False, indent=1))
    conn.close()
