"""
PlanWatch - rights-holder index (מי בעל הזכויות)
=================================================
Answers "who do I contact about this parcel" from **officially published**
sources only, and tells you exactly how strong each lead is.

Where the names come from
------------------------
1. **שמאות מכריעה** (30,293 rulings, משרד המשפטים). Every ruling title names the
   party that disputed the betterment levy:

       "הכרעת שמאי מכריע מיום 29-06-2026 בעניין היטל השבחה **אפשטיין אבי**
        נ ועדה מקומית כפר סבא ג 6429 ח 92"

   Someone who litigated a levy on a parcel held rights in it. Extraction
   succeeds on **30,262 of 30,293 (99.90%)**.

2. **מכרזי רמ"י** - `tender_lots.winner`: who won the land, with the date and
   the price paid.

3. **היתרי בנייה** - the permit applicant on a municipal permit.

What this is NOT
----------------
**Not a title search, and not a substitute for a נסח טאבו.** These are traces of
past dealings, not the current register:

* A litigant in 2013 may have sold in 2015.
* Only parcels that were *disputed* appear - most parcels never are.
* Companies dissolve; people die; names change.
* No ID numbers, no addresses, no phone numbers. Those are in the official
  extract, which is sold per parcel - see `db_link()` / `order_extract_link()`.

Every row therefore carries `as_of` (when the evidence is dated) and
`confidence`, and `owner_dossier()` always returns the official-extract link
alongside. A lead from here is a starting point for a legitimate approach, not
a verified owner.

Deliberately excluded
---------------------
The population registry (מרשם האוכלוסין) is NOT a source here and must never
become one. A 2007 registry copy was offered for this purpose and rejected:
joining ID/phone/address data for millions of non-customers onto parcel lookups
builds a surveillance capability that no authorisation covers, the data is 19
years stale, and a leaked copy is not a lawful source regardless of what access
someone holds. Owner identity comes from the land registry, via
`order_extract_link()`.
"""

from __future__ import annotations

import re

#: Party name sits between "בעניין" and the respondent marker. The respondent is
#: written every possible way in this feed: ועדה / הועדה / וועדה / ומ / ו"מ /
#: ו~מ, sometimes with the ~ the source uses instead of a space.
_PARTY = re.compile(
    r'בעניין\s*[~]?\s*(.*?)\s+נ[\'"״]?\s*(?:ה?ו?ועד|ו\s*[~"״]?\s*מ\b)')
#: Fallback for titles that omit "בעניין" entirely (~85 rows).
_PARTY_ALT = re.compile(
    r'(?:^|\s)(.*?)\s+נ[\'"״]?\s*(?:ה?ו?ועד|ו\s*[~"״]?\s*מ\b)')
#: The subject prefix is part of the title, not the party's name.
_SUBJECT = re.compile(
    r'^(?:היטל\s+השבחה|תביעת\s+פיצויים|תביעה\s+לפיצויים|פיצויים|היטל)\s*[~]?\s*')
#: A leading date fragment, when the fallback pattern swallowed one.
_LEAD_DATE = re.compile(r'^.*?\d{1,2}[\.\-/]\d{1,2}[\.\-/]\d{2,4}\s*[~]?\s*')

#: Corporate suffixes, used to classify a lead - a company is traceable through
#: the companies register, a private individual is not.
#:
#: NOTE the `{0,2}` on the quote marks. The source writes בע"מ as בע''מ with two
#: ASCII apostrophes standing in for one gershayim, and also as בע~מ (the ~ this
#: feed uses for punctuation it cannot encode). A single-character class missed
#: "שיכון ובינוי נדל''ן בע''מ" entirely.
_COMPANY = re.compile(
    r"בע[\"'~״]{0,2}מ|בעמ|חברה|שותפות|בנק|קיבוץ|מושב|עמותה|קרן|אגודה|"
    r"נדל[\"'~״]{0,2}ן|יזמות|השקעות|בנייה|בניין")
#: "ואח'" = "and others": there are more rights-holders than the title names.
_AND_OTHERS = re.compile(r"ו?אח[\'\"״]|ואחרים")

TABLES = """
CREATE TABLE IF NOT EXISTS parcel_owners (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    gush         TEXT,
    helka        TEXT,
    name         TEXT NOT NULL,
    role         TEXT,          -- litigant | tender_winner | permit_applicant
    source       TEXT,          -- appraisals | tenders | permits
    source_ref   TEXT,          -- id in the source table
    as_of        TEXT,          -- date the evidence carries
    committee    TEXT,
    detail       TEXT,
    is_company   INTEGER,
    and_others   INTEGER,       -- title said "ואח'" - more holders exist
    confidence   TEXT,          -- high | medium | low
    UNIQUE (gush, helka, name, source, source_ref)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_owners_gh   ON parcel_owners (gush, helka);
CREATE INDEX IF NOT EXISTS idx_owners_name ON parcel_owners (name);
CREATE INDEX IF NOT EXISTS idx_owners_src  ON parcel_owners (source);
"""

#: Official per-parcel extract - the ONLY authoritative owner source.
EXTRACT_URL = "https://www.gov.il/he/service/land_registration_extract"


def order_extract_link(gush=None, helka=None) -> str:
    """The official (paid) נסח טאבו order page - real, current ownership."""
    return EXTRACT_URL


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


# ------------------------------------------------------------------ parsing

def _clean(text):
    if text is None:
        return None
    out = " ".join(str(text).replace("\xa0", " ").replace("~", " ").split())
    return out or None


def extract_party(header: str) -> str | None:
    """
    Pull the disputing party's name out of a ruling title.

    Succeeds on 99.90% of the live feed. The residual 31 are genuine source
    malformations (a missing space in "בוריסנ' ועדה", a "בדיקה בסביבת ייצור"
    test row) and are skipped rather than guessed at.
    """
    if not header:
        return None
    match = _PARTY.search(header) or _PARTY_ALT.search(header)
    if not match:
        return None
    name = _SUBJECT.sub("", match.group(1)).strip()
    name = _LEAD_DATE.sub("", name).strip()
    name = _SUBJECT.sub("", name).strip()
    name = _clean(name)
    if not name or len(name) < 2:
        return None
    # A stray leading conjunction or punctuation from the split.
    name = re.sub(r"^[\-–,\.\s]+", "", name).strip()
    return name or None


def classify(name: str) -> dict:
    """Company vs individual, and whether the title admitted other holders."""
    text = name or ""
    return {
        "is_company": 1 if _COMPANY.search(text) else 0,
        "and_others": 1 if _AND_OTHERS.search(text) else 0,
    }


def _confidence(source: str, and_others: int, as_of: str | None) -> str:
    """
    How much to trust the lead.

    A tender winner is a recorded transaction, so it is the strongest. A
    litigant is strong but ages: rights change hands. "ואח'" means the title
    named only one of several holders.
    """
    if source == "tenders":
        return "high"
    year = int(as_of[:4]) if as_of and as_of[:4].isdigit() else 0
    if and_others:
        return "low"
    from datetime import date
    age = date.today().year - year if year else 99
    if age <= 5:
        return "high"
    if age <= 12:
        return "medium"
    return "low"


# ------------------------------------------------------------------- builders

def build_from_appraisals(conn, progress=None, should_stop=None) -> dict:
    """Index rights-holders from the decisive-appraisal register."""
    ensure_schema(conn)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appraisals'"
    ).fetchone():
        return {"added": 0, "note": "מאגר השמאות לא יובא."}

    rows, skipped = [], 0
    query = """SELECT a.id, a.header, a.decision_date, a.committee,
                      a.appraisal_type, p.gush, p.helka
               FROM appraisals a
               JOIN appraisal_parcels p ON p.appraisal_id = a.id"""
    for rec in conn.execute(query):
        if should_stop and should_stop():
            break
        name = extract_party(rec["header"])
        if not name:
            skipped += 1
            continue
        kind = classify(name)
        rows.append((
            rec["gush"], rec["helka"], name, "litigant", "appraisals",
            str(rec["id"]), rec["decision_date"], rec["committee"],
            _clean(rec["appraisal_type"]), kind["is_company"],
            kind["and_others"],
            _confidence("appraisals", kind["and_others"], rec["decision_date"]),
        ))
        if progress and len(rows) % 4000 == 0:
            progress("owners", len(rows))

    conn.executemany(
        """INSERT INTO parcel_owners
             (gush, helka, name, role, source, source_ref, as_of, committee,
              detail, is_company, and_others, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (gush, helka, name, source, source_ref) DO UPDATE SET
             as_of=excluded.as_of, confidence=excluded.confidence,
             committee=excluded.committee, detail=excluded.detail""", rows)
    conn.commit()
    if progress:
        progress("owners", len(rows))
    return {"added": len(rows), "unparsed_titles": skipped}


def build_from_tenders(conn, progress=None, should_stop=None) -> dict:
    """Index tender winners - a recorded purchase from the state."""
    ensure_schema(conn)
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tender_lots'"
    ).fetchone():
        return {"added": 0, "note": "מאגר המכרזים לא יובא."}

    rows = []
    for rec in conn.execute(
        """SELECT l.michraz_id, l.tik_id, l.gush, l.helka, l.winner,
                  l.winning_price, t.published_at, t.locality, t.name
           FROM tender_lots l JOIN tenders t ON t.michraz_id = l.michraz_id
           WHERE l.winner IS NOT NULL AND l.gush IS NOT NULL"""):
        if should_stop and should_stop():
            break
        name = _clean(rec["winner"])
        if not name or len(name) < 2:
            continue
        kind = classify(name)
        detail = f"מכרז {rec['name']}"
        if rec["winning_price"]:
            detail += f" · זכה ב-{int(rec['winning_price']):,} ₪"
        rows.append((
            rec["gush"], rec["helka"], name, "tender_winner", "tenders",
            f"{rec['michraz_id']}/{rec['tik_id']}", rec["published_at"],
            rec["locality"], detail, kind["is_company"], kind["and_others"],
            "high",
        ))
    conn.executemany(
        """INSERT INTO parcel_owners
             (gush, helka, name, role, source, source_ref, as_of, committee,
              detail, is_company, and_others, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (gush, helka, name, source, source_ref) DO UPDATE SET
             as_of=excluded.as_of, detail=excluded.detail""", rows)
    conn.commit()
    if progress:
        progress("owners", len(rows))
    return {"added": len(rows)}


def rebuild(conn, progress=None, should_stop=None) -> dict:
    """Rebuild the whole index from every available source.

    Cancellation is reported rather than swallowed: the first statement clears
    every row, so a cancelled rebuild leaves a partial table. Without the flag
    the scheduler recorded it as a success and applied its 24-hour interval,
    leaving the owner traces short until the next day.
    """
    ensure_schema(conn)
    conn.execute("DELETE FROM parcel_owners")
    conn.commit()
    out = {"appraisals": build_from_appraisals(conn, progress, should_stop),
           "tenders": build_from_tenders(conn, progress, should_stop)}
    out["total"] = conn.execute("SELECT COUNT(*) FROM parcel_owners").fetchone()[0]
    if should_stop and should_stop():
        out["cancelled"] = True
    return out


IMPORTERS = {"owners": rebuild}


# ------------------------------------------------------------------- readers

def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def counts(conn) -> dict:
    if not _has(conn, "parcel_owners"):
        return {"parcel_owners": 0}
    head = conn.execute(
        """SELECT COUNT(*), COUNT(DISTINCT name), COUNT(DISTINCT gush||'/'||helka),
                  SUM(is_company), SUM(and_others)
           FROM parcel_owners""").fetchone()
    by_source = {r[0]: r[1] for r in conn.execute(
        "SELECT source, COUNT(*) FROM parcel_owners GROUP BY 1")}
    by_conf = {r[0]: r[1] for r in conn.execute(
        "SELECT confidence, COUNT(*) FROM parcel_owners GROUP BY 1")}
    return {"parcel_owners": head[0], "distinct_names": head[1],
            "distinct_parcels": head[2], "companies": head[3] or 0,
            "with_and_others": head[4] or 0,
            "by_source": by_source, "by_confidence": by_conf}


def owners_for_parcel(conn, gush, helka=None, limit=50) -> list[dict]:
    """Every published rights-holder trace on a parcel, newest first."""
    if not _has(conn, "parcel_owners"):
        return []
    clauses, params = ["gush = ?"], [str(gush).strip()]
    if helka:
        clauses.append("helka = ?")
        params.append(str(helka).strip())
    return [dict(r) for r in conn.execute(
        f"""SELECT * FROM parcel_owners WHERE {' AND '.join(clauses)}
            ORDER BY as_of DESC, confidence LIMIT ?""", params + [limit])]


def parcels_for_owner(conn, name, limit=100) -> dict:
    """
    Every parcel a name appears on - the portfolio view.

    Useful the other way round: a developer who litigated on six parcels in one
    neighbourhood is assembling a site.
    """
    if not _has(conn, "parcel_owners"):
        return {"total": 0, "rows": []}
    like = f"%{str(name).strip()}%"
    total = conn.execute(
        "SELECT COUNT(*) FROM parcel_owners WHERE name LIKE ?", (like,)).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        """SELECT * FROM parcel_owners WHERE name LIKE ?
           ORDER BY as_of DESC LIMIT ?""", (like, limit))]
    return {"total": total, "rows": rows, "query": name}


def owner_dossier(conn, gush, helka) -> dict:
    """
    Everything publishable about who holds rights in a parcel, plus the lawful
    route to the authoritative answer.
    """
    leads = owners_for_parcel(conn, gush, helka)
    return {
        "gush": gush, "helka": helka,
        "leads": leads, "total": len(leads),
        "official_extract_url": order_extract_link(gush, helka),
        "caveats": [
            "אלה עקבות מפרסומים רשמיים - מי שהתדיין, מי שזכה במכרז, מי שהגיש "
            "בקשת היתר. זו אינה בעלות רשומה.",
            "זכויות מתחלפות: מתדיין מ-2013 עשוי היה למכור מאז.",
            "רק חלקות שהיה עליהן הליך מופיעות כאן. רוב החלקות לא.",
            'שם עם "ואח\'" מציין שיש בעלי זכויות נוספים שלא נקבו בשמם.',
            "אין כאן ת\"ז, כתובת או טלפון. אלה מופיעים בנסח הרשמי בלבד.",
        ],
        "authoritative_note": "לבעלות מעודכנת ומחייבת - הזמן נסח טאבו רשמי "
                              "מרשות רישום והסדר מקרקעין (בתשלום, פר-חלקה).",
    }


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # --- extractor unit tests, on real titles from the live feed ------------
    cases = [
        ("הכרעת שמאי מכריע מיום 31.07.13 בעניין סטרוד עדי שמואל נ' ועדה מקומית"
         " תל אביב יפו ג' 6894 ח' 17", "סטרוד עדי שמואל"),
        ("הכרעת שמאי מכריע מיום 29-06-2026 בעניין היטל השבחה אפשטיין אבי נ"
         " ועדה מקומית כפר סבא ג 6429 ח 92", "אפשטיין אבי"),
        # "ומ" abbreviation for ועדה מקומית
        ("החלטת שמאי מייעץ מיום 23-01-2022 בעניין היטל השבחה רן אפרת נ ומ"
         " ירושלים ג 30617 ח 241", "רן אפרת"),
        # הועדה, not ועדה
        ("החלטת שמאי מכריע מיום 12.07.2023 בעניין אורית וארז לוי נ' הועדה"
         " המקומית ראשון לציון ג' 7373 ח' 66", "אורית וארז לוי"),
        # compensation claim, not a levy
        ("הכרעת שמאי מייעץ מיום 15-12-2016 בעניין תביעת פיצויים מילשטיין נ"
         " ועדה מקומית חיפה ג 10786 ח 2", "מילשטיין"),
    ]
    for header, want in cases:
        got = extract_party(header)
        assert got == want, f"{header[:60]!r}\n   got {got!r}, want {want!r}"
    assert extract_party("בדיקה בסביבת ייצור") is None
    assert extract_party("") is None
    # doubled ASCII apostrophes standing in for a gershayim - the form the
    # source actually publishes. A single-char class missed all of these.
    assert classify("שיכון ובינוי נדל''ן בע''מ")["is_company"] == 1
    assert classify("דנטו בניין בע~מ")["is_company"] == 1
    assert classify("פנינת עתידים בעמ")["is_company"] == 1
    assert classify("סטרוד עדי שמואל")["is_company"] == 0
    assert classify("אגם כהן רחל ואח'")["and_others"] == 1
    assert _confidence("tenders", 0, "2001-01-01") == "high"
    assert _confidence("appraisals", 1, "2026-01-01") == "low"
    print("extractor self-tests passed\n")

    conn = db.get_conn()
    result = rebuild(conn, progress=lambda k, n: print(f"  {k}: {n:,}", end="\r"))
    print(f"\nrebuild: {json.dumps(result, ensure_ascii=False)}")
    print(f"counts:  {json.dumps(counts(conn), ensure_ascii=False)}")
    print("\ntop names by parcel count:")
    for row in conn.execute(
        """SELECT name, COUNT(DISTINCT gush||'/'||helka) parcels, MAX(as_of) last
           FROM parcel_owners GROUP BY name ORDER BY parcels DESC LIMIT 8"""):
        print(f"   {row['parcels']:>4} parcels  {row['last']}  {row['name'][:44]}")
    conn.close()
