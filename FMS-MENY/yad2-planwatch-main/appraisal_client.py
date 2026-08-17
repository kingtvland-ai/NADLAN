"""
PlanWatch - decisive appraisals (שמאות מכריעה) + building progress
==================================================================
Three sources found in the 2026-07-28 sweep of data.gov.il, all live.

1. `samay-dataset` - **מאגר שומות מכריעות**, 31,073 rows, משרד המשפטים.
   A שמאי מכריע ruling is what happens when an owner disputes the היטל השבחה
   (betterment levy) a local committee charged after a plan raised the value of
   their land. Every row carries **גוש + חלקה** (100% populated on a 600-row
   sample), the committee, the decision date, the appraiser's name, and a link
   to the ruling document.

   Why this matters more than it sounds: a betterment-levy dispute is
   *documentary proof that a plan created value on that specific parcel*, and
   that the amount was large enough to be worth litigating. It is the closest
   thing to per-parcel valuation evidence available from an open Israeli source
   - the actual transaction database (nadlan.gov.il) is reCAPTCHA-gated.

   Verified: 2 appraisal types (היטל השבחה 98.8%, תביעת פיצויים 1.2%),
   2 appraiser types (מכריע 96.8%, מייעץ 3.2%), 75 committees.

2. `hitkadmuthabnia` - **התקדמות הבניה**, 10,371 rows, משרד הבינוי.
   Per-BUILDING construction progress on מתחמי בנייה רוויה: גוש, חלקה, building
   number, floors, units, area, and eight dated construction milestones. This
   is per-building, not per-plan - the finest granularity in the project.

3. `metachnenim` - **רשימת מודדים ומתכננים**, 3,737 rows: surveyors and
   planners with specialisation and locality.

Field traps, all verified against live data
-------------------------------------------
* `samay.block` / `samay.plot` are TEXT and hold ranges and lists, not just
  integers: "6894", "17", but also multi-value forms. `parse_parcels()` splits
  them so the join to `plans`/`tabu_assets` actually lands, and rows it cannot
  parse are kept with `parse_ok=0` rather than dropped.
* `samay.decision_date` is `DD/MM/YYYY HH:MM`, not ISO.
* `hitkadmuthabnia.HELKA` is `"0"` on a large share of rows - meaning
  "not recorded", not parcel zero. Stored as NULL.
* `TAARICH_SHLAV_BNIYA_*` are **Excel serial day numbers** (e.g. 40951), and
  the same column mixes strings and ints. 40951 -> 2012-02-20. Treating them
  as epoch or as year numbers produces garbage.
* `SHEM_YISHUV` / locality names are space-padded across all three feeds.
"""

from __future__ import annotations

import json
import re

from http_client import build_session

CKAN = "https://data.gov.il/api/3/action/datastore_search"

RES_SAMAY = "c4e178ff-9038-45f4-9dd1-fe7aeea6f70c"
RES_PROGRESS = "1ec45809-5927-430a-9b30-77f77f528ce3"
RES_PLANNERS = "a8ca5203-7adc-4bc1-a0ed-3fd4eaa74dcc"

PAGE = 1000

#: The eight construction-milestone columns, in the source's own order.
#: The numbers are the ministry's internal stage codes, not a sequence.
PROGRESS_STAGES = (5, 7, 8, 16, 18, 29, 39, 42)

TABLES = """
CREATE TABLE IF NOT EXISTS appraisals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    department_id INTEGER,
    header        TEXT,        -- the ruling's full title line
    appraisal_type TEXT,       -- היטל השבחה | תביעת פיצויים
    appraiser_type TEXT,       -- שמאי מכריע | שמאי מייעץ
    appraiser     TEXT,
    committee     TEXT,        -- the local planning committee
    block_raw     TEXT,        -- as published
    plot_raw      TEXT,        -- as published
    decision_date TEXT,        -- ISO
    publicity_date TEXT,       -- ISO
    version       TEXT,
    link          TEXT,
    /*
     * The key has to include block/plot/date, not just (department_id, header).
     *
     * The source publishes 793 EXACT duplicate rows - identical department,
     * header, block, plot and date - which this key correctly collapses. But it
     * also truncates long headers, so 33 genuinely different rulings share a
     * header prefix and were being collapsed too. Verified against the live
     * feed: one such pair is גוש 3926 חלקה 158 vs חלקה 616, same day, same
     * truncated title. Another pair has גוש and חלקה transposed between the two
     * rows at source ('12','4878') / ('4878','12').
     *
     * 31,073 source rows -> 30,280 after removing true duplicates.
     */
    UNIQUE (department_id, header, block_raw, plot_raw, decision_date)
);

/* One row per (appraisal, parcel). `block_raw`/`plot_raw` can name several
   parcels, and a levy dispute genuinely can span them, so the join table is
   the honest shape - not a single gush/helka column on `appraisals`. */
CREATE TABLE IF NOT EXISTS appraisal_parcels (
    appraisal_id INTEGER NOT NULL,
    gush         TEXT,
    helka        TEXT,
    parse_ok     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (appraisal_id, gush, helka)
);

CREATE TABLE IF NOT EXISTS building_progress (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    district      TEXT,
    locality      TEXT,
    site          TEXT,
    complex_id    TEXT,
    complex_name  TEXT,
    lot           TEXT,
    gush          TEXT,
    helka         TEXT,        -- NULL when the source published "0"
    building_no   TEXT,
    floors        INTEGER,
    units         INTEGER,
    area_m2       REAL,
    marketing     TEXT,
    decisive_date TEXT,
    contract_year TEXT,
    stages_json   TEXT,        -- {stage_code: ISO date}
    first_stage   TEXT,        -- earliest milestone, ISO
    last_stage    TEXT,        -- latest milestone, ISO
    UNIQUE (complex_id, gush, building_no, lot)
);

CREATE TABLE IF NOT EXISTS planners (
    code          TEXT PRIMARY KEY,
    name          TEXT,
    street        TEXT,
    house_no      TEXT,
    locality_code TEXT,
    locality      TEXT,
    phone         TEXT,
    speciality    TEXT,
    sub_speciality TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_appr_committee ON appraisals (committee);
CREATE INDEX IF NOT EXISTS idx_appr_date      ON appraisals (decision_date);
CREATE INDEX IF NOT EXISTS idx_appr_type      ON appraisals (appraisal_type);
CREATE INDEX IF NOT EXISTS idx_ap_gh          ON appraisal_parcels (gush, helka);
CREATE INDEX IF NOT EXISTS idx_prog_gh        ON building_progress (gush, helka);
CREATE INDEX IF NOT EXISTS idx_prog_locality  ON building_progress (locality);
CREATE INDEX IF NOT EXISTS idx_planners_loc   ON planners (locality);
CREATE INDEX IF NOT EXISTS idx_planners_spec  ON planners (speciality);
"""

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session()
    return _session


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


# ------------------------------------------------------------------ parsing

def _text(value):
    if value is None:
        return None
    out = " ".join(str(value).replace("\xa0", " ").split())
    return out if out and out != "-" else None


def _int(value):
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _float(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")


def _date(value):
    """'31/07/2013 00:00' -> '2013-07-31'."""
    text = _text(value)
    if not text:
        return None
    match = _DMY.match(text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return text[:10]


def excel_serial_to_iso(value):
    """
    Excel serial day number -> ISO date.

    The `TAARICH_SHLAV_BNIYA_*` columns hold values like 40951 and mix int and
    str in the same column. Excel's epoch is 1899-12-30 (the 1900 leap-year bug
    is why it is not 12-31). Anchor-checked: serial 44927 -> 2023-01-01, which
    is what Excel itself displays. 40951 -> 2012-02-12.

    Anything outside a sane construction window (1990..2050) is rejected rather
    than converted, so a stray real date string or a code cannot become 1902.
    """
    number = _float(value)
    if number is None or number <= 0:
        # Some rows carry an actual date string instead of a serial.
        as_date = _date(value)
        return as_date if as_date and as_date[:2] in ("19", "20") else None
    from datetime import date, timedelta
    try:
        out = date(1899, 12, 30) + timedelta(days=int(number))
    except (OverflowError, ValueError):
        return None
    return out.isoformat() if 1990 <= out.year <= 2050 else None


#: A gush/helka field can be "6894", "17", "17,18", "17-19", "17 ,18".
_NUMS = re.compile(r"\d{1,6}")
_RANGE = re.compile(r"^(\d{1,6})\s*[-–]\s*(\d{1,6})$")


def parse_parcels(block_raw, plot_raw) -> tuple[list[tuple[str, str]], bool]:
    """
    Expand the published block/plot text into concrete (gush, helka) pairs.

    Returns `(pairs, parse_ok)`. When nothing usable can be extracted the
    caller still stores the appraisal with `parse_ok=0`; dropping it would hide
    a real ruling just because its parcel text was awkward.

    A range is only expanded when it is small. "17-19" is three parcels;
    a 4-digit span is a typo or a gush range, and expanding it would invent
    thousands of rows.

    Separators seen in the live feed, all handled:
      "6666  6665" / "1970 1972"  whitespace-separated list (the common form)
      "17,18"  "17;18"  "17 ו18"  punctuation / Hebrew conjunction
      "17-19"                     small range, expanded
      "354/3"                     sub-parcel (תת-חלקה), NOT a list - the slash
                                  denotes subdivision, so only "354" is the
                                  parcel. Splitting on "/" would fabricate a
                                  parcel 3.
    """
    blocks = _text(block_raw) or ""
    plots = _text(plot_raw) or ""

    def expand(text):
        out = []
        # "/" is subdivision, so cut the sub-parcel suffix before splitting.
        text = re.sub(r"(\d)\s*/\s*\d+", r"\1", text)
        for chunk in re.split(r"[,،;]|\s+| ו", text):
            chunk = chunk.strip()
            if not chunk:
                continue
            span = _RANGE.match(chunk)
            if span:
                lo, hi = int(span.group(1)), int(span.group(2))
                if 0 < hi - lo <= 40:
                    out.extend(str(n) for n in range(lo, hi + 1))
                    continue
            out.extend(_NUMS.findall(chunk))
        return [n for n in out if n and int(n) > 0]

    gushim, helkot = expand(blocks), expand(plots)
    if not gushim:
        return [], False
    if not helkot:
        return [(g, None) for g in gushim], False
    pairs = {(g, h) for g in gushim for h in helkot}
    # Guard against a combinatorial blow-up from a messy row.
    if len(pairs) > 200:
        return [(gushim[0], helkot[0])], False
    return sorted(pairs), True


def _ckan_all(resource_id, timeout=(25, 120), should_stop=None):
    offset = 0
    while True:
        if should_stop and should_stop():
            return
        payload = _sess().get(CKAN, params={
            "resource_id": resource_id, "limit": PAGE, "offset": offset,
        }, timeout=timeout).json()
        if not payload.get("success"):
            raise RuntimeError(f"CKAN failed for {resource_id} at {offset}")
        records = payload["result"]["records"]
        if not records:
            return
        yield from records
        offset += len(records)
        if len(records) < PAGE:
            return


# ------------------------------------------------------------------ imports

def import_appraisals(conn, progress=None, should_stop=None,
                      timeout=(25, 120)) -> int:
    """
    Import the decisive-appraisal register and expand its parcel references.

    Rebuilt in place (DELETE + insert inside one transaction) rather than
    staged-swap, because `appraisal_parcels` has a foreign relationship to the
    autoincrement id and a table rename would break the pairing mid-flight.
    """
    ensure_schema(conn)
    rows, pairs, unparsed, seen = [], [], 0, set()
    for rec in _ckan_all(RES_SAMAY, timeout, should_stop):
        header = _text(rec.get("appraisal_header"))
        dept = _int(rec.get("department_id"))
        block, plot = _text(rec.get("block")), _text(rec.get("plot"))
        decided = _date(rec.get("decision_date"))
        key = (dept, header, block, plot, decided)
        if key in seen:          # true source duplicate
            continue
        seen.add(key)
        parcels, ok = parse_parcels(rec.get("block"), rec.get("plot"))
        if not ok:
            unparsed += 1
        rows.append((
            dept, header,
            _text(rec.get("appraisal_type")), _text(rec.get("appraiser_type")),
            _text(rec.get("decisive_appraiser")), _text(rec.get("committee")),
            block, plot, decided, _date(rec.get("publicity_date")),
            _text(rec.get("appraisal_version")), _text(rec.get("link")),
        ))
        pairs.append((key, parcels, ok))
        if progress and len(rows) % 2000 == 0:
            progress("appraisals", len(rows))

    if should_stop and should_stop():
        return 0

    conn.execute("DELETE FROM appraisal_parcels")
    conn.execute("DELETE FROM appraisals")
    conn.executemany(
        """INSERT INTO appraisals
             (department_id, header, appraisal_type, appraiser_type, appraiser,
              committee, block_raw, plot_raw, decision_date, publicity_date,
              version, link)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (department_id, header, block_raw, plot_raw, decision_date)
           DO NOTHING""", rows)

    ids = {(r["department_id"], r["header"], r["block_raw"], r["plot_raw"],
            r["decision_date"]): r["id"] for r in conn.execute(
        """SELECT id, department_id, header, block_raw, plot_raw, decision_date
           FROM appraisals""")}
    parcel_rows = []
    for key, parcels, ok in pairs:
        aid = ids.get(key)
        if aid is None:
            continue
        for gush, helka in parcels:
            parcel_rows.append((aid, gush, helka, 1 if ok else 0))
    conn.executemany(
        """INSERT INTO appraisal_parcels (appraisal_id, gush, helka, parse_ok)
           VALUES (?,?,?,?) ON CONFLICT DO NOTHING""", parcel_rows)
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM appraisals").fetchone()[0]
    if progress:
        progress("appraisals", total)
    if unparsed:
        print(f"  ({unparsed:,} rows had parcel text that could not be fully "
              f"parsed - kept with parse_ok=0)")
    return total


def import_building_progress(conn, progress=None, should_stop=None,
                             timeout=(25, 120)) -> int:
    """Per-building construction progress, with Excel serial dates decoded."""
    ensure_schema(conn)
    rows = []
    for rec in _ckan_all(RES_PROGRESS, timeout, should_stop):
        stages = {}
        for code in PROGRESS_STAGES:
            iso = excel_serial_to_iso(rec.get(f"TAARICH_SHLAV_BNIYA_{code}"))
            if iso:
                stages[str(code)] = iso
        dates = sorted(stages.values())
        # "0" means "not recorded", not parcel/block zero. This applies to
        # GUSH as well as HELKA - 1,221 rows publish GUSH="0", and storing
        # those made the join to tabu_assets look 34% broken when in fact the
        # source simply had no parcel for that building.
        helka = _text(rec.get("HELKA"))
        gush = _text(rec.get("GUSH"))
        rows.append((
            _text(rec.get("MAHOZ")), _text(rec.get("YESHUV_LAMAS")),
            _text(rec.get("ATAR")), _text(rec.get("MISPAR_MITHAM")),
            _text(rec.get("SHEM_MITHAM")), _text(rec.get("MIGRASH")),
            None if gush in (None, "0") else gush,
            None if helka in (None, "0") else helka,
            _text(rec.get("MISPAR_BINYAN")), _int(rec.get("KOMOT_BINYAN")),
            _int(rec.get("YEHIDOT_BINYAN")), _float(rec.get("SHETAH")),
            _text(rec.get("SHITAT_SHIVUK")), _date(rec.get("TAARICH_KOBEA")),
            _text(rec.get("SHNAT_HOZE")),
            json.dumps(stages, ensure_ascii=False) if stages else None,
            dates[0] if dates else None, dates[-1] if dates else None,
        ))
        if progress and len(rows) % 2000 == 0:
            progress("progress", len(rows))
    if should_stop and should_stop():
        return 0

    conn.execute("DELETE FROM building_progress")
    conn.executemany(
        """INSERT INTO building_progress
             (district, locality, site, complex_id, complex_name, lot, gush,
              helka, building_no, floors, units, area_m2, marketing,
              decisive_date, contract_year, stages_json, first_stage, last_stage)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (complex_id, gush, building_no, lot) DO NOTHING""", rows)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM building_progress").fetchone()[0]
    if progress:
        progress("progress", total)
    return total


def import_planners(conn, progress=None, should_stop=None,
                    timeout=(25, 120)) -> int:
    ensure_schema(conn)
    rows = [(
        _text(r.get("KOD_SAPAK")), _text(r.get("SHEM_SAPAK")),
        _text(r.get("RECHOV")), _text(r.get("MISPAR_BAIT")),
        _text(r.get("SEMEL_YISHUV")), _text(r.get("SHEM_YISHUV")),
        _text(r.get("MISPAR_TEL")), _text(r.get("HITMAHUT_DESCRIPTION")),
        _text(r.get("SUB_KOD_HITMAHUT_DESCRIPTION")),
    ) for r in _ckan_all(RES_PLANNERS, timeout, should_stop)]
    if should_stop and should_stop():
        return 0
    conn.executemany(
        """INSERT INTO planners
             (code, name, street, house_no, locality_code, locality, phone,
              speciality, sub_speciality)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (code) DO UPDATE SET
             name=excluded.name, street=excluded.street,
             house_no=excluded.house_no, locality=excluded.locality,
             phone=excluded.phone, speciality=excluded.speciality,
             sub_speciality=excluded.sub_speciality""", rows)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM planners").fetchone()[0]
    if progress:
        progress("planners", total)
    return total


IMPORTERS = {
    "appraisals": import_appraisals,
    "progress": import_building_progress,
    "planners": import_planners,
}


def import_all(conn, progress=None, should_stop=None, timeout=(25, 120)) -> int:
    total = 0
    for key, fn in IMPORTERS.items():
        if should_stop and should_stop():
            break
        total += fn(conn, progress=progress, should_stop=should_stop,
                    timeout=timeout)
    return total


# ------------------------------------------------------------------ readers

def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def counts(conn) -> dict:
    out = {}
    for table in ("appraisals", "appraisal_parcels", "building_progress",
                  "planners"):
        out[table] = conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] if _has(conn, table) else 0
    return out


def appraisals_for_parcel(conn, gush, helka=None, limit=50) -> list[dict]:
    """
    Decisive appraisals touching a parcel.

    A hit means a plan raised this land's value enough that the levy was
    disputed before a שמאי מכריע - documentary evidence of realised upside.
    """
    if not _has(conn, "appraisal_parcels"):
        return []
    clauses, params = ["p.gush = ?"], [str(gush).strip()]
    if helka:
        clauses.append("p.helka = ?")
        params.append(str(helka).strip())
    return [dict(r) for r in conn.execute(
        f"""SELECT a.*, p.gush, p.helka, p.parse_ok
            FROM appraisal_parcels p JOIN appraisals a ON a.id = p.appraisal_id
            WHERE {' AND '.join(clauses)}
            ORDER BY a.decision_date DESC LIMIT ?""", params + [limit])]


def appraisal_activity(conn, limit=40) -> list[dict]:
    """
    Committees ranked by betterment-levy dispute volume - a proxy for where
    plans have been creating contested value.
    """
    if not _has(conn, "appraisals"):
        return []
    return [dict(r) for r in conn.execute(
        """SELECT committee, COUNT(*) rulings,
                  SUM(CASE WHEN appraisal_type LIKE '%השבחה%' THEN 1 ELSE 0 END) betterment,
                  SUM(CASE WHEN appraisal_type LIKE '%פיצוי%'  THEN 1 ELSE 0 END) compensation,
                  MIN(decision_date) first_ruling,
                  MAX(decision_date) last_ruling,
                  COUNT(DISTINCT appraiser) appraisers
           FROM appraisals WHERE committee IS NOT NULL
           GROUP BY committee ORDER BY rulings DESC LIMIT ?""", (limit,))]


def progress_for_parcel(conn, gush, helka=None, limit=50) -> list[dict]:
    """Buildings on a parcel with their construction milestones."""
    if not _has(conn, "building_progress"):
        return []
    clauses, params = ["gush = ?"], [str(gush).strip()]
    if helka:
        clauses.append("helka = ?")
        params.append(str(helka).strip())
    rows = [dict(r) for r in conn.execute(
        f"""SELECT * FROM building_progress WHERE {' AND '.join(clauses)}
            ORDER BY last_stage DESC LIMIT ?""", params + [limit])]
    for row in rows:
        row["stages"] = json.loads(row.pop("stages_json") or "{}")
    return rows


def progress_summary(conn) -> dict:
    """Build-out totals by district, plus the reporting window."""
    if not _has(conn, "building_progress"):
        return {"buildings": 0}
    head = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(units),0), COALESCE(SUM(area_m2),0),
                  MIN(first_stage), MAX(last_stage)
           FROM building_progress""").fetchone()
    districts = [dict(r) for r in conn.execute(
        """SELECT district, COUNT(*) buildings, COALESCE(SUM(units),0) units
           FROM building_progress WHERE district IS NOT NULL
           GROUP BY district ORDER BY units DESC""")]
    return {"buildings": head[0], "units": head[1],
            "area_m2": round(head[2] or 0, 1),
            "first_milestone": head[3], "last_milestone": head[4],
            "districts": districts}


def planners_in(conn, locality="", speciality="", limit=60) -> list[dict]:
    if not _has(conn, "planners"):
        return []
    clauses, params = ["1=1"], []
    if locality:
        clauses.append("locality LIKE ?")
        params.append(f"%{locality}%")
    if speciality:
        clauses.append("speciality LIKE ?")
        params.append(f"%{speciality}%")
    return [dict(r) for r in conn.execute(
        f"""SELECT * FROM planners WHERE {' AND '.join(clauses)}
            ORDER BY name LIMIT ?""", params + [limit])]


if __name__ == "__main__":
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # unit-check the two parsers that carry the most risk
    # 44927 is the anchor: Excel itself displays serial 44927 as 2023-01-01.
    assert excel_serial_to_iso(44927) == "2023-01-01", excel_serial_to_iso(44927)
    assert excel_serial_to_iso(40951) == "2012-02-12", excel_serial_to_iso(40951)
    assert excel_serial_to_iso("40951") == "2012-02-12"
    assert excel_serial_to_iso(0) is None
    assert excel_serial_to_iso(None) is None
    assert excel_serial_to_iso(999999) is None          # out of window
    assert parse_parcels("6894", "17") == ([("6894", "17")], True)
    assert parse_parcels("6894", "17-19")[0] == [
        ("6894", "17"), ("6894", "18"), ("6894", "19")]
    assert parse_parcels("", "")[1] is False
    assert parse_parcels("6894", "")[0] == [("6894", None)]
    # whitespace-separated lists, seen live as '6666  6665' / '146   463'
    assert parse_parcels("6666  6665", "146   463")[0] == [
        ("6665", "146"), ("6665", "463"), ("6666", "146"), ("6666", "463")]
    assert parse_parcels("6043", "1970 1972")[0] == [
        ("6043", "1970"), ("6043", "1972")]
    # '354/3' is sub-parcel 3 of parcel 354 - one parcel, not two
    assert parse_parcels("6020", "354/3")[0] == [("6020", "354")], \
        parse_parcels("6020", "354/3")
    # a huge span must not be expanded into thousands of invented parcels
    assert parse_parcels("6894", "1-9999")[0] == [("6894", "1"), ("6894", "9999")]
    print("parser self-tests passed\n")

    conn = db.get_conn()
    total = import_all(conn, progress=lambda k, n: print(f"  {k:11} {n:,}", end="\r"))
    print(f"\nimported {total:,} rows")
    print("counts:", json.dumps(counts(conn), ensure_ascii=False))
    print("\ntop committees by dispute volume:")
    for r in appraisal_activity(conn, limit=8):
        print(f"  {r['committee'][:20]:20} {r['rulings']:>5} rulings "
              f"({r['first_ruling']} .. {r['last_ruling']}) "
              f"{r['appraisers']} appraisers")
    print("\nprogress:", json.dumps(progress_summary(conn), ensure_ascii=False)[:400])
    conn.close()
