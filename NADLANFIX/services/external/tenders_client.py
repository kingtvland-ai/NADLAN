"""
PlanWatch - RAMI land tenders (מכרזי רשות מקרקעי ישראל)
========================================================
**Real properties for sale, with real prices.** This is the answer to "where
are the listings": the State of Israel is the largest seller of land in the
country, and it publishes every tender openly.

Source: `apps.land.gov.il/MichrazimSite`. Endpoint names taken from the site's
own JS bundle (`main-HNW7SAE2.js`), not guessed:

    POST api/SearchApi/Search            -> all tenders (10,600 rows, 4.4 MB)
    GET  api/MichrazDetailsApi/Get?michrazID=<id>  -> one tender, ~42 KB
    GET  api/YeshuvimApi/Get             -> 1,421 localities

Verified live 2026-07-28: 10,600 tenders, 6,289 with housing units, published
2000-01-08 through **today**, 481 currently open for bids.

Why this is better than a listings feed
---------------------------------------
Each tender's `Tik` array is one row per lot, and each lot carries:

* **גוש + חלקה** - joins straight to `tabu_assets` and to plan geometry
* **`Shetach`** - lot area in m²
* **`Tochnit`** - the governing plan number, joins to `plans`
* **`mechirShuma`** - the STATE APPRAISER's valuation, in shekels
* **`MechirSaf`** - reserve (minimum) price
* **`SchumZchiya` + `ShemZoche`** - the winning bid and who won it
* `HotzaotPituach` - development costs payable on top

`mechirShuma` is an official appraisal and `SchumZchiya` is a *concluded
transaction price*. On 5,235 closed tenders that is genuine comparable-sale
evidence - the thing nadlan.gov.il gates behind a reCAPTCHA. It is land rather
than second-hand apartments, so it is not a substitute for apartment comps, but
it is real, dated, priced, and parcel-identified.

Field traps, all verified against live responses
------------------------------------------------
* `Gush` here can be an 8-DIGIT internal key ("10022201") rather than a
  cadastral gush. Real gushim are 3-6 digits. `_split_gush()` keeps the raw
  value and only exposes `gush` for joining when it is plausible - otherwise a
  join against `tabu_assets` would silently match nothing while looking fine.
* `SchumZchiya` is `0.0` (not NULL) when a lot drew no bids, and `ShemZoche`
  then reads " " or "אין הצעות למתחם זה". Averaging raw `SchumZchiya` would
  drag every statistic toward zero.
* `MechirSaf` is often NULL while `mechirShuma` is populated, and vice versa.
  Neither is a reliable stand-in for the other.
* `Shchuna` and `MigrashName` are space-padded.
* Dates are ISO with an offset (`2026-07-28T00:00:00+03:00`); only the date part
  is kept.
"""

from __future__ import annotations

import json
from legacy.tools.http_client import build_session

BASE = "https://apps.land.gov.il/MichrazimSite/api"
SEARCH_URL = f"{BASE}/SearchApi/Search"
DETAIL_URL = f"{BASE}/MichrazDetailsApi/Get"
YESHUV_URL = f"{BASE}/YeshuvimApi/Get"

#: `StatusMichraz` -> label. Derived from the observed distribution
#: (5=8,928  3=623  7=546  2=276  1=205  4=22) cross-read against the site UI.
STATUS = {
    1: "נפתח להצעות",
    2: "פתוח להצעות",
    3: "נסגר - בבדיקה",
    4: "בוטל",
    5: "הוכרז זוכה / הסתיים",
    7: "ארכיון",
}
#: Statuses where a bidder can still act.
OPEN_STATUSES = (1, 2)

TABLES = """
CREATE TABLE IF NOT EXISTS tenders (
    michraz_id     INTEGER PRIMARY KEY,
    name           TEXT,
    status_code    INTEGER,
    status         TEXT,
    locality_code  TEXT,          -- KodYeshuv, joins od_localities.code
    locality       TEXT,          -- resolved from YeshuvimApi
    neighborhood   TEXT,
    region_code    INTEGER,
    purpose_code   INTEGER,       -- KodYeudMichraz (1 = מגורים)
    kind_code      INTEGER,
    housing_units  INTEGER,
    published_at   TEXT,
    opens_at       TEXT,
    closes_at      TEXT,
    committee_at   TEXT,
    online_bids    INTEGER,
    detail_fetched INTEGER NOT NULL DEFAULT 0,
    fetched_at     TEXT
);

/* One row per LOT (Tik). This is the property-level table. */
CREATE TABLE IF NOT EXISTS tender_lots (
    michraz_id     INTEGER NOT NULL,
    tik_id         TEXT NOT NULL,
    lot_name       TEXT,
    gush_raw       TEXT,          -- exactly as published
    gush           TEXT,          -- only when plausibly cadastral (3-6 digits)
    helka          TEXT,
    plan_number    TEXT,          -- Tochnit, joins plans.pl_number_sq
    plot_name      TEXT,
    area_m2        REAL,
    built_area_m2  REAL,
    capacity       INTEGER,       -- Kibolet: units/lots on this parcel
    dev_costs      REAL,
    reserve_price  REAL,          -- MechirSaf, NULL when not published
    appraised_price REAL,         -- mechirShuma - the state appraiser's value
    winning_price  REAL,          -- NULL when there was no winner (source: 0.0)
    winner         TEXT,          -- NULL when blank / "אין הצעות"
    deposit        REAL,
    price_per_m2   REAL,          -- derived: best available price / area
    price_basis    TEXT,          -- which field price_per_m2 came from
    PRIMARY KEY (michraz_id, tik_id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tenders_status   ON tenders (status_code);
CREATE INDEX IF NOT EXISTS idx_tenders_locality ON tenders (locality);
CREATE INDEX IF NOT EXISTS idx_tenders_pub      ON tenders (published_at);
CREATE INDEX IF NOT EXISTS idx_lots_gh          ON tender_lots (gush, helka);
CREATE INDEX IF NOT EXISTS idx_lots_plan        ON tender_lots (plan_number);
CREATE INDEX IF NOT EXISTS idx_lots_michraz     ON tender_lots (michraz_id);
"""

_session = None
_yeshuvim: dict | None = None


def _sess():
    global _session
    if _session is None:
        _session = build_session(total_retries=2, backoff_factor=0.8)
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
    return out or None


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    out = _num(value)
    return int(out) if out is not None else None


def _date(value):
    """'2026-07-28T00:00:00+03:00' -> '2026-07-28'."""
    text = _text(value)
    return text[:10] if text else None


def _split_gush(raw):
    """
    Return `(gush_raw, gush_for_joining)`.

    Some lots publish an 8-digit internal key ("10022201") where a cadastral
    gush belongs. Real Israeli gush numbers are 3-6 digits. Exposing the long
    key as `gush` would make every join to `tabu_assets` return nothing while
    the column looked populated, so it is kept only in `gush_raw`.
    """
    text = _text(raw)
    if not text:
        return None, None
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and 3 <= len(digits) <= 6:
        return text, str(int(digits))
    return text, None


def _no_bid(winner, price):
    """A lot with no bids publishes SchumZchiya=0.0 and a blank/notice winner."""
    label = _text(winner) or ""
    if not label or "אין הצעות" in label:
        return True
    return not price


def _price_per_m2(area, winning, appraised, reserve):
    """
    Best-available price per m², with the source recorded.

    Order of preference is deliberate: a concluded sale beats an appraisal,
    which beats a reserve. `price_basis` travels with the number so a caller
    can never mistake a reserve price for a sale.
    """
    if not area or area <= 0:
        return None, None
    for value, basis in ((winning, "winning_price"),
                         (appraised, "appraised_price"),
                         (reserve, "reserve_price")):
        if value and value > 0:
            return round(value / area, 1), basis
    return None, None


#: Exact field names from YeshuvimApi/Get, read off a live response - NOT
#: guessed. An earlier version tried `mtysvSemel` and every locality came back
#: None, which is the kind of failure that looks like "no data" rather than
#: "wrong key". Verified: 1,421 rows.
_YESHUV_CODE = "mtysvSemelYishuv"
_YESHUV_NAME = "mtysvShemYishuv"


def yeshuvim(refresh=False) -> dict:
    """locality code -> name, from the tender site's own lookup (1,421 rows)."""
    global _yeshuvim
    if _yeshuvim is not None and not refresh:
        return _yeshuvim
    rows = _sess().get(YESHUV_URL, timeout=(20, 120)).json()
    if rows and _YESHUV_CODE not in rows[0]:
        raise RuntimeError(
            f"YeshuvimApi changed shape: expected {_YESHUV_CODE!r}, "
            f"got {sorted(rows[0])[:6]}")
    out = {}
    for row in rows:
        code, name = row.get(_YESHUV_CODE), _text(row.get(_YESHUV_NAME))
        if code is not None and name:
            out[str(_int(code))] = name
    _yeshuvim = out
    return out


# ------------------------------------------------------------------- imports

def import_tenders(conn, progress=None, should_stop=None,
                   timeout=(25, 240)) -> int:
    """
    Import the tender index (one row per tender, no lots yet).

    Upsert on `michraz_id` and it deliberately does NOT touch
    `detail_fetched`, so re-running the index does not force a re-download of
    every detail page.
    """
    import db as _db

    ensure_schema(conn)
    # Deliberately NOT wrapped in a bare except: if the locality lookup breaks,
    # every tender silently gets locality=None and the whole table looks like a
    # source with no localities. Fail loudly instead.
    names = yeshuvim()

    rows = _sess().post(SEARCH_URL, json={}, timeout=timeout).json()
    now = _db.now_iso()
    batch = []
    for item in rows:
        if should_stop and should_stop():
            return 0
        code = _int(item.get("KodYeshuv"))
        status = _int(item.get("StatusMichraz"))
        batch.append((
            _int(item.get("MichrazID")), _text(item.get("MichrazName")),
            status, STATUS.get(status, f"קוד {status}"),
            str(code) if code is not None else None,
            names.get(str(code)), _text(item.get("Shchuna")),
            _int(item.get("KodMerchav")), _int(item.get("KodYeudMichraz")),
            _int(item.get("KodSugMichraz")), _int(item.get("YechidotDiur")),
            _date(item.get("PirsumDate")), _date(item.get("PtichaDate")),
            _date(item.get("SgiraDate")), _date(item.get("VaadaDate")),
            1 if item.get("Mekuvan") else 0, now,
        ))

    conn.executemany(
        """INSERT INTO tenders
             (michraz_id, name, status_code, status, locality_code, locality,
              neighborhood, region_code, purpose_code, kind_code,
              housing_units, published_at, opens_at, closes_at, committee_at,
              online_bids, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (michraz_id) DO UPDATE SET
             name=excluded.name, status_code=excluded.status_code,
             status=excluded.status, locality_code=excluded.locality_code,
             locality=excluded.locality, neighborhood=excluded.neighborhood,
             housing_units=excluded.housing_units,
             published_at=excluded.published_at, opens_at=excluded.opens_at,
             closes_at=excluded.closes_at, committee_at=excluded.committee_at,
             online_bids=excluded.online_bids, fetched_at=excluded.fetched_at""",
        batch)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
    if progress:
        progress("tenders", total)
    return total


def fetch_lots(conn, michraz_id: int, *, timeout=(20, 120)) -> int:
    """Fetch one tender's lots. Idempotent on (michraz_id, tik_id)."""
    ensure_schema(conn)
    detail = _sess().get(DETAIL_URL, params={"michrazID": int(michraz_id)},
                         timeout=timeout).json()
    rows = []
    for lot in (detail.get("Tik") or []):
        parcel = (lot.get("GushHelka") or [{}])[0]
        plan = (lot.get("TochnitMigrash") or [{}])[0]
        gush_raw, gush = _split_gush(parcel.get("Gush"))
        area = _num(lot.get("Shetach"))
        appraised = _num(lot.get("mechirShuma")) or None
        reserve = _num(lot.get("MechirSaf"))
        raw_win = _num(lot.get("SchumZchiya"))
        winner_name = _text(lot.get("ShemZoche"))
        if _no_bid(winner_name, raw_win):
            winning, winner = None, None
        else:
            winning, winner = raw_win, winner_name
        per_m2, basis = _price_per_m2(area, winning, appraised, reserve)
        rows.append((
            int(michraz_id), _text(lot.get("TikID")),
            _text(lot.get("MitchamName")), gush_raw, gush,
            _text(parcel.get("Helka")), _text(plan.get("Tochnit")),
            _text(plan.get("MigrashName")), area,
            _num(lot.get("ShetachBniya")), _int(lot.get("Kibolet")),
            _num(lot.get("HotzaotPituach")), reserve, appraised,
            winning, winner, _num(lot.get("SchumArvut")), per_m2, basis,
        ))

    if rows:
        conn.executemany(
            """INSERT INTO tender_lots
                 (michraz_id, tik_id, lot_name, gush_raw, gush, helka,
                  plan_number, plot_name, area_m2, built_area_m2, capacity,
                  dev_costs, reserve_price, appraised_price, winning_price,
                  winner, deposit, price_per_m2, price_basis)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (michraz_id, tik_id) DO UPDATE SET
                 lot_name=excluded.lot_name, gush_raw=excluded.gush_raw,
                 gush=excluded.gush, helka=excluded.helka,
                 plan_number=excluded.plan_number, plot_name=excluded.plot_name,
                 area_m2=excluded.area_m2, built_area_m2=excluded.built_area_m2,
                 capacity=excluded.capacity, dev_costs=excluded.dev_costs,
                 reserve_price=excluded.reserve_price,
                 appraised_price=excluded.appraised_price,
                 winning_price=excluded.winning_price, winner=excluded.winner,
                 deposit=excluded.deposit, price_per_m2=excluded.price_per_m2,
                 price_basis=excluded.price_basis""", rows)
    conn.execute("UPDATE tenders SET detail_fetched=1 WHERE michraz_id=?",
                 (int(michraz_id),))
    conn.commit()
    return len(rows)


def import_lots(conn, *, limit=300, open_first=True, progress=None,
                should_stop=None, timeout=(20, 120)) -> dict:
    """
    Fetch lots for tenders that have none yet.

    Resumable: selects only `detail_fetched=0`, commits per tender. Open tenders
    are done first because those are the ones a user can still act on.
    """
    ensure_schema(conn)
    order = ("CASE WHEN status_code IN (1,2) THEN 0 ELSE 1 END, "
             "published_at DESC") if open_first else "published_at DESC"
    ids = [r[0] for r in conn.execute(
        f"""SELECT michraz_id FROM tenders WHERE detail_fetched = 0
            ORDER BY {order} LIMIT ?""", (limit,))]
    stats = {"tenders": 0, "lots": 0, "errors": 0}
    for michraz_id in ids:
        if should_stop and should_stop():
            break
        try:
            stats["lots"] += fetch_lots(conn, michraz_id, timeout=timeout)
            stats["tenders"] += 1
        except Exception:
            stats["errors"] += 1
            # Mark it done so one poisoned tender cannot block the queue
            # forever; re-runnable with reset_failed().
            conn.execute("UPDATE tenders SET detail_fetched=2 WHERE michraz_id=?",
                         (michraz_id,))
            conn.commit()
        if progress:
            progress("tender_lots", stats["lots"])
    return stats


def reset_failed(conn) -> int:
    """Re-queue tenders whose detail fetch errored (detail_fetched=2)."""
    cur = conn.execute("UPDATE tenders SET detail_fetched=0 WHERE detail_fetched=2")
    conn.commit()
    return cur.rowcount


def import_all(conn, progress=None, should_stop=None, timeout=(25, 240)) -> int:
    total = import_tenders(conn, progress=progress, should_stop=should_stop,
                           timeout=timeout)
    import_lots(conn, limit=250, progress=progress, should_stop=should_stop)
    return total


IMPORTERS = {"tenders": import_tenders}


# ------------------------------------------------------------------- readers

def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def counts(conn) -> dict:
    if not _has(conn, "tenders"):
        return {"tenders": 0, "tender_lots": 0}
    head = conn.execute(
        """SELECT COUNT(*), SUM(status_code IN (1,2)), SUM(detail_fetched=1),
                  SUM(detail_fetched=2), COALESCE(SUM(housing_units),0)
           FROM tenders""").fetchone()
    return {"tenders": head[0], "open_now": head[1] or 0,
            "details_fetched": head[2] or 0, "detail_errors": head[3] or 0,
            "housing_units": head[4] or 0,
            "tender_lots": conn.execute(
                "SELECT COUNT(*) FROM tender_lots").fetchone()[0]
            if _has(conn, "tender_lots") else 0}


def search(conn, *, locality="", only_open=False, min_units=None,
           max_price=None, purpose=None, since=None, limit=100,
           offset=0) -> dict:
    """Tenders, newest first. `only_open=True` = still biddable."""
    if not _has(conn, "tenders"):
        return {"total": 0, "rows": [], "note": "מאגר המכרזים לא יובא."}
    clauses, params = ["1=1"], []
    if locality:
        clauses.append("(t.locality LIKE ? OR t.neighborhood LIKE ?)")
        params += [f"%{locality}%"] * 2
    if only_open:
        clauses.append("t.status_code IN (1,2)")
    if min_units is not None:
        clauses.append("t.housing_units >= ?")
        params.append(min_units)
    if purpose is not None:
        clauses.append("t.purpose_code = ?")
        params.append(purpose)
    if since:
        clauses.append("t.published_at >= ?")
        params.append(since)

    where = " AND ".join(clauses)
    total = conn.execute(
        f"SELECT COUNT(*) FROM tenders t WHERE {where}", params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"""SELECT t.*,
                   (SELECT COUNT(*) FROM tender_lots l
                     WHERE l.michraz_id = t.michraz_id) lots,
                   (SELECT MIN(l.price_per_m2) FROM tender_lots l
                     WHERE l.michraz_id = t.michraz_id
                       AND l.price_per_m2 IS NOT NULL) min_price_per_m2
            FROM tenders t WHERE {where}
            ORDER BY t.published_at DESC, t.michraz_id DESC
            LIMIT ? OFFSET ?""", params + [limit, offset])]
    if max_price is not None:
        rows = [r for r in rows if (r["min_price_per_m2"] or 0) <= max_price]
    return {"total": total, "rows": rows, "limit": limit, "offset": offset,
            "note": "מכרזי רמ\"י - קרקע ומגורים. mechirShuma הוא שומת השמאי "
                    "הממשלתי; SchumZchiya הוא מחיר הזכייה בפועל."}


def lots_for_tender(conn, michraz_id) -> list[dict]:
    if not _has(conn, "tender_lots"):
        return []
    return [dict(r) for r in conn.execute(
        """SELECT * FROM tender_lots WHERE michraz_id = ?
           ORDER BY area_m2 DESC""", (int(michraz_id),))]


def lots_for_parcel(conn, gush, helka=None, limit=50) -> list[dict]:
    """Tender lots on a parcel - i.e. "was this land ever sold by the state"."""
    if not _has(conn, "tender_lots"):
        return []
    clauses, params = ["l.gush = ?"], [str(gush).strip()]
    if helka:
        clauses.append("l.helka = ?")
        params.append(str(helka).strip())
    return [dict(r) for r in conn.execute(
        f"""SELECT l.*, t.name, t.status, t.locality, t.neighborhood,
                   t.published_at, t.closes_at
            FROM tender_lots l JOIN tenders t ON t.michraz_id = l.michraz_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.published_at DESC LIMIT ?""", params + [limit])]


def price_benchmarks(conn, locality="", min_lots=3) -> dict:
    """
    ₪/m² for LAND, per locality, from concluded tenders only.

    Uses `winning_price` exclusively - a real, dated, concluded transaction.
    Appraisals and reserve prices are excluded so this cannot be confused with
    an estimate. This is land, not apartments; do not compare the two.
    """
    if not _has(conn, "tender_lots"):
        return {"localities": []}
    clause, params = "", []
    if locality:
        clause = " AND t.locality LIKE ?"
        params.append(f"%{locality}%")
    rows = [dict(r) for r in conn.execute(
        f"""SELECT t.locality, COUNT(*) lots,
                   ROUND(AVG(l.winning_price / l.area_m2), 1) avg_per_m2,
                   ROUND(MIN(l.winning_price / l.area_m2), 1) min_per_m2,
                   ROUND(MAX(l.winning_price / l.area_m2), 1) max_per_m2,
                   ROUND(SUM(l.winning_price)) total_nis,
                   MIN(t.published_at) first_sale, MAX(t.published_at) last_sale
            FROM tender_lots l JOIN tenders t ON t.michraz_id = l.michraz_id
            WHERE l.winning_price > 0 AND l.area_m2 > 0
              AND t.locality IS NOT NULL{clause}
            GROUP BY t.locality HAVING COUNT(*) >= ?
            ORDER BY avg_per_m2 DESC""", params + [min_lots])]
    return {"localities": rows, "basis": "winning_price",
            "note": 'מחירי קרקע ממכרזי רמ"י שהסתיימו - עסקאות בפועל. '
                    'זו קרקע, לא דירות יד שנייה.'}


def open_now(conn, limit=60) -> list[dict]:
    """Tenders a user can still bid on, closing soonest first."""
    if not _has(conn, "tenders"):
        return []
    return [dict(r) for r in conn.execute(
        """SELECT t.*, (SELECT COUNT(*) FROM tender_lots l
                         WHERE l.michraz_id = t.michraz_id) lots
           FROM tenders t WHERE t.status_code IN (1,2)
           ORDER BY t.closes_at ASC LIMIT ?""", (limit,))]


def tender_url(michraz_id) -> str:
    return f"https://apps.land.gov.il/MichrazimSite/#/michraz/{michraz_id}"


if __name__ == "__main__":
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    # unit tests for the traps that would silently corrupt statistics
    assert _split_gush("10022201") == ("10022201", None), _split_gush("10022201")
    assert _split_gush("21218") == ("21218", "21218")
    assert _split_gush(" 6894 ") == ("6894", "6894")
    assert _no_bid(" ", 0.0) is True
    assert _no_bid("אין הצעות למתחם זה", 0.0) is True
    assert _no_bid("ארזי הנגב ייזום ובניה", 8256825.0) is False
    assert _price_per_m2(100, 500_000, 400_000, 300_000) == (5000.0, "winning_price")
    assert _price_per_m2(100, None, 400_000, 300_000) == (4000.0, "appraised_price")
    assert _price_per_m2(100, None, None, 300_000) == (3000.0, "reserve_price")
    assert _price_per_m2(0, 500_000, None, None) == (None, None)
    print("parser self-tests passed\n")

    conn = db.get_conn()
    total = import_tenders(conn, progress=lambda k, n: print(f"  {k}: {n:,}"))
    print(f"tender index: {total:,}")
    stats = import_lots(conn, limit=int(sys.argv[1]) if len(sys.argv) > 1 else 40,
                        progress=lambda k, n: print(f"  lots: {n:,}", end="\r"))
    print(f"\nlots: {json.dumps(stats, ensure_ascii=False)}")
    print(f"counts: {json.dumps(counts(conn), ensure_ascii=False)}")
    print("\nopen for bids now (closing soonest):")
    for t in open_now(conn, limit=6):
        print(f"  {t['name']:10} {str(t['locality'])[:16]:16} "
              f"{str(t['neighborhood'] or '')[:14]:14} units={t['housing_units']:>4} "
              f"closes {t['closes_at']} lots={t['lots']}")
    bench = price_benchmarks(conn, min_lots=1)
    print(f"\nland ₪/m² from concluded tenders ({len(bench['localities'])} localities):")
    for row in bench["localities"][:8]:
        print(f"  {str(row['locality'])[:18]:18} {row['lots']:>3} lots  "
              f"avg {row['avg_per_m2']:>10,.0f} ₪/m²  "
              f"({row['first_sale']}..{row['last_sale']})")
    conn.close()
