"""The other side of the same board: what these flats let for.

Why this is the missing half
----------------------------
Every measure in this system so far compares an asking price to another asking
price. That answers "is this cheap for the area" and cannot answer the question
an investor actually asks - *what does it return*. A 2,400,000 ₪ flat is a
different proposition at 5,500 ₪ a month than at 9,000 ₪, and no amount of
comparing it to other 2,400,000 ₪ flats will say which it is.

Rent is the only independent yardstick available here. Sale prices are opinions
until somebody signs; a rent is a price two parties already agreed on, monthly,
and it moves with what a place is worth to live in rather than with what buyers
hope it will be worth later. Where the two diverge - Tel Aviv's yields against
Beer Sheva's - is information no sale-side comparison contains.

Where it comes from
-------------------
The same ONMAP endpoint `onmap_feed` already uses, with one parameter changed:
`option=rent` instead of `option=buy`. Same payload shape, same normaliser, so
this module is a harvester and a table and borrows the parsing wholesale.

Two things differ from the sale feed and both matter:

* **The window is much shallower.** The sale feed paginates to `$skip≈3000` per
  ordering; rent stops returning rows between 480 and 960. Four orderings still
  union to a usable sample, but this is a *sample of the current board*, not an
  inventory, and nothing downstream should treat a count from here as market
  size.
* **The fields are far better.** Measured over 246 rows: price, rooms, area,
  city, neighbourhood, street, coordinates, property type and date are present
  on **100%**, against the sale feed where area and rooms are routinely absent.
  A rent benchmark can therefore be built on rooms *and* size, which is what
  makes the yield comparable between a studio and a five-room flat.

The plausibility window, and why it is not optional
----------------------------------------------------
The same feed returned a "rent" of 3,000,000 ₪ and one of 480 ₪ in a 246-row
sample. The first is a sale price or a whole building filed under the wrong
option; the second is a parking space, a storage room, or a per-night figure.
Either one entering a benchmark moves it enough to invert the answer for every
listing in that neighbourhood - a single 3,000,000 in a ten-row neighbourhood
sample makes every flat around it look like a 40% yield. Rents outside
`MIN_PLAUSIBLE_RENT`..`MAX_PLAUSIBLE_RENT` are stored (nothing is hidden) and
excluded from every benchmark, exactly as `listing_analytics` treats an
implausible floor area.
"""

from __future__ import annotations

import json
import threading

import db
import onmap_feed
from onmap_feed import OnmapError, normalise

#: Orderings walked in turn, unioned by id - the same trick the sale harvest
#: uses to see past a single ordering's depth limit.
SORTS = ("-search_date", "search_date", "price", "-price")

#: Measured, not guessed: `$skip=480` still returns a full page and `$skip=960`
#: returns nothing, on every ordering. Walking to the sale feed's 3,000 would
#: be ~100 pointless requests against someone else's API per run.
MAX_SKIP = 1000
PAGE_SIZE = onmap_feed.PAGE_SIZE

#: A monthly residential rent in Israel. Below the floor the row is a parking
#: space, a storage unit or a nightly rate; above the ceiling it is a sale
#: price filed under the wrong option, a whole building, or a yearly figure.
#: Rows outside the window are kept and flagged, never used as a benchmark.
MIN_PLAUSIBLE_RENT = 1_000
MAX_PLAUSIBLE_RENT = 60_000

TABLES = """
CREATE TABLE IF NOT EXISTS onmap_rentals (
    id            TEXT PRIMARY KEY,
    price         INTEGER,          -- monthly rent, ILS
    currency      TEXT,
    property_type TEXT,
    city          TEXT,
    neighborhood  TEXT,
    street        TEXT,
    house_number  TEXT,
    address_text  TEXT,
    rooms         REAL,
    area_sqm      REAL,
    floor         TEXT,
    bathrooms     REAL,
    parking       REAL,
    lat           REAL,
    lon           REAL,
    created_at    TEXT,
    search_date   TEXT,
    slug          TEXT,
    url           TEXT,
    image         TEXT,
    phone         TEXT,
    plausible     INTEGER NOT NULL DEFAULT 1,  -- inside the rent window
    first_seen_at TEXT,
    last_seen_at  TEXT,
    seen_count    INTEGER NOT NULL DEFAULT 1,
    delisted_at   TEXT,
    raw_json      TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_rent_city  ON onmap_rentals(city, neighborhood);
CREATE INDEX IF NOT EXISTS idx_rent_usable ON onmap_rentals(plausible, delisted_at);
"""

COLUMNS = ("id", "price", "currency", "property_type", "city", "neighborhood",
           "street", "house_number", "address_text", "rooms", "area_sqm",
           "floor", "bathrooms", "parking", "lat", "lon", "created_at",
           "search_date", "slug", "url", "image", "phone", "plausible")


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


def plausible_rent(price) -> bool:
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return MIN_PLAUSIBLE_RENT <= value <= MAX_PLAUSIBLE_RENT


def fetch_page(skip: int, sort: str, *, timeout: float = 60.0) -> dict:
    """One page of the rent board.

    Deliberately not `onmap_feed.fetch_page`: that one hard-codes the
    definition's `option=buy`. Everything else - URL, headers, cookies - is
    shared, so a session that works for one works for the other.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    definition = onmap_feed._definition()
    params = dict(definition.get("params") or {})
    params.update({"option": "rent", "$skip": str(int(skip)), "$sort": sort,
                   "$limit": str(PAGE_SIZE)})
    url = f"{definition['url']}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=onmap_feed._headers(definition))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OnmapError(f"HTTP {exc.code} at rent skip={skip} sort={sort}") from exc
    except Exception as exc:
        raise OnmapError(f"{type(exc).__name__} at rent skip={skip}: {exc}") from exc


def store(conn, rows: list[dict]) -> tuple[int, int]:
    """Upsert normalised rentals. Returns (seen, newly inserted)."""
    if not rows:
        return 0, 0
    now = onmap_feed._now()
    before = conn.execute("SELECT COUNT(*) FROM onmap_rentals").fetchone()[0]
    payload = []
    for row in rows:
        values = [row.get(c) for c in COLUMNS[:-1]]
        values.append(1 if plausible_rent(row.get("price")) else 0)
        payload.append((*values, now, now,
                        json.dumps(row.get("raw"), ensure_ascii=False)))
    conn.executemany(
        f"""INSERT INTO onmap_rentals ({", ".join(COLUMNS)}, first_seen_at,
                last_seen_at, raw_json)
            VALUES ({", ".join("?" for _ in COLUMNS)}, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                {", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "id")},
                last_seen_at=excluded.last_seen_at,
                seen_count=onmap_rentals.seen_count + 1,
                delisted_at=NULL,
                raw_json=excluded.raw_json""", payload)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM onmap_rentals").fetchone()[0]
    return len(payload), after - before


_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None
_STATE: dict = {"status": "idle", "pages": 0, "rows_seen": 0, "rows_new": 0,
                "sort": None, "error": ""}


def counts(conn=None) -> dict:
    own = conn is None
    conn = conn or db.get_conn()
    try:
        ensure_schema(conn)
        row = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN plausible = 1 THEN 1 ELSE 0 END) usable,
                      COUNT(DISTINCT city) cities,
                      MAX(last_seen_at) last
                 FROM onmap_rentals WHERE delisted_at IS NULL""").fetchone()
        return {"total": row["n"] or 0, "usable": row["usable"] or 0,
                "cities": row["cities"] or 0, "last_seen": row["last"]}
    except Exception:
        return {"total": 0, "usable": 0, "cities": 0, "last_seen": None}
    finally:
        if own:
            conn.close()


def state() -> dict:
    snapshot = dict(_STATE)
    snapshot.update(counts())
    snapshot["running"] = snapshot["status"] == "running"
    return snapshot


def harvest(*, sorts=SORTS, max_skip: int = MAX_SKIP) -> dict:
    """Walk every ordering to the rent feed's depth limit, unioning by id."""
    conn = db.get_conn()
    ensure_schema(conn)
    _STATE.update(status="running", pages=0, rows_seen=0, rows_new=0, error="")
    pages = seen = new = 0
    status, error = "complete", ""
    try:
        for sort in sorts:
            _STATE["sort"] = sort
            skip = 0
            while skip < max_skip:
                try:
                    payload = fetch_page(skip, sort)
                except OnmapError as exc:
                    error = str(exc)
                    break
                data = payload.get("data")
                if not isinstance(data, list) or not data:
                    break
                normalised = [n for n in (normalise(r) for r in data) if n]
                wave_seen, wave_new = store(conn, normalised)
                pages += 1
                seen += wave_seen
                new += wave_new
                _STATE.update(pages=pages, rows_seen=seen, rows_new=new)
                if not (payload.get("meta") or {}).get("hasNextPage", True):
                    break
                skip += PAGE_SIZE
    except Exception as exc:                        # pragma: no cover
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
        _STATE.update(status=status, error=error)
    return state()


def start(**kwargs) -> dict:
    global _WORKER
    with _LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return state()
        _WORKER = threading.Thread(target=harvest, kwargs=kwargs,
                                   name="onmap-rent-harvest", daemon=True)
        _STATE.update(status="running", error="")
        _WORKER.start()
    return state()


def rows(*, usable_only: bool = True) -> list[dict]:
    """Stored rentals. `usable_only` drops the implausible-rent rows."""
    conn = db.get_conn()
    try:
        ensure_schema(conn)
        sql = ("""SELECT id, price, property_type, city, neighborhood, street,
                         house_number, rooms, area_sqm, floor, lat, lon,
                         created_at, url, plausible
                    FROM onmap_rentals
                   WHERE delisted_at IS NULL""")
        if usable_only:
            sql += " AND plausible = 1"
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


if __name__ == "__main__":                       # pragma: no cover - CLI
    result = harvest()
    print(json.dumps(result, ensure_ascii=False, indent=2))
