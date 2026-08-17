"""Transparent locality strategy metrics from already-loaded public data."""

from __future__ import annotations

import math
from services.external import market_client as market


ROLE_VIEWS = {
    "investor": {"label": "משקיע פרטי", "focus": ["מחיר יחסי", "סיכון", "מלאי עתידי"],
                 "report": "דוח חשיפה אזורית: תנודתיות, צנרת עתידית ופערי כיסוי."},
    "developer": {"label": "יזם", "focus": ["תחרות לפרויקט", "תכנון", "היתרים ובנייה"],
                  "report": "דוח צנרת מתחרה: שימושים, שלבים, יח״ד ומוקדי בנייה."},
    "broker": {"label": "מתווך", "focus": ["מלאי מורשה", "מגמות", "התראות"],
               "report": "דוח שוק מקומי: צנרת, תנודתיות ונכסים מפיד מורשה כשקיים."},
    "appraiser": {"label": "שמאי", "focus": ["מקורות", "השוואה", "כיסוי נתונים"],
                 "report": "דוח ראיות: מקורות, מגבלות כיסוי ואותות להשוואה מקצועית."},
    "lender": {"label": "בנק / מממן", "focus": ["סיכון", "בנייה", "מלאי עתידי"],
               "report": "דוח חשיפת פרויקט: תחרות, קצב בנייה ופערי כיסוי היתר."},
    "authority": {"label": "רשות מקומית", "focus": ["צנרת תכנונית", "התחדשות", "היתרים"],
                  "report": "דוח קיבולת עירונית: צנרת, התחדשות, היתרים ובנייה מתועדת."},
}


def _has(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())


def _normal(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def risk_from_signals(*, future_units: int, approved_units: int,
                      stalled_units: int, permit_coverage: bool) -> dict:
    """Planning exposure, explicitly not a financial or credit rating."""
    supply = min(45, round(math.log1p(max(0, future_units)) * 5))
    stalled = min(35, round(math.log1p(max(0, stalled_units)) * 5))
    coverage_gap = 20 if approved_units and not permit_coverage else 0
    score = min(100, supply + stalled + coverage_gap)
    # "No matching data" and "a genuinely quiet area" both score 0, and calling
    # both of them "low exposure" is the one reading that is definitely wrong: a
    # misspelt locality would present as a safe area. Say so instead.
    no_data = not (future_units or approved_units or stalled_units)
    if no_data:
        return {"score": None, "label": "אין נתונים לאזור הזה",
                "components": [],
                "caveat": "לא נמצאו תכניות, מלאי או מתחמים בשם הזה. בדוק את "
                          "שם הרשות — היעדר נתונים אינו סיכון נמוך."}
    return {"score": score,
            "label": "חשיפת תכנון נמוכה" if score < 25 else
                     "חשיפת תכנון בינונית" if score < 55 else "חשיפת תכנון גבוהה",
            "components": [
                {"name": "לחץ מלאי עתידי", "points": supply, "evidence": future_units},
                {"name": "יח״ד במתחמים תקועים", "points": stalled, "evidence": stalled_units},
                {"name": "פער כיסוי היתרים", "points": coverage_gap,
                 "evidence": bool(approved_units and not permit_coverage)},
            ],
            "caveat": "זהו מדד חשיפת תכנון, לא דירוג אשראי, שווי או המלצת השקעה."}


def _volatility(conn, district: str | None) -> dict:
    series_id = market.DISTRICT_TO_CBS.get(_normal(district))
    if not series_id or not _has(conn, "cbs_price_index"):
        return {"value": None, "coverage": "אין סדרת למ״ס מחוזית תואמת"}
    values = [row[0] for row in conn.execute(
        """SELECT pct_change FROM cbs_price_index WHERE series_id=?
           AND period_type='month' AND pct_change IS NOT NULL
           ORDER BY period DESC LIMIT 12""", (series_id,))]
    if len(values) < 4:
        return {"value": None, "coverage": "אין מספיק חודשי מדד לחישוב"}
    mean = sum(values) / len(values)
    return {"value": round((sum((x - mean) ** 2 for x in values) / len(values)) ** .5, 2),
            "unit": "סטיית תקן חודשית באחוזים", "periods": len(values),
            "coverage": "מדד מחירי דירות למ״ס — מחוז"}


def role_kpis(role: str, metrics: dict, competition: dict, risk: dict) -> list[dict]:
    """Select different decision cards per role; values are never personalised."""
    all_kpis = {
        "future": {"label": "מלאי עתידי", "value": metrics["future_inventory_units"],
                   "note": "רמ״י + התחדשות"},
        "approved": {"label": "יח״ד מאושרות", "value": metrics["approved_units"],
                     "note": "תוכניות באזור"},
        "construction": {"label": "בנייה מתועדת", "value": metrics["construction_units"],
                         "note": f'{competition["construction_sites"]} אתרים · {metrics["construction_updates_last_year"] if metrics["construction_updates_last_year"] is not None else "אין קצב"} עדכונים/12ח׳'},
        "risk": {"label": "חשיפת תכנון", "value": risk["score"], "note": risk["label"]},
        "volatility": {"label": "תנודתיות מחוזית", "value": metrics["price_volatility"]["value"],
                       "note": metrics["price_volatility"].get("unit", "אין כיסוי")},
        "permits": {"label": "בקשות היתר (12 חוד׳)", "value": metrics["permit_requests_last_year"],
                    "note": "כיסוי מקומי בלבד"},
        "rami": {"label": "צנרת רמ״י", "value": competition["rami_units"],
                 "note": f'{competition["rami_projects"]} פרויקטים'},
    }
    keys = {
        "investor": ("volatility", "future", "risk", "approved"),
        "developer": ("future", "rami", "construction", "risk"),
        "broker": ("future", "approved", "volatility", "construction"),
        "appraiser": ("approved", "volatility", "construction", "permits"),
        "lender": ("risk", "construction", "approved", "future"),
        "authority": ("future", "approved", "permits", "construction"),
    }.get(role, ("future", "approved", "construction", "risk"))
    return [all_kpis[key] for key in keys]


def locality_strategy(conn, locality: str, role: str = "investor") -> dict:
    """Area snapshot, with missing data explicitly reported as missing."""
    locality = _normal(locality)
    if len(locality) < 2:
        raise ValueError("locality must contain at least two characters")
    role = role if role in ROLE_VIEWS else "investor"
    like = f"%{locality}%"
    plans, plan_units, approved_units, district = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(housing_units),0),
                  COALESCE(SUM(CASE WHEN station LIKE '%אישור%' THEN housing_units ELSE 0 END),0),
                  MAX(district_name) FROM plans WHERE jurisdiction_name LIKE ?""", (like,)
    ).fetchone()
    inventory_rows = inventory_units = 0
    if _has(conn, "rami_inventory"):
        inventory_rows, inventory_units = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(units_potential),0) FROM rami_inventory WHERE locality LIKE ?", (like,)
        ).fetchone()
        rami_stages = [dict(row) for row in conn.execute(
            """SELECT COALESCE(stage,'לא סווג') stage, COUNT(*) projects,
                      COALESCE(SUM(units_potential),0) units
               FROM rami_inventory WHERE locality LIKE ? GROUP BY 1 ORDER BY units DESC""", (like,)
        )]
    else:
        rami_stages = []
    landuses = [dict(row) for row in conn.execute(
        """SELECT COALESCE(landuse,'לא סווג') landuse, COUNT(*) plans,
                  COALESCE(SUM(housing_units),0) units
           FROM plans WHERE jurisdiction_name LIKE ? GROUP BY 1 ORDER BY units DESC LIMIT 12""", (like,)
    )]
    renewal_complexes = renewal_units = stalled_units = 0
    if _has(conn, "urban_renewal"):
        renewal_complexes, renewal_units, stalled_units = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(CAST(units_planned AS INTEGER)),0),
                      COALESCE(SUM(CASE WHEN COALESCE(CAST(permits AS INTEGER),0)=0
                                        THEN CAST(units_planned AS INTEGER) ELSE 0 END),0)
               FROM urban_renewal WHERE locality LIKE ?""", (like,)
        ).fetchone()
    construction_sites = construction_units = 0
    construction_updates = None
    if _has(conn, "building_progress"):
        construction_sites, construction_units = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(units),0) FROM building_progress WHERE locality LIKE ?", (like,)
        ).fetchone()
        construction_updates = conn.execute(
            "SELECT COUNT(*) FROM building_progress WHERE locality LIKE ? AND decisive_date >= date('now','-365 days')", (like,)
        ).fetchone()[0]
        construction_stages = [dict(row) for row in conn.execute(
            """SELECT COALESCE(first_stage,'לא סווג') stage, COUNT(*) sites,
                      COALESCE(SUM(units),0) units
               FROM building_progress WHERE locality LIKE ? GROUP BY 1 ORDER BY units DESC""", (like,)
        )]
    else:
        construction_stages = []
    permit_coverage = _has(conn, "muni_permits") and "תל אביב" in locality
    permit_requests = None
    if permit_coverage:
        permit_requests = conn.execute(
            "SELECT COUNT(*) FROM muni_permits WHERE city LIKE ? AND requested_at >= date('now','-365 days')", (like,)
        ).fetchone()[0]
    future_units = int(inventory_units or 0) + int(renewal_units or 0)
    risk = risk_from_signals(future_units=future_units, approved_units=int(approved_units or 0),
                             stalled_units=int(stalled_units or 0), permit_coverage=permit_coverage)
    metrics = {"planning_units": int(plan_units or 0), "approved_units": int(approved_units or 0),
               "future_inventory_units": future_units, "construction_units": int(construction_units or 0),
               "construction_updates_last_year": construction_updates,
               "permit_requests_last_year": permit_requests, "price_volatility": _volatility(conn, district),
               "transaction_volume": {"value": None, "coverage": "נדרש פיד עסקאות מורשה"}}
    competition = {"plans": int(plans or 0), "approved_units": int(approved_units or 0),
                   "rami_projects": int(inventory_rows or 0), "rami_units": int(inventory_units or 0),
                   "renewal_complexes": int(renewal_complexes or 0), "renewal_units": int(renewal_units or 0),
                   "construction_sites": int(construction_sites or 0), "construction_units": int(construction_units or 0),
                   "rami_stages": rami_stages, "landuses": landuses,
                   "construction_stages": construction_stages,
                   "interpretation": "צנרת מתחרה/משלימה לפי תכנון, התחדשות ובנייה מתועדת — לא מלאי שיווקי חי."}
    return {
        "locality": locality, "role": role, "role_view": ROLE_VIEWS[role],
        "metrics": metrics, "competition": competition,
        "role_kpis": role_kpis(role, metrics, competition, risk),
        "risk": risk,
        "coverage": {"plans": True, "rami_inventory": _has(conn, "rami_inventory"),
                     "renewal": _has(conn, "urban_renewal"), "construction": _has(conn, "building_progress"),
                     "permits": "תל אביב-יפו בלבד" if _has(conn, "muni_permits") else "לא יובא",
                     "transactions": "נדרש פיד עסקאות מורשה"},
    }
