"""
PlanWatch - unified entity index (חיפוש והצלבה על כל המערכת)
=============================================================
One searchable index over **every** table that names a person, a company, a
place or a parcel, so a single query can be cross-referenced across all of them.

What you can search by
----------------------
| by | example | resolves against |
|----|---------|------------------|
| **שם** | `קידר מבנים` | בעלי זכויות · קבלנים · שמאים · מודדים · זוכי מכרזים · יזמים ומשווקים · גורמים מקדמים |
| **גוש/חלקה** | `6429/92` | חלקות · תכניות · שמאות · מכרזים · התחדשות · היתרים · פוליגונים · נכסים למכירה |
| **כתובת** | `דיזנגוף 45` | מבנים מסוכנים · מודדים · היתרי בנייה · מבנים מזוהים · ישובים |
| **מספר תכנית** | `408-0242412` | תכניות · מלאי רמ"י · התחדשות · תאי שטח · מכרזים |
| **מספר תיק בניין / היתר** | `30430120` | בקשות רישוי והיתרי בנייה |
| **שם מבנה / פרויקט** | `בית הדר`, `מחיר למשתכן` | מבנים מזוהים · מחיר למשתכן · הוצאות פיתוח |
| **ישוב / שכונה** | `כפר סבא` | כל טבלה שיש בה ישוב |
| **מספר מכרז / מתחם** | `162/2026`, `4001` | מכרזים · מתחמי התחדשות |

Why an index table rather than querying 32 tables live
------------------------------------------------------
A name search has to hit ~90k rows across nine tables with `LIKE '%…%'`, which
cannot use an index and takes seconds. `search_entities` is denormalised, has a
normalised key column that IS indexed, and answers in milliseconds. It is
rebuilt from the source tables, never edited by hand.

Name normalisation - and why it matters
---------------------------------------
The same company is written differently in every feed:

    "זלמן בראשי ואחיו בע''מ"   (שמאות - doubled ASCII apostrophes)
    "זלמן בראשי ואחיו בעמ"     (פנקס הקבלנים - no punctuation at all)
    "דנטו בניין בע~מ"          (~ standing in for a gershayim)

`norm_name()` folds all of these together. Measured on the live data: exact
matching links 428 owner names to contractors; normalised matching links **534**
- a 25% gain, i.e. 106 real people and companies that would otherwise look
unconnected.

ID numbers (ת"ז)
----------------
**Deliberately not indexed, and there is no field for one.** No open Israeli
source publishes ID numbers against parcels, and the only bulk source is a
leaked population-registry copy, which this project does not use (see
`owners_client` and `PROJECT-MAP.md`). Searching by ת"ז is served the lawful
way: `credentialed_client.fetch_extract()` returns the official land-registry
extract, which carries the ID, per parcel, audited, and is not stored here.

So: a **name** search finds every trace across the system; an **ID** is
retrieved for one parcel through the credentialed route. That is the difference
between an intelligence tool and a surveillance one, and it is on purpose.
"""

from __future__ import annotations

import re

TABLES = """
CREATE TABLE IF NOT EXISTS search_entities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,     -- person | company | place | parcel | plan
                                  -- | tender | complex | building | permit
                                  -- | project | listing | landuse
    label      TEXT NOT NULL,     -- what to show
    norm       TEXT NOT NULL,     -- normalised key, indexed
    source     TEXT NOT NULL,     -- table it came from
    source_ref TEXT,              -- primary key in that table
    role       TEXT,              -- why this entity exists in that source
    gush       TEXT,
    helka      TEXT,
    locality   TEXT,
    address    TEXT,
    plan_number TEXT,
    as_of      TEXT,
    detail     TEXT,
    lat        REAL,
    lon        REAL,
    UNIQUE (source, source_ref, kind, norm)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_se_norm     ON search_entities (norm);
CREATE INDEX IF NOT EXISTS idx_se_kind     ON search_entities (kind, norm);
CREATE INDEX IF NOT EXISTS idx_se_gh       ON search_entities (gush, helka);
CREATE INDEX IF NOT EXISTS idx_se_plan     ON search_entities (plan_number);
CREATE INDEX IF NOT EXISTS idx_se_locality ON search_entities (locality);
CREATE INDEX IF NOT EXISTS idx_se_source   ON search_entities (source);
"""

#: Corporate markers, folded to one spelling so the same firm matches itself.
_CORP = re.compile(r"בע\s*['\"~״]{0,2}\s*מ\b|\bבעמ\b")
_COMPANY_HINT = re.compile(
    r"בעמ|חברה|שותפות|בנק|קיבוץ|מושב|עמותה|קרן|אגודה|יזמות|השקעות|"
    r"בנייה|בניין|מבנים|נדלן|קבוצת|אחזקות")
_KEEP = re.compile(r"[^א-תa-zA-Z0-9 ]")
#: A gershayim BETWEEN two Hebrew letters is orthographic (נדל''ן, תמ''א) and
#: must vanish, not become a word break.
_INWORD_QUOTE = re.compile(r"(?<=[א-ת])'+(?=[א-ת])")


def norm_name(value) -> str:
    """
    Fold a name to a comparable key.

    Handles, in order: the `~` this project's feeds use for punctuation they
    cannot encode, doubled ASCII apostrophes standing in for a gershayim, the
    many spellings of בע"מ, and every remaining punctuation mark.
    """
    text = str(value or "").replace("\xa0", " ").replace("~", "'")
    text = text.replace('"', "'").replace("״", "'").replace("’", "'")
    text = _CORP.sub(" בעמ ", text)
    # In-word gershayim must be DELETED, not turned into a space. `_KEEP` maps
    # punctuation to a space, which split נדל''ן into "נדל ן" and stopped it
    # matching נדלן - the very fold this function exists to perform.
    text = _INWORD_QUOTE.sub("", text)
    text = _KEEP.sub(" ", text)
    return " ".join(text.split()).lower()


def guess_kind(name) -> str:
    """company vs person, from the corporate markers the feeds actually use."""
    return "company" if _COMPANY_HINT.search(norm_name(name)) else "person"


def norm_address(street, house=None, locality=None) -> str:
    """Normalised address key: street + number, locality kept separate."""
    parts = [str(street or "")]
    if house:
        parts.append(str(house))
    text = _KEEP.sub(" ", " ".join(parts).replace("~", " "))
    text = re.sub(r"\b(?:רחוב|רח|שדרות|שד|סמטת|דרך|מס)\b", " ", text)
    return " ".join(text.split()).lower()


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


_COLS = ("kind", "label", "norm", "source", "source_ref", "role", "gush",
         "helka", "locality", "address", "plan_number", "as_of", "detail",
         "lat", "lon")


def _insert(conn, rows) -> int:
    if not rows:
        return 0
    marks = ",".join("?" for _ in _COLS)
    conn.executemany(
        f"""INSERT INTO search_entities ({', '.join(_COLS)})
            VALUES ({marks})
            ON CONFLICT (source, source_ref, kind, norm) DO UPDATE SET
              label=excluded.label, role=excluded.role, gush=excluded.gush,
              helka=excluded.helka, locality=excluded.locality,
              address=excluded.address, plan_number=excluded.plan_number,
              as_of=excluded.as_of, detail=excluded.detail,
              lat=excluded.lat, lon=excluded.lon""", rows)
    conn.commit()
    return len(rows)


def _txt(value):
    if value is None:
        return None
    out = " ".join(str(value).replace("\xa0", " ").split())
    return out or None


# --------------------------------------------------------------- builders

def _people(conn) -> int:
    """Every named person or company, from all six sources that carry one."""
    rows = []

    if _has(conn, "parcel_owners"):
        for r in conn.execute(
            """SELECT id, name, role, gush, helka, locality_or_committee, as_of,
                      detail FROM (SELECT id, name, role, gush, helka,
                      committee AS locality_or_committee, as_of, detail
                      FROM parcel_owners)"""):
            name = _txt(r["name"])
            if not name:
                continue
            rows.append((guess_kind(name), name, norm_name(name),
                         "parcel_owners", str(r["id"]), r["role"], r["gush"],
                         r["helka"], r["locality_or_committee"], None, None,
                         r["as_of"], r["detail"], None, None))

    if _has(conn, "od_contractors"):
        for r in conn.execute(
            """SELECT license, name, city, branch, grade, phone, email
               FROM od_contractors WHERE name IS NOT NULL"""):
            name = _txt(r["name"])
            if not name:
                continue
            detail = " · ".join(x for x in (
                r["branch"], f"סיווג {r['grade']}" if r["grade"] else None,
                r["phone"], r["email"]) if x)
            rows.append((guess_kind(name), name, norm_name(name),
                         "od_contractors", str(r["license"]), "קבלן רשום",
                         None, None, _txt(r["city"]), None, None, None,
                         detail or None, None, None))

    if _has(conn, "od_appraisers"):
        for r in conn.execute(
            "SELECT license, name, city FROM od_appraisers WHERE name IS NOT NULL"):
            name = _txt(r["name"])
            if not name:
                continue
            rows.append(("person", name, norm_name(name), "od_appraisers",
                         str(r["license"]), "שמאי מקרקעין", None, None,
                         _txt(r["city"]), None, None, None, None, None, None))

    if _has(conn, "planners"):
        for r in conn.execute(
            """SELECT code, name, street, house_no, locality, phone, speciality
               FROM planners WHERE name IS NOT NULL"""):
            name = _txt(r["name"])
            if not name:
                continue
            addr = _txt(f"{r['street'] or ''} {r['house_no'] or ''}".strip())
            detail = " · ".join(x for x in (r["speciality"], r["phone"]) if x)
            rows.append((guess_kind(name), name, norm_name(name), "planners",
                         str(r["code"]), "מודד / מתכנן", None, None,
                         _txt(r["locality"]), addr, None, None,
                         detail or None, None, None))

    if _has(conn, "appraisals"):
        # The deciding appraiser is a professional worth finding by name too.
        for r in conn.execute(
            """SELECT id, appraiser, committee, decision_date, block_raw, plot_raw
               FROM appraisals WHERE appraiser IS NOT NULL"""):
            name = _txt(r["appraiser"])
            if not name:
                continue
            rows.append(("person", name, norm_name(name), "appraisals",
                         str(r["id"]), "שמאי מכריע", _txt(r["block_raw"]),
                         _txt(r["plot_raw"]), _txt(r["committee"]), None, None,
                         r["decision_date"], None, None, None))
    return _insert(conn, rows)


def _places(conn) -> int:
    """Addresses and localities."""
    rows = []
    if _has(conn, "muni_dangerous"):
        for r in conn.execute(
            """SELECT city, object_id, street, house_num, address, order_kind,
                      lat, lon FROM muni_dangerous"""):
            label = _txt(r["address"]) or _txt(
                f"{r['street'] or ''} {r['house_num'] or ''}")
            if not label:
                continue
            rows.append(("place", label,
                         norm_address(r["street"], r["house_num"]),
                         "muni_dangerous", f"{r['city']}/{r['object_id']}",
                         "מבנה מסוכן", None, None, _txt(r["city"]), label,
                         None, None, _txt(r["order_kind"]), r["lat"], r["lon"]))

    if _has(conn, "od_localities"):
        for r in conn.execute(
            "SELECT code, name, name_en, nafa, council FROM od_localities"):
            name = _txt(r["name"])
            if not name:
                continue
            detail = " · ".join(x for x in (r["nafa"], r["council"]) if x)
            rows.append(("place", name, norm_name(name), "od_localities",
                         str(r["code"]), "ישוב", None, None, name, None, None,
                         None, detail or None, None, None))

    if _has(conn, "muni_buildings"):
        # A named building ("בית הדר", "מגדל שלום") is how people refer to a
        # site in practice - by name, never by object_id. Year and floors ride
        # along so an address search shows the stock's age immediately.
        for r in conn.execute(
            """SELECT city, object_id, building_id, name, kind, floors,
                      year_built, height_m FROM muni_buildings
               WHERE name IS NOT NULL AND trim(name) <> ''"""):
            name = _txt(r["name"])
            if not name:
                continue
            detail = " · ".join(x for x in (
                _txt(r["kind"]),
                f"נבנה {r['year_built']}" if r["year_built"] else None,
                f"{r['floors']} קומות" if r["floors"] else None,
                f"גובה {r['height_m']:.0f} מ'" if r["height_m"] else None,
            ) if x)
            rows.append(("building", name, norm_name(name), "muni_buildings",
                         f"{r['city']}/{r['object_id']}", "מבנה מזוהה", None,
                         None, _txt(r["city"]), None, None, None,
                         detail or None, None, None))
    return _insert(conn, rows)


def _permits(conn) -> int:
    """Building permits and licence requests - searchable by address and file.

    A permit is the strongest signal that something is actually happening on a
    parcel, and the building-file number (``file_num``) is the key a user
    already holds from municipal correspondence. Both the address and the file
    number are indexed so either one finds the request.
    """
    rows = []
    if not _has(conn, "muni_permits"):
        return 0
    for r in conn.execute(
        """SELECT city, object_id, request_num, permit_num, addresses,
                  file_num, request_kind, housing_units, is_tama38,
                  permitted_at, requested_at, stage, archive_url,
                  min_lat, min_lon, max_lat, max_lon
           FROM muni_permits"""):
        # `addresses` holds every address a permit covers, comma separated for
        # an assembled site. Index the whole string as the label and the first
        # address as the normalised key so a street search still lands.
        address = _txt(r["addresses"])
        label = address or _txt(r["permit_num"]) or _txt(r["request_num"])
        if not label:
            continue
        primary = (address or "").split(",")[0].strip()
        detail = " · ".join(x for x in (
            _txt(r["stage"]),
            f"{r['housing_units']} יח\"ד" if r["housing_units"] else None,
            'תמ"א 38' if r["is_tama38"] else None,
            f"תיק בניין {_txt(r['file_num'])}" if r["file_num"] else None,
            _txt(r["request_kind"]),
        ) if x)
        # Permits carry a bounding box rather than a point; its centre is a
        # good enough pin for a map hit and keeps the row's lat/lon honest.
        lat = lon = None
        if r["min_lat"] is not None and r["max_lat"] is not None:
            lat = (r["min_lat"] + r["max_lat"]) / 2
            lon = (r["min_lon"] + r["max_lon"]) / 2
        ref = f"{r['city']}/{r['object_id']}"
        role = "היתר בנייה" if r["permit_num"] else "בקשה לרישוי"
        as_of = _txt(r["permitted_at"]) or _txt(r["requested_at"])
        keys = {norm_address(primary)} if primary else set()
        # The file and permit numbers are identifiers people search verbatim.
        for extra in (r["file_num"], r["permit_num"], r["request_num"]):
            if _txt(extra):
                keys.add(norm_name(extra))
        for key in filter(None, keys):
            rows.append(("permit", label, key, "muni_permits", f"{ref}:{key}",
                         role, None, None, _txt(r["city"]), address, None,
                         as_of, detail or None, lat, lon))
    return _insert(conn, rows)


def _projects(conn) -> int:
    """Market-side projects: Mehir LaMishtaken, RMI inventory, development costs.

    These name the promoters and marketing companies that also appear as
    contractors and owners elsewhere, so indexing them turns three market
    tables into cross-source confirmations of one firm.
    """
    rows = []
    if _has(conn, "ml_projects"):
        for r in conn.execute(
            """SELECT lottery_id, project_name, locality, neighborhood, units,
                      price_per_m2, status, provider, lottery_date
               FROM ml_projects"""):
            detail = " · ".join(x for x in (
                _txt(r["status"]),
                f"{r['units']} יח\"ד" if r["units"] else None,
                f"{r['price_per_m2']:,.0f} ש\"ח למ\"ר" if r["price_per_m2"] else None,
                _txt(r["neighborhood"]),
            ) if x)
            name = _txt(r["project_name"])
            if name:
                rows.append(("project", f"מחיר למשתכן: {name}", norm_name(name),
                             "ml_projects", str(r["lottery_id"]),
                             "פרויקט מחיר למשתכן", None, None, _txt(r["locality"]),
                             None, None, _txt(r["lottery_date"]), detail or None,
                             None, None))
            # The provider is a company worth finding on its own name.
            provider = _txt(r["provider"])
            if provider:
                rows.append((guess_kind(provider), provider, norm_name(provider),
                             "ml_projects", f"{r['lottery_id']}:provider",
                             "יזם/משווק מחיר למשתכן", None, None,
                             _txt(r["locality"]), None, None,
                             _txt(r["lottery_date"]),
                             _txt(r["project_name"]), None, None))

    if _has(conn, "rami_inventory"):
        for r in conn.execute(
            """SELECT rowid, plan_number, plan_name, stage, promoter, locality,
                      units_potential, approve_date FROM rami_inventory"""):
            detail = " · ".join(x for x in (
                _txt(r["stage"]),
                f"{r['units_potential']:,} יח\"ד בפוטנציאל"
                if r["units_potential"] else None,
            ) if x)
            label = _txt(r["plan_name"]) or _txt(r["plan_number"])
            for key in filter(None, {norm_name(r["plan_number"]),
                                     norm_name(r["plan_name"])}):
                rows.append(("plan", label or key, key, "rami_inventory",
                             f"{r['rowid']}:{key}", "מלאי תכנוני רמ\"י", None,
                             None, _txt(r["locality"]), None,
                             _txt(r["plan_number"]), _txt(r["approve_date"]),
                             detail or None, None, None))
            promoter = _txt(r["promoter"])
            if promoter:
                rows.append((guess_kind(promoter), promoter, norm_name(promoter),
                             "rami_inventory", f"{r['rowid']}:promoter",
                             "גורם מקדם תכנית", None, None, _txt(r["locality"]),
                             None, _txt(r["plan_number"]),
                             _txt(r["approve_date"]), label, None, None))

    if _has(conn, "dev_costs"):
        for r in conn.execute(
            """SELECT rowid, project_id, project_name, site_name, locality,
                      district, units, status, develop_pay FROM dev_costs"""):
            name = _txt(r["project_name"]) or _txt(r["site_name"])
            if not name:
                continue
            detail = " · ".join(x for x in (
                _txt(r["status"]),
                f"{r['units']} יח\"ד" if r["units"] else None,
                f"הוצאות פיתוח {r['develop_pay']:,.0f} ש\"ח"
                if r["develop_pay"] else None,
                _txt(r["district"]),
            ) if x)
            rows.append(("project", name, norm_name(name), "dev_costs",
                         str(r["rowid"]), "אתר בהוצאות פיתוח", None, None,
                         _txt(r["locality"]), None, None, None,
                         detail or None, None, None))
    return _insert(conn, rows)


def _listings(conn) -> int:
    """For-sale listings, when a licensed feed has been imported.

    Empty until ``market_client.import_listings_csv`` is pointed at a feed, but
    indexed unconditionally so a licensed import becomes searchable - and
    cross-referenced against parcels and plans - with no further change.
    """
    rows = []
    if not _has(conn, "listings"):
        return 0
    for r in conn.execute(
        """SELECT id, source, gush, helka, address, locality, neighborhood,
                  property_type, rooms, area_m2, price, price_per_m2,
                  listed_at, lat, lon FROM listings"""):
        address = _txt(r["address"])
        gush, helka = _txt(r["gush"]), _txt(r["helka"])
        label = address or (f"גוש {gush} חלקה {helka}" if gush else None)
        if not label:
            continue
        detail = " · ".join(x for x in (
            _txt(r["property_type"]),
            f"{r['rooms']:g} חדרים" if r["rooms"] else None,
            f"{r['area_m2']:,.0f} מ\"ר" if r["area_m2"] else None,
            f"{r['price']:,.0f} ש\"ח" if r["price"] else None,
            f"{r['price_per_m2']:,.0f} ש\"ח למ\"ר" if r["price_per_m2"] else None,
            _txt(r["neighborhood"]), _txt(r["source"]),
        ) if x)
        keys = set()
        if address:
            keys.add(norm_address(address))
        if gush:
            keys.add(f"{gush}/{helka or ''}")
        for key in filter(None, keys):
            rows.append(("listing", label, key, "listings", f"{r['id']}:{key}",
                         "נכס למכירה", gush, helka, _txt(r["locality"]),
                         address, None, _txt(r["listed_at"]), detail or None,
                         r["lat"], r["lon"]))
    return _insert(conn, rows)


def _parcels(conn) -> int:
    """Parcels, from every table that identifies one."""
    rows = []
    sources = [
        ("muni_parcels", """SELECT city||'/'||object_id ref, gush, helka,
             city AS loc, area_computed_m2 AS extra FROM muni_parcels
             WHERE gush IS NOT NULL""", "חלקה (פוליגון)"),
        ("tender_lots", """SELECT michraz_id||'/'||tik_id ref, gush, helka,
             NULL AS loc, area_m2 AS extra FROM tender_lots
             WHERE gush IS NOT NULL""", "מגרש במכרז"),
        ("building_progress", """SELECT id ref, gush, helka, locality AS loc,
             units AS extra FROM building_progress WHERE gush IS NOT NULL""",
         "מבנה בבנייה"),
    ]
    for table, sql, role in sources:
        if not _has(conn, table):
            continue
        for r in conn.execute(sql):
            gush, helka = _txt(r["gush"]), _txt(r["helka"])
            if not gush:
                continue
            label = f"גוש {gush}" + (f" חלקה {helka}" if helka else "")
            detail = f"{r['extra']:,.0f}" if isinstance(
                r["extra"], (int, float)) and r["extra"] else None
            rows.append(("parcel", label, f"{gush}/{helka or ''}", table,
                         str(r["ref"]), role, gush, helka, _txt(r["loc"]),
                         None, None, None, detail, None, None))

    if _has(conn, "appraisal_docs"):
        # The parsed decision documents carry the parcel the appraisal actually
        # concerned, which is frequently more precise than the register's own
        # block_raw/plot_raw. Only confident parses are indexed.
        for r in conn.execute(
            """SELECT appraisal_id, doc_gush, doc_helka, doc_appraiser,
                      amount_nis, effective_date, is_tama38, parcel_scope
               FROM appraisal_docs
               WHERE doc_gush IS NOT NULL AND parcel_confident = 1"""):
            gush, helka = _txt(r["doc_gush"]), _txt(r["doc_helka"])
            if not gush:
                continue
            label = f"גוש {gush}" + (f" חלקה {helka}" if helka else "")
            detail = " · ".join(x for x in (
                _txt(r["doc_appraiser"]),
                f"{r['amount_nis']:,.0f} ש\"ח" if r["amount_nis"] else None,
                'תמ"א 38' if r["is_tama38"] else None,
                _txt(r["parcel_scope"]),
            ) if x)
            rows.append(("parcel", label, f"{gush}/{helka or ''}",
                         "appraisal_docs", str(r["appraisal_id"]),
                         "חלקה בהחלטת שמאי", gush, helka, None, None, None,
                         _txt(r["effective_date"]), detail or None, None, None))
    return _insert(conn, rows)


def _plans(conn) -> int:
    """Plans, renewal complexes and tenders - all searchable by their number."""
    rows = []
    if _has(conn, "plans"):
        for r in conn.execute(
            """SELECT object_id, pl_number, pl_name, jurisdiction_name,
                      district_name, last_update, housing_units, station
               FROM plans WHERE pl_number <> ''"""):
            label = _txt(r["pl_name"]) or _txt(r["pl_number"])
            detail = " · ".join(x for x in (
                r["station"],
                f"{r['housing_units']:,} יח\"ד" if r["housing_units"] else None
            ) if x)
            rows.append(("plan", label, norm_name(r["pl_number"]), "plans",
                         str(r["object_id"]), "תכנית", None, None,
                         _txt(r["jurisdiction_name"]), None,
                         _txt(r["pl_number"]), r["last_update"],
                         detail or None, None, None))

    if _has(conn, "urban_renewal"):
        for r in conn.execute(
            """SELECT rowid, mitham_id, name, locality, plan_number, status,
                      units_existing FROM urban_renewal"""):
            label = _txt(r["name"]) or f"מתחם {r['mitham_id']}"
            detail = " · ".join(x for x in (
                r["status"],
                f"{r['units_existing']} דירות קיימות" if r["units_existing"] else None
            ) if x)
            for key in filter(None, (_txt(r["mitham_id"]), _txt(r["name"]))):
                rows.append(("complex", label, norm_name(key), "urban_renewal",
                             f"{r['rowid']}:{key}", "מתחם התחדשות", None, None,
                             _txt(r["locality"]), None, _txt(r["plan_number"]),
                             None, detail or None, None, None))

    if _has(conn, "tenders"):
        for r in conn.execute(
            """SELECT michraz_id, name, locality, neighborhood, status,
                      housing_units, published_at FROM tenders"""):
            label = f"מכרז {_txt(r['name']) or r['michraz_id']}"
            detail = " · ".join(x for x in (
                r["status"],
                f"{r['housing_units']} יח\"ד" if r["housing_units"] else None,
                _txt(r["neighborhood"])) if x)
            rows.append(("tender", label, norm_name(r["name"]), "tenders",
                         str(r["michraz_id"]), "מכרז רמ\"י", None, None,
                         _txt(r["locality"]), None, None, r["published_at"],
                         detail or None, None, None))

    if _has(conn, "plan_landuse"):
        # A תא שטח is what a plan's provisions are actually written against.
        # Indexing the cell by its plan number lets a plan search surface the
        # designations and their areas, not just the plan's title row.
        for r in conn.execute(
            """SELECT object_id, pl_number, pl_name, cell_num, mavat_name,
                      legal_area_dunam, shape_area_m2, last_update
               FROM plan_landuse WHERE pl_number IS NOT NULL
                                   AND pl_number <> ''"""):
            cell = _txt(r["cell_num"])
            plan_number = _txt(r["pl_number"])
            label = f"תא שטח {cell}" if cell else _txt(r["pl_name"]) or plan_number
            area = r["legal_area_dunam"] or (
                (r["shape_area_m2"] / 1000) if r["shape_area_m2"] else None)
            detail = " · ".join(x for x in (
                _txt(r["mavat_name"]),
                f"{area:,.1f} דונם" if area else None,
                _txt(r["pl_name"]),
            ) if x)
            rows.append(("landuse", label, norm_name(plan_number),
                         "plan_landuse", str(r["object_id"]),
                         _txt(r["mavat_name"]) or "תא שטח", None, None, None,
                         None, plan_number, _txt(r["last_update"]),
                         detail or None, None, None))
    return _insert(conn, rows)


BUILDERS = {"people": _people, "places": _places, "parcels": _parcels,
            "plans": _plans, "permits": _permits, "projects": _projects,
            "listings": _listings}


def rebuild(conn, progress=None, should_stop=None) -> dict:
    """Rebuild the whole index. Safe to re-run; it is derived data.

    Cancellation is reported, not swallowed. The first statement deletes every
    row, so a cancelled rebuild leaves a PARTIAL index - and returning a plain
    dict made the scheduler record that as a success, apply its 24-hour
    interval, and leave the search index short (or empty) until the next day.
    `run_job` treats a `cancelled` key as a failed run, so the next cycle
    retries instead.
    """
    ensure_schema(conn)
    conn.execute("DELETE FROM search_entities")
    conn.commit()
    out = {}
    cancelled = False
    for key, fn in BUILDERS.items():
        if should_stop and should_stop():
            cancelled = True
            break
        out[key] = fn(conn)
        if progress:
            progress(f"index:{key}", out[key])
    out["total"] = conn.execute(
        "SELECT COUNT(*) FROM search_entities").fetchone()[0]
    if cancelled:
        out["cancelled"] = True
    return out


IMPORTERS = {"index": rebuild}


# ----------------------------------------------------------------- readers

def counts(conn) -> dict:
    if not _has(conn, "search_entities"):
        return {"search_entities": 0}
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, COUNT(*) FROM search_entities GROUP BY 1 ORDER BY 2 DESC")}
    by_source = {r[0]: r[1] for r in conn.execute(
        "SELECT source, COUNT(*) FROM search_entities GROUP BY 1 ORDER BY 2 DESC")}
    return {
        "search_entities": conn.execute(
            "SELECT COUNT(*) FROM search_entities").fetchone()[0],
        "distinct_names": conn.execute(
            "SELECT COUNT(DISTINCT norm) FROM search_entities").fetchone()[0],
        "by_kind": by_kind, "by_source": by_source,
        # The index holds no ID numbers. That is stated once, where it is
        # actionable - on a query that actually looks like an ID (see
        # `classify_query`) - rather than repeated on every routine response.
        "no_id_numbers": True,
    }


#: `NNN-NNNNNNN` plan numbers must be tested BEFORE the bare parcel shape, or
#: "507-0584706" is read as "גוש 507 חלקה 05847". Same trap as dashboard.py.
_PLAN_RE = re.compile(r"\b\d{3}-\d{6,8}\b")
_PARCEL_WORDED = re.compile(r"גוש\s*(\d{1,6})\D{0,12}?(\d{1,5})")
_PARCEL_BARE = re.compile(r"\b(\d{3,6})\s*[/\\-]\s*(\d{1,4})\b")
_ID_LIKE = re.compile(r"^\d{9}$")


def classify_query(text: str) -> dict:
    """
    What did the user type? Drives which indexes are consulted.

    A bare 9-digit number is flagged as `id_number` and explicitly NOT searched:
    the system holds no ID numbers, and silently returning zero results would
    read as "this person owns nothing".
    """
    text = (text or "").strip()
    out = {"query": text, "kinds": []}
    if not text:
        return out
    if _ID_LIKE.match(text.replace("-", "")):
        out["kinds"].append("id_number")
        out["id_note"] = (
            'מספר בן 9 ספרות זוהה כת"ז. המערכת אינה מחזיקה מספרי זהות '
            'ולכן לא ניתן לחפש לפיהם. לזהות בעלים רשומה — הזמן נסח טאבו '
            'רשמי לפי גוש/חלקה (POST /api/tabu/extract).')
        return out
    worded = _PARCEL_WORDED.search(text)
    if worded:
        out["kinds"].append("parcel")
        out["gush"], out["helka"] = worded.group(1), worded.group(2)
    elif _PLAN_RE.search(text):
        out["kinds"].append("plan")
        out["plan_number"] = _PLAN_RE.search(text).group(0)
    else:
        bare = _PARCEL_BARE.search(text)
        if bare:
            out["kinds"].append("parcel")
            out["gush"], out["helka"] = bare.group(1), bare.group(2)
    if re.search(r"[א-תa-zA-Z]", text):
        out["kinds"].append("name")
    if not out["kinds"]:
        out["kinds"].append("name")
    return out


def search(conn, text, *, kind=None, limit=60, offset=0) -> dict:
    """
    One query, every source. Groups hits by entity so cross-source links are
    visible rather than buried in a flat list.
    """
    if not _has(conn, "search_entities"):
        return {"total": 0, "groups": [],
                "note": "האינדקס לא נבנה. הרץ ייבוא 'index'."}
    parsed = classify_query(text)
    if "id_number" in parsed["kinds"]:
        return {"total": 0, "groups": [], "query": text,
                "classified": parsed["kinds"], "id_note": parsed["id_note"]}

    clauses, params = [], []
    if parsed.get("gush"):
        clauses.append("(gush = ? AND (helka = ? OR ? IS NULL))")
        params += [parsed["gush"], parsed.get("helka"), parsed.get("helka")]
    if parsed.get("plan_number"):
        clauses.append("plan_number LIKE ?")
        params.append(f"%{parsed['plan_number']}%")
    if "name" in parsed["kinds"]:
        key = norm_name(text)
        addr = norm_address(text)
        clauses.append("(norm LIKE ? OR norm LIKE ? OR locality LIKE ?)")
        params += [f"%{key}%", f"%{addr}%", f"%{text.strip()}%"]
    if not clauses:
        return {"total": 0, "groups": [], "query": text}

    where = "(" + " OR ".join(clauses) + ")"
    if kind:
        where += " AND kind = ?"
        params.append(kind)

    total = conn.execute(
        f"SELECT COUNT(*) FROM search_entities WHERE {where}", params).fetchone()[0]
    # Order by `norm` FIRST, so every row of one entity is adjacent, and take a
    # generous row budget. Ordering by kind/date and then truncating split
    # entities apart: "קידר מבנים" exists in both parcel_owners and
    # od_contractors, but the contractor row fell outside the LIMIT and the
    # result claimed a single source - hiding exactly the cross-source link
    # this function exists to surface.
    rows = [dict(r) for r in conn.execute(
        f"""SELECT * FROM search_entities WHERE {where}
            ORDER BY norm,
                     CASE kind WHEN 'person' THEN 0 WHEN 'company' THEN 1
                          WHEN 'parcel' THEN 2 WHEN 'plan' THEN 3 ELSE 4 END,
                     as_of DESC
            LIMIT ?""", params + [max(2000, limit * 40)])]

    # Group by normalised identity: the whole point is seeing that a name in
    # the appraisal register is the same firm as in the contractors register.
    groups: dict = {}
    for row in rows:
        key = (row["kind"], row["norm"])
        item = groups.setdefault(key, {
            "kind": row["kind"], "label": row["label"], "norm": row["norm"],
            "sources": [], "parcels": set(), "localities": set(),
        })
        item["sources"].append({
            "source": row["source"], "ref": row["source_ref"],
            "role": row["role"], "as_of": row["as_of"],
            "detail": row["detail"], "gush": row["gush"], "helka": row["helka"],
            "locality": row["locality"], "address": row["address"],
            "plan_number": row["plan_number"],
            "lat": row["lat"], "lon": row["lon"],
        })
        if row["gush"]:
            item["parcels"].add(f"{row['gush']}/{row['helka'] or ''}")
        if row["locality"]:
            item["localities"].add(row["locality"])

    out = []
    for item in groups.values():
        item["parcels"] = sorted(item["parcels"])[:40]
        item["localities"] = sorted(item["localities"])[:12]
        item["source_count"] = len({s["source"] for s in item["sources"]})
        item["hit_count"] = len(item["sources"])
        item["cross_source"] = item["source_count"] > 1
        out.append(item)
    # Entities confirmed by several independent sources first.
    out.sort(key=lambda x: (-x["source_count"], -x["hit_count"]))

    # Paging applies to ENTITIES, not raw rows: grouping happens first, so a
    # page boundary never splits one entity's sources across two pages.
    return {"query": text, "classified": parsed["kinds"], "total": total,
            "entities": len(out), "limit": limit, "offset": offset,
            "groups": out[offset:offset + limit],
            "cross_source_hits": sum(1 for x in out if x["cross_source"])}


def entities_for_parcel(conn, gush, helka, limit=120) -> list[dict]:
    """Every indexed entity tied to one parcel, grouped by identity.

    The parcel view previously had to be cross-referenced by hand: an owner
    name shown there gave no hint that the same firm is also a registered
    contractor and a tender winner. Grouping by normalised name surfaces that
    directly, with `source_count` saying how many INDEPENDENT registers confirm
    it - which is the difference between a lead and a fact.
    """
    if not _has(conn, "search_entities"):
        return []
    gush, helka = str(gush or "").strip(), str(helka or "").strip()
    if not gush:
        return []
    rows = [dict(r) for r in conn.execute(
        """SELECT kind, label, norm, source, source_ref, role, locality,
                  address, plan_number, as_of, detail
             FROM search_entities
            WHERE gush = ? AND (helka = ? OR ? = '')
         ORDER BY norm LIMIT ?""", (gush, helka, helka, limit))]

    groups: dict = {}
    for row in rows:
        item = groups.setdefault((row["kind"], row["norm"]), {
            "kind": row["kind"], "label": row["label"], "sources": [],
        })
        item["sources"].append({k: row[k] for k in
                                ("source", "source_ref", "role", "as_of",
                                 "detail", "locality", "plan_number")})
    out = []
    for item in groups.values():
        item["source_count"] = len({s["source"] for s in item["sources"]})
        item["cross_source"] = item["source_count"] > 1
        out.append(item)
    # Multiply-confirmed entities first; they are the ones worth acting on.
    out.sort(key=lambda x: (-x["source_count"], -len(x["sources"])))
    return out


def crosswalk(conn, name, limit=200) -> dict:
    """
    Everything the system knows about one name, grouped by source.

    This is the cross-reference view: the same normalised name appearing in the
    appraisal register, the contractors register and a tender result is one
    entity with three independent confirmations.
    """
    if not _has(conn, "search_entities"):
        return {"sources": {}}
    key = norm_name(name)
    rows = [dict(r) for r in conn.execute(
        """SELECT * FROM search_entities WHERE norm = ? OR norm LIKE ?
           ORDER BY as_of DESC LIMIT ?""", (key, f"%{key}%", limit))]
    by_source: dict = {}
    parcels, localities = set(), set()
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)
        if row["gush"]:
            parcels.add(f"{row['gush']}/{row['helka'] or ''}")
        if row["locality"]:
            localities.add(row["locality"])
    return {
        "query": name, "norm": key, "total": len(rows),
        "sources": by_source, "source_count": len(by_source),
        "parcels": sorted(parcels), "localities": sorted(localities),
        "kind": rows[0]["kind"] if rows else None,
    }


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # --- normalisation unit tests, on the real spellings in this data --------
    assert norm_name("זלמן בראשי ואחיו בע''מ") == norm_name("זלמן בראשי ואחיו בעמ")
    assert norm_name("דנטו בניין בע~מ") == norm_name('דנטו בניין בע"מ')
    assert norm_name("שיכון ובינוי נדל''ן בע''מ") == norm_name("שיכון ובינוי נדלן בעמ")
    assert guess_kind("קידר מבנים בעמ") == "company"
    assert guess_kind("אפשטיין אבי") == "person"
    assert norm_address("רחוב דיזנגוף", "45") == norm_address("דיזנגוף", "45")
    # plan number must not be read as a parcel
    assert classify_query("507-0584706")["kinds"] == ["plan"], \
        classify_query("507-0584706")
    assert classify_query("גוש 6429 חלקה 92")["gush"] == "6429"
    assert classify_query("6429/92")["helka"] == "92"
    # a 9-digit number is an ID: flagged, never silently searched
    q = classify_query("123456789")
    assert q["kinds"] == ["id_number"] and "id_note" in q, q
    assert "name" in classify_query("קידר מבנים")["kinds"]
    print("index self-tests passed\n")

    conn = db.get_conn()
    result = rebuild(conn, progress=lambda k, n: print(f"  {k}: {n:,}"))
    print(f"\nrebuild: {json.dumps(result, ensure_ascii=False)}")
    print(f"counts:  {json.dumps(counts(conn), ensure_ascii=False)[:400]}")
    for probe in ("קידר מבנים", "גוש 6429 חלקה 92", "דיזנגוף 45", "123456789"):
        res = search(conn, probe, limit=3)
        print(f"\n[{probe}] classified={res.get('classified')} "
              f"total={res['total']} cross_source={res.get('cross_source_hits')}")
        if res.get("id_note"):
            print("   ", res["id_note"][:110])
        for g in res["groups"][:3]:
            srcs = ", ".join(sorted({s["source"] for s in g["sources"]}))
            print(f"    {g['kind']:8} {g['label'][:34]:34} "
                  f"{g['source_count']} sources ({srcs})")
    conn.close()
