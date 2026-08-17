"""
PlanWatch - document reader (קריאת מסמכים)
===========================================
Fetches the actual ruling documents behind `appraisals.link` and extracts what
can be extracted *safely*, from the ruling text itself.

The source is `free-justice.openapi.gov.il`, which serves the שומה מכרעת as an
openly-readable PDF (verified: HTTP 200, `application/pdf`, real text layer -
no OCR needed, no login, no captcha). 30,293 rulings, all with a unique link.

WHY THIS MODULE REFUSES TO GUESS AN AMOUNT
------------------------------------------
The obvious goal is "get the levy amount". Measured on a random sample of 14
rulings from 2022 onwards, extracting a single trustworthy number is possible
in **2 of 14 cases (14%)**:

    single unambiguous amount   2
    2+ amounts in the section   6   <- ambiguous
    section found, no amount    4   <- amount is in a table the text layer flattens
    no החלטה section at all     2

Concrete failure that killed the naive approach - ruling 29769's operative
section contains `221,844,149` and `908,429` side by side, a factor of 244
apart. Ruling 24113's decision table reads `109,551 ₪` (the השבחה) next to
`54,776 ₪` (the levy, 50% of it): picking "the biggest" or "the first" number
is wrong in opposite directions on those two documents.

So `extract()` returns a **candidate list with an explicit confidence**, and
`amount_nis` is populated ONLY when exactly one candidate survives. Everything
else is stored as `amount_status` = `ambiguous` / `not_found` / `no_section`,
with the candidates kept for a human to adjudicate. An `amount_nis` from this
module is therefore trustworthy; a NULL one is honest.

WHAT IS RELIABLY EXTRACTABLE
----------------------------
Independent of the amount, every document yields verifiable facts that are used
to CROSS-CHECK the CSV metadata (see `verify_against_csv`):

* גוש / חלקה as written in the ruling's own heading
* the מועד קובע (effective date) and the plan numbers cited
* page count, appraiser name, whether it is a תמ"א 38 matter
* the "החלטה" section text itself, for display

That cross-check is the real prize: it is an independent second source for the
parcel identity, which is what every join in PlanWatch depends on.
"""

from __future__ import annotations

import io
import json
import os
import re
from legacy.tools.http_client import build_session

#: pypdf is imported lazily so the rest of PlanWatch keeps working without it.
_PYPDF_HINT = ("pypdf is required to read documents: "
               "python -m pip install pypdf")

TABLES = """
CREATE TABLE IF NOT EXISTS appraisal_docs (
    appraisal_id  INTEGER PRIMARY KEY,
    /*
     * `link` is the STABLE identity here, not appraisal_id.
     *
     * `import_appraisals` does DELETE + INSERT, and AUTOINCREMENT continues
     * from the old high-water mark, so every appraisal id shifts on each
     * rebuild: a document fetched for id 28714 became id 59007 for the same
     * ruling. All 191 stored documents were silently orphaned - the download
     * and parse work was intact but unreachable, and `fetch_batch` would have
     * re-downloaded every one. `relink()` repairs by link.
     */
    link          TEXT,
    http_status   INTEGER,
    content_type  TEXT,
    bytes         INTEGER,
    pages         INTEGER,
    chars         INTEGER,          -- extracted text length; 0 => scanned image
    has_text      INTEGER,          -- 0 when the PDF has no usable text layer

    /* Amount: populated ONLY when exactly one candidate survives. */
    amount_nis    REAL,
    amount_status TEXT,             -- ok | ambiguous | not_found | no_section
                                    -- | no_text | fetch_error
    candidates_json TEXT,           -- every money figure seen in the section

    /* Independently extracted facts, used to cross-check the CSV. */
    doc_gush      TEXT,
    doc_helka     TEXT,
    doc_helka_range TEXT,           -- JSON list when the ruling states a range
    parcel_scope  TEXT,             -- decision | letterhead | body
    parcel_confident INTEGER,       -- 0 when the parcel came from the body
    doc_appraiser TEXT,
    doc_plans_json TEXT,            -- plan numbers cited in the ruling
    effective_date TEXT,            -- מועד קובע, ISO
    is_tama38     INTEGER,
    decision_text TEXT,             -- the החלטה section, trimmed

    error         TEXT,
    fetched_at    TEXT
);
"""

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_link ON appraisal_docs (link);
CREATE INDEX IF NOT EXISTS idx_docs_status ON appraisal_docs (amount_status);
CREATE INDEX IF NOT EXISTS idx_docs_gh     ON appraisal_docs (doc_gush, doc_helka);
"""

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session(total_retries=2, backoff_factor=0.8)
    return _session


#: Columns added after this table first shipped. `CREATE TABLE IF NOT EXISTS`
#: will NOT alter an existing table, so without this an older database keeps
#: failing on every new column - the same trap that broke `min_lon` and
#: `pl_number_sq` earlier in this project. Third time, hence the explicit list.
_ADDED_COLUMNS = {
    "doc_helka_range": "TEXT",
    "parcel_scope": "TEXT",
    "parcel_confident": "INTEGER",
}


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    existing = {r["name"] for r in conn.execute("PRAGMA table_xinfo(appraisal_docs)")}
    for column, decl in _ADDED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE appraisal_docs ADD COLUMN {column} {decl}")
    # The UNIQUE index on `link` cannot be created while duplicates exist, and
    # duplicates DO exist in any database written before this fix: an appraisals
    # rebuild shifted every id, so the same PDF was fetched again under a new
    # id. Measured on this database: 191 rows for 151 distinct links, i.e. 40
    # documents downloaded twice. Keep the newest row per link and drop the rest.
    dupes = conn.execute(
        "SELECT COUNT(*) FROM (SELECT link FROM appraisal_docs"
        " WHERE link IS NOT NULL GROUP BY link HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if dupes:
        conn.execute(
            """DELETE FROM appraisal_docs WHERE rowid NOT IN (
                 SELECT MAX(rowid) FROM appraisal_docs
                 WHERE link IS NOT NULL GROUP BY link)
               AND link IS NOT NULL""")
        conn.commit()
    conn.executescript(INDEXES)
    conn.commit()


# ------------------------------------------------------------------- patterns

#: The operative section. Anchored to a line start so a mid-sentence mention of
#: "החלטה" (e.g. "ההחלטה המשפטית בבית המשפט העליון") does not open a section -
#: that exact false positive was observed on ruling 24156.
_DECISION = re.compile(
    r'(?:^|\n)[ \t]*(?:\d+[\.\)]\s*)?'
    r'(החלטה|הכרעה|החלטתי|לסיכום|סיכום ההשבחה)'
    r'\s*[:\.\-]?\s*(?:\n|$| )')

#: Money: thousands-separated, followed by a shekel marker. A bare number is NOT
#: money - the documents are full of areas in מ"ר and coefficients.
_MONEY = re.compile(
    r'(\d{1,3}(?:,\d{3})+(?:\.\d+)?)\s*(?:₪|ש"ח|ש״ח|שקל)')

#: The parcel as stated in the ruling itself.
#:
#: Order matters and is deliberate: the FIRST pattern is the operative-section
#: form ("המקרקעין המזוהים כגוש: 40236, חלקה: 8"), which is the ruling's own
#: authoritative statement of what it decided. Validation against the CSV
#: showed that searching the document body loosely picks up OTHER parcels the
#: appraiser merely cites - comparables, neighbouring lots, prior rulings - and
#: produced 4 false mismatches out of 27 (e.g. reading ג40070 ח1 when both the
#: CSV and the document's own decision said גוש 40236 חלקה 8).
#:
#: Every one of those 4 disagreements turned out to be THIS extractor's fault,
#: not the CSV's - confirmed three independent ways: the row header text, the
#: national registry, and the document's decision paragraph. So the CSV is the
#: reference and these patterns exist to corroborate it, not to override it.
#: `\d[\d ]{2,6}` not `\d{3,6}`: the PDF text layer splits digit runs, so
#: "גוש 6217" extracts as "בגוש621 7". Matching only unbroken digits read that
#: as gush 621 - a real mismatch against the CSV's 6217. Internal spaces are
#: stripped by `_digits()` after the match.
_DOC_PARCEL = [
    # colon form, used in the operative "החלטה"/"הכרעה" paragraph
    re.compile(r'גוש\s*:\s*(\d[\d ]{2,7})\s*,?\s*חלק(?:ה|ות)\s*:?\s*(\d[\d ]{0,5})'),
    re.compile(r'גוש\s*(\d[\d ]{2,7})\s*,?\s*חלק(?:ה|ות)\s*(\d[\d ]{0,5})'),
    re.compile(r'חלק(?:ה|ות)\s*(\d[\d ]{0,5})\s*,?\s*ב?גוש\s*:?\s*(\d[\d ]{2,7})'),
    re.compile(r'ג[\'"״]\s*(\d[\d ]{2,7})\s*ח[\'"״]\s*(\d[\d ]{0,5})'),
    re.compile(r'גו["״]ח\s*(\d[\d ]{2,7})\s*/\s*(\d[\d ]{0,5})'),
]


def _digits(value):
    """Strip the spaces the PDF text layer injects inside a number."""
    out = "".join(ch for ch in str(value or "") if ch.isdigit())
    return out or None
#: patterns whose groups are (helka, gush) rather than (gush, helka)
_PARCEL_REVERSED = {2}

#: A ruling can genuinely cover a parcel RANGE - "חלקות 481-482 בגוש 6213" is a
#: single unified registered plot. When the document states a range, all members
#: are recorded so a CSV value of 482 matches even though the range starts 481.
_DOC_PARCEL_RANGE = re.compile(
    r'חלק(?:ה|ות)\s*(\d{1,5})\s*[-–]\s*(\d{1,5})\s*,?\s*ב?גוש\s*:?\s*(\d{3,6})')

_PLAN_NUM = re.compile(r'\b(?:תא|תמא|תמ["״]א)\s*/?\s*\d{1,5}(?:/[֐-ת\d]+)*'
                       r'|\b\d{3}-\d{6,8}\b')
_EFFECTIVE = re.compile(
    r'מועד\s*(?:ה)?קובע[^\d]{0,40}(\d{1,2})[./-](\d{1,2})[./-](\d{4})')
_TAMA38 = re.compile(r'תמ["״]?א\s*/?\s*38|תמא\s*38')
_APPRAISER = re.compile(r'([֐-ת\'" ]{4,40}?)\s*[-–]\s*שמאי[ת]?\s*מקרקעין')


def _clean(text):
    if text is None:
        return None
    out = " ".join(str(text).replace("\xa0", " ").split())
    return out or None


def _money_to_float(text):
    try:
        return float(str(text).replace(",", ""))
    except (TypeError, ValueError):
        return None


def extract(pdf_bytes: bytes) -> dict:
    """
    Parse one ruling PDF. Never raises on content; returns a status instead.

    The amount is only reported when exactly ONE distinct candidate is found in
    the operative section - see the module docstring for why.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(_PYPDF_HINT) from exc

    out = {
        "pages": None, "chars": 0, "has_text": 0, "amount_nis": None,
        "amount_status": "no_text", "candidates": [], "doc_gush": None,
        "doc_helka": None, "doc_helka_range": [], "doc_appraiser": None,
        "doc_plans": [], "effective_date": None, "is_tama38": 0,
        "decision_text": None, "error": None,
        "parcel_scope": None, "parcel_confident": None,
    }
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as exc:
        out["error"] = f"pdf parse: {type(exc).__name__}: {exc}"[:300]
        out["amount_status"] = "no_text"
        return out

    out["pages"] = len(pages)
    text = "\n".join(pages)
    out["chars"] = len(text)
    if len(text.strip()) < 300:
        # Genuinely image-only scan. Marked, not guessed at.
        return out
    out["has_text"] = 1

    # --- the operative section (needed early: it is the best parcel source) --
    sections = list(_DECISION.finditer(text))
    section = text[sections[-1].start():sections[-1].start() + 1600] if sections else ""

    # --- facts that cross-check the CSV -----------------------------------
    # Search order = confidence order: the ruling's own decision paragraph,
    # then the letterhead, then the body. The body is last because it cites
    # comparable and neighbouring parcels that are NOT the subject of the
    # ruling - that is what caused the false mismatches described above.
    head = "\n".join(pages[:2])
    # Scope order is confidence order, and `scope_used` records which one
    # answered. The document BODY is searched last and its result is marked
    # low-confidence: an appraiser cites comparable and neighbouring parcels
    # throughout the reasoning, and those are not the subject of the ruling.
    # Measured: 9 of 149 documents disagreed with the CSV, and in every case
    # inspected the CSV was right and the body match was a comparable.
    for scope_name, scope in (("decision", section), ("letterhead", head),
                              ("body", text[:9000])):
        if not scope:
            continue
        for index, pattern in enumerate(_DOC_PARCEL):
            match = pattern.search(scope)
            if not match:
                continue
            first = _digits(match.group(1))
            second = _digits(match.group(2))
            if not first:
                continue
            gush, helka = ((second, first) if index in _PARCEL_REVERSED
                           else (first, second))
            out["doc_gush"], out["doc_helka"] = gush, helka
            out["parcel_scope"] = scope_name
            out["parcel_confident"] = 1 if scope_name != "body" else 0
            break
        if out["doc_gush"]:
            break

    # A stated parcel range means several parcels are legitimately in scope.
    out["doc_helka_range"] = []
    for scope in (section, head):
        span = _DOC_PARCEL_RANGE.search(scope or "")
        if span:
            low, high, gush = int(span.group(1)), int(span.group(2)), span.group(3)
            if 0 < high - low <= 20:
                out["doc_helka_range"] = [str(n) for n in range(low, high + 1)]
                out["doc_gush"] = out["doc_gush"] or gush
            break

    appraiser = _APPRAISER.search(head)
    if appraiser:
        out["doc_appraiser"] = _clean(appraiser.group(1))
    out["doc_plans"] = sorted({_clean(m) for m in _PLAN_NUM.findall(text[:40000])} - {None})[:12]
    effective = _EFFECTIVE.search(text)
    if effective:
        day, month, year = effective.groups()
        if 1990 <= int(year) <= 2050 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
            out["effective_date"] = f"{year}-{int(month):02d}-{int(day):02d}"
    out["is_tama38"] = 1 if _TAMA38.search(text) else 0

    # --- the amount, from the operative section ---------------------------
    if not sections:
        out["amount_status"] = "no_section"
        return out
    out["decision_text"] = _clean(section[:900])

    found = _MONEY.findall(section)
    values = []
    for raw in found:
        value = _money_to_float(raw)
        # A levy below 1,000 ₪ is not a ruling outcome; above 500M is a
        # cumulative table figure, not this parcel's levy.
        if value is not None and 1_000 <= value <= 500_000_000:
            values.append(value)
    out["candidates"] = sorted(set(values))

    if not out["candidates"]:
        out["amount_status"] = "not_found"
    elif len(out["candidates"]) == 1:
        out["amount_nis"] = out["candidates"][0]
        out["amount_status"] = "ok"
    else:
        out["amount_status"] = "ambiguous"
    return out


# ---------------------------------------------------------------- fetch loop

def relink(conn) -> dict:
    """
    Repair `appraisal_id` after an appraisals rebuild, matching on `link`.

    Safe and idempotent - run it after any `import_appraisals`. Without it the
    documents table points at ids that no longer exist and every PDF gets
    downloaded again.
    """
    ensure_schema(conn)
    fixed = conn.execute(
        """UPDATE appraisal_docs SET appraisal_id = (
               SELECT a.id FROM appraisals a WHERE a.link = appraisal_docs.link)
           WHERE link IS NOT NULL
             AND EXISTS (SELECT 1 FROM appraisals a
                         WHERE a.link = appraisal_docs.link
                           AND a.id <> appraisal_docs.appraisal_id)""").rowcount
    conn.commit()
    orphans = conn.execute(
        """SELECT COUNT(*) FROM appraisal_docs d WHERE NOT EXISTS
           (SELECT 1 FROM appraisals a WHERE a.id = d.appraisal_id)"""
    ).fetchone()[0]
    return {"relinked": fixed, "still_orphaned": orphans}


def fetch_one(conn, appraisal_id: int, *, timeout=(20, 150),
              force=False) -> dict:
    """Fetch and parse one ruling. Idempotent; skips if already stored."""
    import db as _db

    ensure_schema(conn)
    row = conn.execute("SELECT id, link FROM appraisals WHERE id=?",
                       (appraisal_id,)).fetchone()
    if row is None:
        raise ValueError(f"no appraisal {appraisal_id}")

    if not force:
        # Checked by LINK as well as id: after a rebuild the id has changed but
        # the PDF is already downloaded and parsed. Checking id alone re-fetched
        # every document.
        existing = conn.execute(
            "SELECT * FROM appraisal_docs WHERE link = ? OR appraisal_id = ?",
            (row["link"], appraisal_id)).fetchone()
        if existing is not None:
            if existing["appraisal_id"] != appraisal_id:
                conn.execute(
                    "UPDATE appraisal_docs SET appraisal_id=? WHERE link=?",
                    (appraisal_id, row["link"]))
                conn.commit()
            return dict(existing)

    full = _download_and_parse(appraisal_id, row["link"], timeout=timeout)
    _store(conn, appraisal_id, full)
    return full


_BLANK = {"pages": None, "chars": 0, "has_text": 0, "amount_nis": None,
          "candidates": [], "doc_gush": None, "doc_helka": None,
          "doc_helka_range": [], "doc_appraiser": None, "doc_plans": [],
          "effective_date": None, "is_tama38": 0, "decision_text": None,
          "error": None, "parcel_scope": None, "parcel_confident": None}


def _download(appraisal_id: int, link: str, *, timeout=(20, 150)) -> dict:
    """Fetch one PDF's bytes. Network only - safe on a worker THREAD."""
    import db as _db

    record = {"appraisal_id": appraisal_id, "link": link, "http_status": None,
              "content_type": None, "bytes": 0, "fetched_at": _db.now_iso(),
              "content": None, "fetch_error": None}
    try:
        resp = _sess().get(link, timeout=timeout)
        record["http_status"] = resp.status_code
        record["content_type"] = (resp.headers.get("content-type") or "")[:80]
        record["bytes"] = len(resp.content)
        resp.raise_for_status()
        if resp.content[:5] != b"%PDF-":
            record["fetch_error"] = (
                f"not a PDF (content-type {record['content_type']})")
        else:
            record["content"] = resp.content
    except Exception as exc:
        record["fetch_error"] = f"{type(exc).__name__}: {exc}"[:300]
    return record


def parse_pdf_bytes(content: bytes) -> dict:
    """Parse PDF bytes to a document dict.

    Module-level and self-contained so it can be dispatched to a PROCESS pool:
    parsing is the expensive half (~9.7s vs ~3.9s to download) and it is pure
    CPU, which threads cannot parallelise in CPython.
    """
    try:
        return extract(content)
    except Exception as exc:
        return {"amount_status": "fetch_error",
                "error": f"{type(exc).__name__}: {exc}"[:300]}


def _assemble(record: dict, parsed: dict) -> dict:
    """Merge a download record and its parse result into a storable row."""
    record = {k: v for k, v in record.items()
              if k not in ("content", "fetch_error")}
    return {**record, **_BLANK, **parsed}


def _download_and_parse(appraisal_id: int, link: str, *,
                        timeout=(20, 150)) -> dict:
    """Fetch and parse one ruling in this thread (the single-document path)."""
    record = _download(appraisal_id, link, timeout=timeout)
    if record["content"] is None:
        parsed = {"amount_status": "fetch_error", "error": record["fetch_error"]}
    else:
        parsed = parse_pdf_bytes(record["content"])
    return _assemble(record, parsed)


def _store(conn, appraisal_id: int, full: dict) -> dict:
    """Write one parsed document. Called only from the connection's own thread."""
    conn.execute(
        """INSERT INTO appraisal_docs
             (appraisal_id, link, http_status, content_type, bytes, pages,
              chars, has_text, amount_nis, amount_status, candidates_json,
              doc_gush, doc_helka, doc_helka_range, doc_appraiser,
              doc_plans_json, effective_date, is_tama38, decision_text,
              error, fetched_at, parcel_scope, parcel_confident)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           -- `link` is UNIQUE too, so target that: the same ruling re-fetched
           -- under a new appraisal_id must update the existing row rather than
           -- fail the index or create a duplicate.
           ON CONFLICT (link) DO UPDATE SET
             appraisal_id=excluded.appraisal_id,
             http_status=excluded.http_status, content_type=excluded.content_type,
             bytes=excluded.bytes, pages=excluded.pages, chars=excluded.chars,
             has_text=excluded.has_text, amount_nis=excluded.amount_nis,
             amount_status=excluded.amount_status,
             candidates_json=excluded.candidates_json,
             doc_gush=excluded.doc_gush, doc_helka=excluded.doc_helka,
             doc_helka_range=excluded.doc_helka_range,
             doc_appraiser=excluded.doc_appraiser,
             doc_plans_json=excluded.doc_plans_json,
             effective_date=excluded.effective_date,
             is_tama38=excluded.is_tama38, decision_text=excluded.decision_text,
             error=excluded.error, fetched_at=excluded.fetched_at,
             parcel_scope=excluded.parcel_scope,
             parcel_confident=excluded.parcel_confident""",
        (appraisal_id, full["link"], full["http_status"], full["content_type"],
         full["bytes"], full["pages"], full["chars"], full["has_text"],
         full["amount_nis"], full["amount_status"],
         json.dumps(full["candidates"]), full["doc_gush"], full["doc_helka"],
         json.dumps(full["doc_helka_range"]), full["doc_appraiser"],
         json.dumps(full["doc_plans"], ensure_ascii=False),
         full["effective_date"], full["is_tama38"], full["decision_text"],
         full["error"], full["fetched_at"], full["parcel_scope"],
         full["parcel_confident"]))
    conn.commit()
    return full


#: Concurrent workers for the fetch+parse backlog.
#:
#: Measured on one real 34-page ruling: 3.9s to download, 9.7s to parse. The
#: work is therefore CPU-bound in pypdf, not network-bound, which is why a
#: thread pool only reached 2x - CPython cannot run bytecode in parallel.
#: `fetch_batch` uses threads for the HTTP wait and a PROCESS pool for parsing,
#: so both halves actually overlap.
#:
#: Kept modest on purpose: this is a public government service, and the goal is
#: to clear a backlog over days without hammering it.
#:
#: Parsing cannot outrun the core count, so more workers than cores only adds
#: memory and contention. Measured end-to-end: 22.5s/doc serial -> 9.0s/doc on
#: a 4-core machine (2.5x), which turns a ~200-night backlog into ~80.
DEFAULT_WORKERS = max(2, min(6, os.cpu_count() or 4))


def fetch_batch(conn, *, limit=100, where="", progress=None, should_stop=None,
                timeout=(20, 150), redo_errors=False, workers=None,
                use_inline_parsing=False) -> dict:
    """
    Fetch documents for appraisals that have none yet.

    Resumable by construction: it selects only rows absent from
    `appraisal_docs`, so stopping and re-running continues where it left off.
    Each document is committed on its own - a crash never loses prior work.

    Downloads run on a small thread pool because the cost is network latency,
    not CPU. Only `_download_and_parse` is threaded; every database write stays
    on this thread, since a SQLite connection must not be shared across threads.
    """
    from concurrent.futures import (ProcessPoolExecutor, ThreadPoolExecutor,
                                    as_completed)

    ensure_schema(conn)
    relink(conn)          # repair ids before deciding what still needs fetching
    # Matches on LINK as well as id. Matching on id alone meant that after an
    # appraisals rebuild (which shifts every id) the queue contained thousands
    # of PDFs that were already downloaded and parsed.
    skip = "" if redo_errors else \
        " AND NOT EXISTS (SELECT 1 FROM appraisal_docs d" \
        " WHERE d.appraisal_id = a.id OR d.link = a.link)"
    if redo_errors:
        skip = (" AND (NOT EXISTS (SELECT 1 FROM appraisal_docs d"
                " WHERE d.appraisal_id = a.id OR d.link = a.link)"
                " OR EXISTS (SELECT 1 FROM appraisal_docs d"
                " WHERE (d.appraisal_id = a.id OR d.link = a.link)"
                " AND d.amount_status = 'fetch_error'))")
    extra = f" AND {where}" if where else ""
    # `link` is selected here so a worker never needs the connection.
    queue = [(r[0], r[1]) for r in conn.execute(
        f"""SELECT a.id, a.link FROM appraisals a
            WHERE a.link IS NOT NULL{extra}{skip}
            ORDER BY a.decision_date DESC LIMIT ?""", (limit,))]

    stats = {"attempted": 0, "ok": 0, "ambiguous": 0, "not_found": 0,
             "no_section": 0, "no_text": 0, "fetch_error": 0}
    if not queue:
        return stats

    count = max(1, int(workers or DEFAULT_WORKERS))
    # Downloads on threads (they only wait), parses in processes (they only
    # compute). A process pool is skipped for tiny batches, where spawning
    # interpreters costs more than it saves. `use_inline_parsing` forces the
    # single-process path, which callers need when the parser is patched or
    # when spawning is unavailable.
    use_processes = (len(queue) > 2 and count > 1 and not use_inline_parsing)
    parse_pool = None
    stats["workers"] = count
    try:
        if use_processes:
            try:
                parse_pool = ProcessPoolExecutor(max_workers=count)
            except (OSError, ValueError):
                # Restricted environments may forbid spawning; parse inline.
                parse_pool = None
        def _record_result(record, parsed):
            _store(conn, record["appraisal_id"], _assemble(record, parsed))
            stats["attempted"] += 1
            status = parsed.get("amount_status", "fetch_error")
            stats[status] = stats.get(status, 0) + 1
            if progress:
                progress("documents", stats["attempted"])

        with ThreadPoolExecutor(max_workers=count) as net:
            downloads = {net.submit(_download, aid, link, timeout=timeout): aid
                         for aid, link in queue}
            #: Parses are submitted as downloads land and collected separately,
            #: so several PDFs are being parsed while others are still in
            #: flight. Awaiting each parse at submission time would have
            #: serialised the expensive half and undone the process pool.
            parses: dict = {}
            cancelled = False
            try:
                for future in as_completed(downloads):
                    if should_stop and should_stop():
                        for pending in downloads:
                            pending.cancel()
                        stats["cancelled"] = cancelled = True
                        break
                    record = future.result()
                    if record["content"] is None:
                        _record_result(record, {
                            "amount_status": "fetch_error",
                            "error": record["fetch_error"]})
                    elif parse_pool is not None:
                        parses[parse_pool.submit(
                            parse_pdf_bytes, record["content"])] = record
                    else:
                        _record_result(record, parse_pdf_bytes(record["content"]))
            finally:
                # Do not let a cancel wait on the whole remaining queue.
                net.shutdown(wait=False, cancel_futures=True)

            # Drain the parses that are still running. `should_stop` is checked
            # here too: with a process pool the downloads finish quickly and
            # queue up behind the slower parses, so testing it only in the
            # download loop left a cancel unable to stop most of the work.
            for future in as_completed(list(parses)):
                if cancelled or (should_stop and should_stop()):
                    if not cancelled:
                        stats["cancelled"] = cancelled = True
                    future.cancel()
                    continue
                record = parses[future]
                try:
                    _record_result(record, future.result())
                except Exception as exc:
                    _record_result(record, {
                        "amount_status": "fetch_error",
                        "error": f"{type(exc).__name__}: {exc}"[:300]})
    finally:
        if parse_pool is not None:
            parse_pool.shutdown(wait=False, cancel_futures=True)
    return stats


# --------------------------------------------------------- cross-validation

def verify_against_csv(conn, limit=None) -> dict:
    """
    Cross-check the CSV metadata against the ruling documents themselves.

    This is the point of reading the PDFs: the document heading is an
    INDEPENDENT source for the parcel identity that every join in PlanWatch
    relies on. Disagreement is reported, never silently reconciled.

    `swapped` counts rows where the document's gush/helka match the CSV's but
    TRANSPOSED - a defect already confirmed in the CSV itself, where one pair of
    rows carries ('12','4878') and ('4878','12') for the same ruling.
    """
    rows = conn.execute(
        f"""SELECT d.appraisal_id, d.doc_gush, d.doc_helka, d.doc_helka_range,
                   d.parcel_scope, d.parcel_confident, d.effective_date,
                   d.is_tama38, d.amount_status, d.amount_nis,
                   a.block_raw, a.plot_raw, a.decision_date, a.committee,
                   a.appraisal_type
            FROM appraisal_docs d JOIN appraisals a ON a.id = d.appraisal_id
            WHERE d.has_text = 1
            {'LIMIT ' + str(int(limit)) if limit else ''}""").fetchall()

    out = {"checked": 0, "parcel_checked": 0, "parcel_match": 0,
           "parcel_mismatch": 0, "swapped": 0, "doc_parcel_missing": 0,
           "amount_ok": 0, "amount_ambiguous": 0, "amount_absent": 0,
           "mismatches": []}

    def digits(value):
        found = re.findall(r"\d+", str(value or ""))
        return {str(int(x)) for x in found if x.isdigit()}

    for row in rows:
        out["checked"] += 1
        status = row["amount_status"]
        if status == "ok":
            out["amount_ok"] += 1
        elif status == "ambiguous":
            out["amount_ambiguous"] += 1
        else:
            out["amount_absent"] += 1

        if not row["doc_gush"]:
            out["doc_parcel_missing"] += 1
            continue
        out["parcel_checked"] += 1   # decremented below if low-confidence
        csv_gush, csv_helka = digits(row["block_raw"]), digits(row["plot_raw"])
        doc_gush = str(int(row["doc_gush"])) if row["doc_gush"].isdigit() else row["doc_gush"]
        doc_helka = (str(int(row["doc_helka"]))
                     if row["doc_helka"] and row["doc_helka"].isdigit() else row["doc_helka"])

        # A stated range covers every parcel in it, so a CSV helka anywhere
        # inside it is a match - "חלקות 481-482" legitimately includes 482.
        try:
            doc_range = set(json.loads(row["doc_helka_range"] or "[]"))
        except (TypeError, ValueError, IndexError):
            doc_range = set()
        helka_ok = (doc_helka in csv_helka or not csv_helka
                    or bool(doc_range & csv_helka))
        # A body-sourced parcel is a low-confidence guess (comparables are
        # cited throughout an appraisal), so it is reported separately rather
        # than counted as a disagreement with the CSV.
        try:
            confident = row["parcel_confident"]
        except (IndexError, KeyError):
            confident = None
        if confident == 0:
            out["low_confidence"] = out.get("low_confidence", 0) + 1
            continue
        if doc_gush in csv_gush and helka_ok:
            out["parcel_match"] += 1
        elif doc_gush in csv_helka and doc_helka in csv_gush:
            out["swapped"] += 1
            out["mismatches"].append({
                "appraisal_id": row["appraisal_id"], "kind": "swapped",
                "csv": f"ג{row['block_raw']} ח{row['plot_raw']}",
                "doc": f"ג{doc_gush} ח{doc_helka}",
                "committee": row["committee"], "date": row["decision_date"]})
        else:
            out["parcel_mismatch"] += 1
            if len(out["mismatches"]) < 40:
                out["mismatches"].append({
                    "appraisal_id": row["appraisal_id"], "kind": "mismatch",
                    "csv": f"ג{row['block_raw']} ח{row['plot_raw']}",
                    "doc": f"ג{doc_gush} ח{doc_helka}",
                    "committee": row["committee"], "date": row["decision_date"]})

    # Exclude low-confidence rows from the denominator too.
    out["parcel_checked"] -= out.get("low_confidence", 0)
    if out["parcel_checked"] > 0:
        out["parcel_match_pct"] = round(
            out["parcel_match"] / out["parcel_checked"] * 100, 1)
    out.setdefault("low_confidence", 0)
    out["low_confidence_note"] = (
        "שורות שבהן הגוש/חלקה נמצא בגוף המסמך ולא בסעיף ההחלטה — "
        "השמאי מצטט חלקות השוואה, ולכן אלה לא נחשבות כאי-התאמה.")
    return out


def counts(conn) -> dict:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appraisal_docs'"
    ).fetchone()
    if not exists:
        return {"appraisal_docs": 0}
    head = conn.execute(
        """SELECT COUNT(*), SUM(has_text), SUM(amount_nis IS NOT NULL),
                  SUM(pages), AVG(pages)
           FROM appraisal_docs""").fetchone()
    by_status = {r[0]: r[1] for r in conn.execute(
        "SELECT amount_status, COUNT(*) FROM appraisal_docs GROUP BY 1")}
    return {"appraisal_docs": head[0], "with_text": head[1] or 0,
            "with_amount": head[2] or 0, "total_pages": head[3] or 0,
            "avg_pages": round(head[4] or 0, 1), "by_status": by_status}


def doc_for_appraisal(conn, appraisal_id) -> dict | None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appraisal_docs'"
    ).fetchone()
    if not exists:
        return None
    row = conn.execute("SELECT * FROM appraisal_docs WHERE appraisal_id=?",
                       (appraisal_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["candidates"] = json.loads(out.pop("candidates_json") or "[]")
    out["doc_plans"] = json.loads(out.pop("doc_plans_json") or "[]")
    # .get(), not [...]: a row written before doc_helka_range existed has no
    # such key, and a KeyError here would break the whole parcel dossier.
    out["doc_helka_range"] = json.loads(out.get("doc_helka_range") or "[]")
    return out


def amount_stats(conn) -> dict:
    """
    Distribution of the amounts that WERE extracted unambiguously.

    Reports the coverage rate loudly: a median built from 14% of documents is
    a median of that 14%, not of the register.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='appraisal_docs'"
    ).fetchone()
    if not exists:
        return {"n": 0}
    values = [r[0] for r in conn.execute(
        """SELECT amount_nis FROM appraisal_docs
           WHERE amount_nis IS NOT NULL ORDER BY amount_nis""")]
    total = conn.execute("SELECT COUNT(*) FROM appraisal_docs").fetchone()[0]
    if not values:
        return {"n": 0, "documents": total,
                "note": "לא חולץ אף סכום חד-משמעי."}

    def pct(fraction):
        return values[min(len(values) - 1, int(len(values) * fraction))]

    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return {
        "n": len(values), "documents": total,
        "coverage_pct": round(len(values) / total * 100, 1) if total else None,
        "min": values[0], "p25": pct(0.25), "median": median,
        "p75": pct(0.75), "max": values[-1],
        "note": f"מבוסס על {len(values)} מסמכים מתוך {total} שנקראו - "
                f"רק אלה שבהם נמצא סכום אחד חד-משמעי. אין להסיק מכך על המאגר "
                f"כולו.",
    }


if __name__ == "__main__":
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # ---- parser unit tests, on synthetic text (no network) ----------------
    def _sec(body):
        return "כותרת\n" + body

    assert _MONEY.findall('היטל השבחה 109,551 ₪ ו-54,776 ₪') == \
        ['109,551', '54,776']
    # a bare number with no shekel marker is not money
    assert _MONEY.findall('שטח 1,116.08 מ"ר') == []
    # mid-sentence "החלטה" must NOT open a section (observed on ruling 24156)
    assert not _DECISION.search('בהתאם להחלטה המשפטית בבית המשפט העליון')
    assert _DECISION.search('\nהחלטה\nלאור האמור')
    assert _DECISION.search('\n7. החלטה:\nהנני מעריך')
    # parcel patterns, in the forms the documents actually use
    for text, want in (
        ('שומה מכרעת להיטל השבחה - גוש 6667 חלקה 721', ('6667', '721')),
        ('ג\' 6894 ח\' 17', ('6894', '17')),
        ('גו"ח 5027/113', ('5027', '113')),
    ):
        got = None
        for idx, pat in enumerate(_DOC_PARCEL):
            m = pat.search(text)
            if m:
                a, b = m.group(1), m.group(2)
                got = (b, a) if idx in _PARCEL_REVERSED else (a, b)
                break
        assert got == want, f"{text!r} -> {got}, want {want}"
    # The operative-section colon form must be recognised. Reading the body
    # loosely instead produced 4 false mismatches out of 27 in validation.
    _op = ("החלטה\nהגעתי לכלל דעה כי ההשבחה במקרקעין המזוהים "
           "כגוש: 40236, חלקה: 8 (מגרש 8)")
    assert _DOC_PARCEL[0].search(_op).groups() == ("40236", "8")
    # A stated parcel range must expand: "חלקות 481-482" includes 482.
    _rng = "הכרעה: הנכס מהווה את חלקות 481-482 בגוש 6213"
    _m = _DOC_PARCEL_RANGE.search(_rng)
    assert _m and _m.groups() == ("481", "482", "6213"), _m
    print("parser self-tests passed\n")

    conn = db.get_conn()
    stats = fetch_batch(conn, limit=int(sys.argv[1]) if len(sys.argv) > 1 else 25,
                        progress=lambda k, n: print(f"  {n} docs", end="\r"))
    print(f"\nfetch: {json.dumps(stats, ensure_ascii=False)}")
    print(f"counts: {json.dumps(counts(conn), ensure_ascii=False)}")
    print(f"\namounts: {json.dumps(amount_stats(conn), ensure_ascii=False)}")
    print("\n=== CROSS-VALIDATION vs the CSV ===")
    check = verify_against_csv(conn)
    for key in ("checked", "parcel_checked", "parcel_match", "parcel_match_pct",
                "parcel_mismatch", "swapped", "doc_parcel_missing",
                "amount_ok", "amount_ambiguous", "amount_absent"):
        print(f"  {key:22} {check.get(key)}")
    for m in check["mismatches"][:6]:
        print(f"    {m['kind']:9} id={m['appraisal_id']} CSV[{m['csv']}] "
              f"DOC[{m['doc']}] {m['committee']} {m['date']}")
    conn.close()
