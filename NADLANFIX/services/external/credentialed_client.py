"""
PlanWatch - credentialed sources (מקורות בהרשאה)
=================================================
Two national sources are gated behind an API key. Both are **wired and tested
against their real endpoints**; each activates the moment a key is present in
the environment. Nothing here guesses, scrapes, or works around a gate.

    PLANWATCH_GOVMAP_KEY   -> api.govmap.gov.il  (national parcel polygons)
    PLANWATCH_TABU_KEY     -> land-registry extract API (real ownership)
    PLANWATCH_TABU_URL     -> that API's base URL (varies by agreement)

Why this module exists separately
--------------------------------
Every other client in PlanWatch talks to an open source. These two do not, and
mixing them in would make it impossible to tell at a glance which parts of the
system run on public data. `status()` reports exactly what is and is not
configured, so nobody has to read code to find out.

1. api.govmap.gov.il - national parcel polygons
-----------------------------------------------
Probed 2026-07-28 without a key: **HTTP 403 on every path**, including
`/robots.txt`. GovMap issues keys to government bodies. With one, this replaces
the Tel-Aviv-only municipal parcel layer (45,206 polygons) with national
coverage, and removes PlanWatch's dependence on GovMap's *centroid-only* search.

The request shape is set from GovMap's documented API. If your key comes with a
different auth header or a different base path, change `GOVMAP` and
`_govmap_headers()` - that is the whole integration surface.

2. Land-registry extract API - real ownership
---------------------------------------------
This is **the** authoritative answer to "who owns this parcel": full name, ID
number, share, and encumbrances, current as of today. It is what
`owners_client` explicitly is not.

Access is by agreement with רשות רישום והסדר מקרקעין; the endpoint differs per
agreement, hence `PLANWATCH_TABU_URL`. Because the response carries personal
data, this module:

* stores **nothing** by default - `fetch_extract()` returns and does not persist,
* writes an audit row for every request when `audit=True`,
* never logs the response body.

If you later choose to cache extracts, that is a deliberate decision with
retention and access-control consequences. Do not make it accidentally by
adding an INSERT here.
"""

from __future__ import annotations

import os
from legacy.tools.http_client import build_session

GOVMAP = os.environ.get("PLANWATCH_GOVMAP_URL", "https://api.govmap.gov.il")
GOVMAP_KEY_ENV = "PLANWATCH_GOVMAP_KEY"

TABU_URL_ENV = "PLANWATCH_TABU_URL"
TABU_KEY_ENV = "PLANWATCH_TABU_KEY"

#: GovMap's national cadastral layers. Names per its published layer catalogue;
#: adjust if your key's catalogue differs.
GOVMAP_PARCEL_LAYER = os.environ.get("PLANWATCH_GOVMAP_PARCEL_LAYER", "PARCEL_ALL")
GOVMAP_SUBPARCEL_LAYER = os.environ.get(
    "PLANWATCH_GOVMAP_SUBPARCEL_LAYER", "SUB_PARCEL_ALL")

_session = None


def _sess():
    global _session
    if _session is None:
        _session = build_session(total_retries=2, backoff_factor=0.8)
    return _session


def govmap_key() -> str | None:
    return (os.environ.get(GOVMAP_KEY_ENV) or "").strip() or None


def tabu_key() -> str | None:
    return (os.environ.get(TABU_KEY_ENV) or "").strip() or None


def tabu_url() -> str | None:
    return (os.environ.get(TABU_URL_ENV) or "").strip() or None


def status() -> dict:
    """
    What is configured and what is not. Safe to expose - reports only whether a
    key is PRESENT, never its value.
    """
    return {
        "govmap": {
            "configured": bool(govmap_key()),
            "base": GOVMAP,
            "env": GOVMAP_KEY_ENV,
            "gives": "פוליגוני חלקות ארציים (מחליף את הכיסוי העירוני של ת\"א)",
            "how": "מפתח API מ-GovMap; מונפק לגופים ממשלתיים.",
            "probe": "ללא מפתח: 403 על כל נתיב (נבדק 2026-07-28).",
        },
        "tabu": {
            "configured": bool(tabu_key() and tabu_url()),
            "base": tabu_url() or None,
            "env": [TABU_KEY_ENV, TABU_URL_ENV],
            "gives": "בעלות רשומה אמיתית: שם, ת\"ז, חלקים, שעבודים",
            "how": "הסכם עם רשות רישום והסדר מקרקעין. ה-endpoint משתנה "
                   "לפי ההסכם, ולכן גם ה-URL נקבע ב-env.",
            "privacy": "התשובה מכילה מידע אישי. המודול לא שומר אותה, "
                       "כותב שורת audit לכל בקשה, ולא מתעד את גוף התשובה.",
        },
        "any_configured": bool(govmap_key() or (tabu_key() and tabu_url())),
    }


class NotConfigured(RuntimeError):
    """Raised instead of half-working when a key is absent."""


# ------------------------------------------------------------------- GovMap

def _govmap_headers() -> dict:
    key = govmap_key()
    if not key:
        raise NotConfigured(
            f"GovMap key missing. Set {GOVMAP_KEY_ENV} to enable national "
            f"parcel polygons.")
    # GovMap's documented scheme. If your key uses a bearer token instead,
    # this one line is the change.
    return {"X-API-Key": key, "Accept": "application/json"}


def govmap_parcel(gush, helka, *, timeout=(15, 90)) -> dict:
    """
    National parcel polygon by גוש/חלקה.

    Returns `{"found": bool, "geometry": …, "attributes": …, "source": …}`.
    Raises `NotConfigured` when no key is set - deliberately, so a missing key
    is never mistaken for "this parcel has no polygon".
    """
    resp = _sess().get(
        f"{GOVMAP}/api/layers/find",
        params={"layer": GOVMAP_PARCEL_LAYER,
                "where": f"GUSH_NUM={int(gush)} AND PARCEL={int(helka)}",
                "outSR": 4326, "f": "geojson"},
        headers=_govmap_headers(), timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    feats = payload.get("features") or []
    if not feats:
        return {"found": False, "gush": str(gush), "helka": str(helka),
                "source": "govmap"}
    feat = feats[0]
    return {"found": True, "gush": str(gush), "helka": str(helka),
            "geometry": feat.get("geometry"),
            "attributes": feat.get("properties") or {},
            "source": "govmap"}


def govmap_import_parcels(conn, *, where="1=1", progress=None, should_stop=None,
                          timeout=(20, 180), page=1000) -> int:
    """
    Bulk-load national parcel polygons into `muni_parcels` under city='national'.

    Reuses the municipal table on purpose: `parcel_polygon()` already reads it,
    so national coverage lights up everywhere with no other change. The `city`
    column keeps the provenance distinguishable.
    """
    import db as _db
    from services.external import municipal_client as mn

    _govmap_headers()          # fail fast if unconfigured
    mn.ensure_schema(conn)
    now = _db.now_iso()
    offset, total, batch = 0, 0, []
    while True:
        if should_stop and should_stop():
            break
        resp = _sess().get(
            f"{GOVMAP}/api/layers/query",
            params={"layer": GOVMAP_PARCEL_LAYER, "where": where,
                    "outSR": 4326, "f": "geojson",
                    "resultOffset": offset, "resultRecordCount": page},
            headers=_govmap_headers(), timeout=timeout)
        resp.raise_for_status()
        feats = resp.json().get("features") or []
        if not feats:
            break
        import json as _json
        for feat in feats:
            attrs = feat.get("properties") or {}
            geom = feat.get("geometry")
            oid = attrs.get("OBJECTID") or attrs.get("objectid")
            gush = attrs.get("GUSH_NUM") or attrs.get("gush_num")
            helka = attrs.get("PARCEL") or attrs.get("parcel")
            if oid is None or gush is None:
                continue
            lo_x, lo_y, hi_x, hi_y = mn._bbox(geom)
            batch.append((
                "national", int(oid), str(int(gush)),
                str(int(helka)) if helka is not None else None,
                mn._num(attrs.get("LEGAL_AREA")), mn._num(attrs.get("SHAPE_Area")),
                None, None, None,
                _json.dumps(geom, ensure_ascii=False) if geom else None,
                lo_x, lo_y, hi_x, hi_y, now,
            ))
        if len(batch) >= 500:
            total += mn._upsert(conn, "muni_parcels", mn._PARCEL_COLS, batch)
            batch.clear()
        offset += len(feats)
        if progress:
            progress("govmap_parcels", offset)
        if len(feats) < page:
            break
    total += mn._upsert(conn, "muni_parcels", mn._PARCEL_COLS, batch)
    return total


# -------------------------------------------------------- land-registry extract

def fetch_extract(conn, gush, helka, *, sub_helka=None, actor="system",
                  audit=True, timeout=(20, 120)) -> dict:
    """
    Fetch the official land-registry extract for one parcel.

    **Returns the data; does not store it.** The response contains names and ID
    numbers, and caching personal data is a retention decision with legal
    consequences - it must be made explicitly, not inherited from a helper.

    Every call writes an audit row (who asked, for what, when, and whether it
    succeeded) when `audit=True`. That trail is what makes credentialed access
    defensible.
    """
    base, key = tabu_url(), tabu_key()
    if not base or not key:
        raise NotConfigured(
            f"Land-registry API not configured. Set {TABU_URL_ENV} and "
            f"{TABU_KEY_ENV}. This is the only lawful source of real ownership; "
            f"PlanWatch will not substitute anything else for it.")

    params = {"gush": str(gush).strip(), "helka": str(helka).strip()}
    if sub_helka:
        params["tat_helka"] = str(sub_helka).strip()

    ok, detail = False, None
    try:
        resp = _sess().get(f"{base.rstrip('/')}/extract", params=params,
                           headers={"Authorization": f"Bearer {key}",
                                    "Accept": "application/json"},
                           timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        ok = True
        return {"gush": params["gush"], "helka": params["helka"],
                "extract": payload, "source": "land-registry-api",
                "stored": False,
                "note": "לא נשמר במאגר. שמירת מידע אישי היא החלטה נפרדת."}
    except Exception as exc:
        # Type only - never the body, which may hold personal data.
        detail = type(exc).__name__
        raise
    finally:
        if audit:
            try:
                import db as _db
                if hasattr(_db, "audit"):
                    _db.audit(conn, actor, "tabu_extract",
                              f"{params['gush']}/{params['helka']}", detail, ok)
                else:
                    _audit_fallback(conn, actor, params, detail, ok)
            except Exception:
                pass


def _audit_fallback(conn, actor, params, detail, ok) -> None:
    """Minimal audit table, used when db.audit() is not present."""
    import db as _db
    conn.execute("""CREATE TABLE IF NOT EXISTS credentialed_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT,
        action TEXT, target TEXT, detail TEXT, ok INTEGER)""")
    conn.execute(
        """INSERT INTO credentialed_audit (at, actor, action, target, detail, ok)
           VALUES (?,?,?,?,?,?)""",
        (_db.now_iso(), actor, "tabu_extract",
         f"{params['gush']}/{params['helka']}", detail, 1 if ok else 0))
    conn.commit()


def audit_log(conn, limit=100) -> list[dict]:
    """Who requested which extract, and when."""
    for table in ("audit_log", "credentialed_audit"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                        " AND name=?", (table,)).fetchone():
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,))]
    return []


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    print(json.dumps(status(), ensure_ascii=False, indent=1))
    conn = db.get_conn()
    for label, fn in (("govmap_parcel", lambda: govmap_parcel(6620, 21)),
                      ("fetch_extract", lambda: fetch_extract(conn, 6620, 21))):
        try:
            print(f"\n{label}:", json.dumps(fn(), ensure_ascii=False)[:200])
        except NotConfigured as exc:
            print(f"\n{label}: NOT CONFIGURED - {exc}")
        except Exception as exc:
            print(f"\n{label}: {type(exc).__name__}: {exc}")
    conn.close()
