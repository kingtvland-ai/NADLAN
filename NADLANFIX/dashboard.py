"""
PlanWatch - local dashboard
============================
A clickable GUI over the database, so nothing needs the command line.

    python dashboard.py                 # then open http://127.0.0.1:8000
    python dashboard.py --port 9000 --no-browser

Built on the standard library only (`http.server`) - no Flask/FastAPI to
install. It binds to 127.0.0.1 by default, so it is reachable only from this
machine; there is no authentication, so do not expose it to a network without
putting a real server and login in front of it.

The browser page is `dashboard.html`; this module is the JSON API behind it.

Endpoints
---------
GET  /api/bootstrap                  stats + filter dropdown values
GET  /api/search?q=                  ONE smart box: classifies the query and
                                     fans out to parcels / plans / localities /
                                     addresses / contractors / appraisers
GET  /api/parcel?gush=&helka=        the parcel view: resolve via GovMap,
                                     covering plans, land-registry ownership
GET  /api/live-deals?gush=&helka=     completed sale/purchase deals, read-only
                                     exact parcel lookup; never persisted
GET  /api/live-sale-listings?limit=400 live Yad2 sale listings via local connector
GET  /api/combined-sale-listings   all existing feeds + up to 18,000 deduplicated Yad2 listings
GET  /api/live-sources              live status of connected real-estate sources
GET  /api/opendata?set=&q=           browse contractors / appraisers / localities
GET  /api/google-places?locality=&category=  optional live business lookup (key required)
GET  /api/market                     price/market snapshot: CBS index, ₪/m² spread
GET  /api/opportunities?<filters>    ranked urban-renewal candidates (scored)
GET  /api/tama38?limit=              localities by תמ"א 38 activity = old stock
GET  /api/deals?<filters>            scored for-sale listings (needs a licensed feed)
GET  /api/strategy?locality=&role=   area strategy: pipeline, competition, risk, coverage
GET  /api/localities?limit=          one row per locality, everything we know
GET  /api/plan-detail?mp_id=&fetch=  the plan's CONTENTS: land-use cells + mix
GET  /api/landuse-at?lat=&lon=       which land-use cells cover a point
GET  /api/designations?limit=        every מבא"ת designation seen, by area
GET  /api/appraisals?gush=&helka=    decisive appraisals (שמאות מכריעה)
GET  /api/progress?gush=&helka=      per-building construction milestones
GET  /api/planners?locality=         surveyors and planners
GET  /api/documents[?appraisal_id=|validate=1]  ruling PDFs + CSV agreement
GET  /api/data-quality               cross-source join agreement report
GET  /api/tenders[?open=1&locality=] RAMI land tenders - property for sale
GET  /api/tender-lots?michraz_id=    one tender's lots: parcel, area, prices
GET  /api/land-prices[?locality=]    land ₪/m² from CONCLUDED tenders
GET  /api/municipal                  municipal GIS: ages, תמ"א 38 permits
GET  /api/parcel-polygon?gush=&helka= the parcel's REAL polygon
GET  /api/owners?gush=&helka=|name=  published rights-holder traces
GET  /api/compare[?locality=]        asking vs concluded vs subsidised ₪/m²
GET  /api/compare-parcel?gush=&helka= every price signal on one parcel
GET  /api/dormant[?kind=]            dormant land: 5 detectors + stalled years
GET  /api/estates                    estate PROXIES (no inheritance field exists)
GET  /api/crossref                   post-import cross-source links
GET  /api/alerts                     alert status: pending, channels, SMTP
POST /api/alerts/watch               run every watcher, raise new alerts
POST /api/alerts/deliver             send pending alerts as per-client digests
POST /api/alerts/seed                baseline a subscription (no alerts)
POST /api/alerts/channel             set a client's delivery channel
POST /api/alerts/run                 watch + deliver (scheduled entry point)
GET  /api/lookup?q=                  ONE search across every source
GET  /api/crosswalk?name=            all sources confirming one entity
GET  /api/sources-status             which credentialed sources are configured
GET  /api/audit                      audit trail of credentialed requests
POST /api/tabu/extract {gush,helka}  official registry extract (needs a key)
POST /api/tabu/import {datasets:[]}  background import: "tabu" (2.86M registry)
                                     and/or localities / contractors / appraisers
POST /api/tabu/stop                  cancel that import (staging table dropped)
GET  /api/tabu/status                progress of that import
GET  /api/scheduler                  per-job schedule, last runs, live progress
POST /api/scheduler/run {only,force}  run a cycle now (the "רענן הכל" button)
POST /api/scheduler/stop             cancel the running cycle between jobs
POST /api/sync/stop                  cancel a running sync on a page boundary
GET  /api/plans?<filters>            paged, sortable plan table
GET  /api/plan/<object_id>           one plan: fields, geometry, observed log
GET  /api/at?lat=&lon=&radius=       which plans cover / neighbour a point
GET  /api/geometries?<filters>       GeoJSON for the map (capped)
GET  /api/timeline?field=            plans per year
GET  /api/breakdown?by=              counts by jurisdiction / status / subtype
GET  /api/history                    status transitions PlanWatch observed
GET  /api/notifications              the outbox
POST /api/notifications/deliver      mark notifications delivered
GET  /api/subscriptions              watched parcels
POST /api/subscriptions              add one
POST /api/subscriptions/delete       remove one
POST /api/subscriptions/resolve      גוש/חלקה -> lat/lon for pending rows
POST /api/backfill                   re-match subscriptions against all plans
POST /api/sync                       start a sync in a background thread
GET  /api/sync/status                progress of the running/last sync
GET  /api/export?format=csv|geojson  download the current filter
"""

from __future__ import annotations

import argparse
import base64
import csv
import hmac
import importlib
import io
import json
import os
import re
import sqlite3
import sys
import statistics
import threading
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "legacy" / "tools"))
sys.path.insert(0, str(HERE / "services" / "external"))
sys.path.insert(0, str(HERE / "services" / "analytics"))
sys.path.insert(0, str(HERE / "services" / "enrichers"))
sys.path.insert(0, str(HERE / "ingestion" / "feeds"))
sys.path.insert(0, str(HERE / "ingestion" / "normalization"))

import accounts
import db
import report

from app.crm.api.routes import CRMAPI

from sources.base.adapter import BaseSourceAdapter, SourceStatus, SourceRunState
from sources.base.models import CanonicalListing
from sources.yad2.adapter import Yad2SourceAdapter
from sources.facebook.adapter import FacebookSourceAdapter
from sources.onmap.adapter import OnmapSourceAdapter
from jobs.scheduler import UnifiedScheduler, SchedulerConfig, JobResult
from jobs.health import HealthMonitor
from jobs.validation import ListingValidator

PAGE = HERE / "dashboard.html"

#: Where bots/whatsapp/bot.js writes its latest link QR (see that file's
#: WHATSAPP_QR_PATH, which must point at the same location).
WHATSAPP_QR_PATH = Path(os.environ.get("WHATSAPP_QR_PATH", HERE / "data" / "whatsapp-auth" / "qr.png"))
#: Same directory the bot passes to Baileys' useMultiFileAuthState - once
#: linked, that library writes creds.json here, which is the only reliable
#: "has this number been scanned in, ever" signal dashboard.py has (it never
#: talks to WhatsApp itself, only to the bot process's auth state on disk).
WHATSAPP_AUTH_DIR = Path(os.environ.get("WHATSAPP_AUTH_DIR", HERE / "data" / "whatsapp-auth"))
#: File-based signals to bots/whatsapp/bot.js, which polls for them - there is
#: no other channel between this process and that one. See that file's
#: DISCONNECT_FLAG_PATH/TEST_FLAG_PATH/TEST_RESULT_PATH, which must agree.
WHATSAPP_DISCONNECT_FLAG = WHATSAPP_AUTH_DIR / ".disconnect_requested"
WHATSAPP_TEST_FLAG = WHATSAPP_AUTH_DIR / ".test_requested"
WHATSAPP_TEST_RESULT_PATH = WHATSAPP_AUTH_DIR / "last_test.json"
YAD2_API_BASE = os.environ.get("PLANWATCH_YAD2_API", "http://127.0.0.1:8100").rstrip("/")

#: `local` (default) is this machine or a private network like Tailscale -
#: nobody untrusted can reach the port, so the server behaves exactly as it
#: always has. `render` is a public cloud deploy, and flips three defaults at
#: once: bind address, whether the in-process scheduler thread starts, and
#: whether requests need credentials at all. See DEPLOY.md for the reasoning
#: behind each - this constant is what turns that document into enforcement
#: instead of a checklist someone has to remember.
DEPLOY_MODE = os.environ.get("PLANWATCH_DEPLOY", "local").strip().lower()

#: "user:pass". Required when DEPLOY_MODE is "render" - main() refuses to
#: start without it, the same fail-fast treatment already given to a taken
#: port or an empty database below. Optional in "local" mode: set it there too
#: and the same gate applies, for anyone who wants defence in depth on a
#: private network without waiting for a reason to need it.
BASIC_AUTH = os.environ.get("PLANWATCH_BASIC_AUTH")

#: Routes the public webapp/bots use, split from the ~90 operator-only routes
#: gated by BASIC_AUTH above. PUBLIC_ROUTES need no credential at all (just
#: enough to log in); USER_ROUTES need a valid end-user bearer token
#: (`accounts.py`) instead of the operator's shared password. Everything not
#: listed here keeps today's BASIC_AUTH gate, unchanged.
PUBLIC_ROUTES = {"/api/health", "/api/user/login", "/api/public/bot-links"}
USER_ROUTES = {
    "/api/search", "/api/combined-sale-listings", "/api/deals",
    "/api/opportunities", "/api/listing-gallery", "/api/listing-contact",
    "/api/parcel", "/api/parcel-at", "/api/market", "/api/localities",
    "/api/user/logout", "/api/user/me",
    "/api/facebook/listings", "/api/facebook/harvest",
}


#: How many ads one Yad2 harvest pass aims to collect. This bounds the *job*,
#: never how much of the store a request may read - conflating the two is what
#: made a 61,478-row store report 18,000 listings on screen.
HARVEST_TARGET = 18000


def _json_list(value):
    """A stored JSON array as a Python list, or None. Never raises."""
    if not value:
        return None
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _annotate_live_rows(rows: list[dict], source: str = "Yad2") -> list[dict]:
    """Add the same analysis fields used by the scored legacy feed.

    Yad2 does not publish a benchmark price, so the benchmark is calculated
    from the currently imported Yad2 stock (neighbourhood first, city fallback).
    This keeps source-specific filtering useful while making its rows display
    the same score, quality and ad columns as every other source.

    ONMAP rows go through the same function under ``source="onmap"``: they
    carry the same fields (city / neighborhood / price / sqm / images_json),
    so scoring them separately would have meant a second copy of this logic
    that could drift. The benchmark is per call, which is what we want - an
    ONMAP asking price is compared to other ONMAP asking prices, not to Yad2's.
    """
    city_values: dict[str, list[float]] = {}
    neighborhood_values: dict[tuple[str, str], list[float]] = {}
    prepared = []
    for original in rows:
        row = dict(original)
        price = row.get("price")
        sqm = row.get("sqm")
        try:
            ppm = float(price) / float(sqm) if price is not None and sqm else None
        except (TypeError, ValueError, ZeroDivisionError):
            ppm = None
        city = str(row.get("city") or "")
        neighborhood = str(row.get("neighborhood") or "")
        if ppm is not None:
            city_values.setdefault(city, []).append(ppm)
            if neighborhood:
                neighborhood_values.setdefault((city, neighborhood), []).append(ppm)
        row["price_per_m2"] = round(ppm) if ppm is not None else None
        prepared.append((row, city, neighborhood, ppm))

    annotated = []
    for row, city, neighborhood, ppm in prepared:
        comparable = neighborhood_values.get((city, neighborhood), [])
        scope = "שכונה"
        if len(comparable) < 5:
            comparable = city_values.get(city, [])
            scope = "עיר"
        benchmark = statistics.median(comparable) if comparable else None
        discount = ((benchmark - ppm) / benchmark * 100
                     if benchmark and ppm else None)
        evidence = min(len(comparable), 20) / 2
        score = min(100, max(0, (discount or 0) * 2 + evidence))
        quality = "תקין" if ppm is not None else "חסר מחיר או שטח"
        if discount is not None and discount > 50:
            quality = "חריג — לאמת מחיר ופרטי מודעה"
            score = min(score, 60)
        row.update({
            "rank_score": round(score, 1),
            "confidence": "high" if len(comparable) >= 10 else "partial",
            "address": ", ".join(filter(None, (neighborhood, row.get("street")))),
            "discount_pct": round(discount, 1) if discount is not None else None,
            "benchmark_m2": round(benchmark) if benchmark else None,
            "benchmark_scope": scope,
            "comparable_count": len(comparable),
            "data_quality": quality,
            "source": source,
            # Derived here rather than only in the combined view: the live
            # endpoint reported its own brokerage_counts from a field nothing
            # had set yet, so both tallies were always zero.
            # A named agency is verified fact. Its absence is not evidence of a
            # private sale - see external_listings_view.brokerage_state.
            # Same rule as _combined_sale_listings and lead_intel. This used
            # to key off `agency` alone, which reported "0 ללא תיווך" on the
            # for-sale tab even after the feed began supplying ad_type.
            "brokerage": ("ללא תיווך" if row.get("ad_type") == "private"
                          else "תיווך" if (row.get("agency") or "").strip()
                          or row.get("ad_type") == "commercial"
                          else "לא ידוע"),
            # The whole gallery, for the lightbox. Every photo the harvest
            # stored, not just the cover: `images_json` holds the source's own
            # array (ONMAP publishes up to ~25 per listing, Yad2 up to 10).
            "images": _json_list(row.get("images_json")),
            # ONMAP stores its own ad URL; Yad2 rows have only a token, and the
            # item URL is derived from it.
            "url": row.get("url")
                   or (f"https://www.yad2.co.il/realestate/item/{row['token']}"
                       if row.get("token") else None),
        })
        annotated.append(row)
    return annotated


#: Every sort the sale tables offer, as key -> sort value. A row missing the
#: sorted field is pushed to the far end rather than dropped, so switching sort
#: never silently changes how many listings the screen reports.
_SALE_SORTS = {
    "score": lambda row: row.get("rank_score") if row.get("rank_score") is not None else -1,
    "discount": lambda row: row.get("discount_pct") if row.get("discount_pct") is not None else -10**9,
    "price_asc": lambda row: row.get("price") if row.get("price") is not None else 10**18,
    "price_desc": lambda row: row.get("price") if row.get("price") is not None else -1,
    "ppm_asc": lambda row: row.get("price_per_m2") if row.get("price_per_m2") is not None else 10**18,
    "ppm_desc": lambda row: row.get("price_per_m2") if row.get("price_per_m2") is not None else -1,
    "area_desc": lambda row: row.get("sqm") if row.get("sqm") is not None else -1,
    "rooms_desc": lambda row: row.get("rooms") if row.get("rooms") is not None else -1,
    # Size-adjusted valuation (listing_analytics). `buy`/`sell` rank by
    # score x confidence, never by score alone - a perfect number from one
    # component is not a better lead than a good one from five.
    "buy": lambda row: row.get("buy_rank") if row.get("buy_rank") is not None else -1,
    "sell": lambda row: row.get("sell_rank") if row.get("sell_rank") is not None else -1,
    "peer_gap": lambda row: (row.get("peer_gap_pct")
                             if row.get("peer_gap_pct") is not None else -10**9),
    "peer_gap_ils": lambda row: (row.get("peer_gap_ils")
                                 if row.get("peer_gap_ils") is not None else -10**18),
    "dom_desc": lambda row: row.get("dom_days") if row.get("dom_days") is not None else -1,
    # Planning upside is ranked by the score, not by raw density: an approved
    # plan at 8 units/dunam beats an unapproved one at 30.
    "planning": lambda row: (row.get("buy_components") or {}).get("planning_upside", -1),
    # Cheap against the flats in its own building - the tightest comparison
    # there is, when a building group exists at all.
    # Sorted on the *scored* component, not the raw gap, so the same
    # implausibility guard that keeps a broken row out of the buy ranking keeps
    # it off the top of this one too.
    "building_gap": lambda row: (row.get("buy_components") or {}).get("building_gap", -1),
    # Buildings where several owners are leaving at once.
    "building_pressure": lambda row: row.get("address_listings") or 0,
}
_SALE_SORTS_DESC = {"score", "discount", "price_desc", "ppm_desc",
                    "area_desc", "rooms_desc", "buy", "sell", "peer_gap",
                    "peer_gap_ils", "dom_desc", "planning", "building_gap",
                    "building_pressure"}


def _filter_sale_rows(annotated: list[dict], q: dict) -> list[dict]:
    """Apply the sale-table filters and sort to already-annotated rows.

    Shared by the Yad2 and ONMAP row sets. It used to live inline inside
    ``_live_sale_listings``, which is why ONMAP - added later - had no way to
    honour the same filters without a second, divergent copy.
    """
    # "locality" is the param name every other caller uses (external.listings,
    # /api/search, /api/opportunities, the webapp) - "city" was this function's
    # own name and nothing ever sent it, so Yad2/ONMAP rows (the majority of
    # the ~69k stock) were silently never filtered by locality at all while
    # the legacy feed (which reads `locality` directly) was. A search for
    # "ירושלים" returned Yad2/ONMAP rows from anywhere in the country.
    city = (q.get("city") or q.get("locality") or "").strip().lower()
    min_price = float(q["min_price"]) if q.get("min_price") else None
    max_price = float(q["max_price"]) if q.get("max_price") else None
    min_rooms = float(q["min_rooms"]) if q.get("min_rooms") else None
    max_rooms = float(q["max_rooms"]) if q.get("max_rooms") else None
    min_sqm = float(q["min_sqm"]) if q.get("min_sqm") else None
    max_sqm = float(q["max_sqm"]) if q.get("max_sqm") else None
    neighborhood = (q.get("neighborhood") or "").strip().lower()
    street = (q.get("street") or "").strip().lower()
    # House number is an exact match, not a substring: "3" must not pull in
    # 13, 23 and 30, which is the whole point of isolating one building.
    house_number = (q.get("house_number") or "").strip().lower()
    min_ppm = float(q["min_ppm"]) if q.get("min_ppm") else None
    max_ppm = float(q["max_ppm"]) if q.get("max_ppm") else None
    min_discount = float(q["min_discount"]) if q.get("min_discount") else None
    max_discount = float(q["max_discount"]) if q.get("max_discount") else None
    has_url = q.get("has_url") == "1"
    exclude_suspect = q.get("exclude_suspect") == "1"
    # Filters over the size-adjusted valuation. Each one is "at least this
    # much"; a row that could not be measured is excluded rather than passed,
    # because "show me everything at least 10% below the area" must not return
    # rows whose distance from the area is unknown.
    min_peer_gap = float(q["min_peer_gap"]) if q.get("min_peer_gap") else None
    max_peer_gap = float(q["max_peer_gap"]) if q.get("max_peer_gap") else None
    min_gap_ils = float(q["min_gap_ils"]) if q.get("min_gap_ils") else None
    min_buy = float(q["min_buy"]) if q.get("min_buy") else None
    min_sell = float(q["min_sell"]) if q.get("min_sell") else None
    min_dom = float(q["min_dom"]) if q.get("min_dom") else None
    max_dom = float(q["max_dom"]) if q.get("max_dom") else None
    min_plan_density = float(q["min_plan_density"]) if q.get("min_plan_density") else None
    min_address_listings = (int(q["min_address_listings"])
                            if q.get("min_address_listings") else None)
    private_cluster = q.get("private_cluster") == "1"
    owner_known = q.get("owner_known") == "1"
    pre_1980 = q.get("pre_1980") == "1"
    has_appraisal = q.get("has_appraisal") == "1"
    has_phone = q.get("has_phone") == "1"
    motivated = q.get("motivated") == "1"
    cheapest_in_building = q.get("cheapest_in_building") == "1"
    cut_only = q.get("price_cut") == "1"
    measured_only = q.get("measured") == "1"

    filtered = []
    for row in annotated:
        gap = row.get("peer_gap_pct")
        if min_peer_gap is not None and (gap is None or gap < min_peer_gap):
            continue
        if max_peer_gap is not None and (gap is None or gap > max_peer_gap):
            continue
        gap_ils = row.get("peer_gap_ils")
        if min_gap_ils is not None and (gap_ils is None or gap_ils < min_gap_ils):
            continue
        if min_buy is not None and (row.get("buy_rank") or 0) < min_buy:
            continue
        if min_sell is not None and (row.get("sell_rank") or 0) < min_sell:
            continue
        dom = row.get("dom_days")
        if min_dom is not None and (dom is None or dom < min_dom):
            continue
        if max_dom is not None and (dom is None or dom > max_dom):
            continue
        if min_plan_density is not None and (row.get("plan_density") or 0) < min_plan_density:
            continue
        if min_address_listings is not None and (row.get("address_listings") or 0) < min_address_listings:
            continue
        # More than one owner selling without a broker in the same building.
        if private_cluster and (row.get("address_private_listings") or 0) < 2:
            continue
        if cheapest_in_building and not row.get("address_cheapest"):
            continue
        # Agent-facing crossings against the people/registry side. These are
        # not price signals and are deliberately filters rather than score
        # components - knowing who holds rights does not make a flat cheap.
        if owner_known and not row.get("owner_count"):
            continue
        if pre_1980 and not row.get("building_pre_1980"):
            continue
        if has_appraisal and not row.get("appraisal_count"):
            continue
        if has_phone and not row.get("phone"):
            continue
        # Only ads whose own text states a reason to sell.
        if motivated and not row.get("motivation_flags"):
            continue
        # Only sellers who have already moved their price - the strongest
        # published evidence of motivation this dataset carries.
        if cut_only and not (row.get("price_change_pct") or 0) < 0:
            continue
        # Rows that actually have a like-for-like comparison behind them.
        if measured_only and gap is None:
            continue
        if city and city not in str(row.get("city") or "").lower():
            continue
        if neighborhood and neighborhood not in str(row.get("neighborhood") or "").lower():
            continue
        if street and street not in str(row.get("street") or "").lower():
            continue
        if house_number and house_number != str(row.get("house_number") or "").strip().lower():
            continue
        price = row.get("price")
        if min_price is not None and (price is None or float(price) < min_price):
            continue
        if max_price is not None and (price is None or float(price) > max_price):
            continue
        rooms = row.get("rooms")
        if min_rooms is not None and (rooms is None or float(rooms) < min_rooms):
            continue
        if max_rooms is not None and (rooms is None or float(rooms) > max_rooms):
            continue
        sqm = row.get("sqm")
        if min_sqm is not None and (sqm is None or float(sqm) < min_sqm):
            continue
        if max_sqm is not None and (sqm is None or float(sqm) > max_sqm):
            continue
        ppm = row.get("price_per_m2")
        if min_ppm is not None and (ppm is None or float(ppm) < min_ppm):
            continue
        if max_ppm is not None and (ppm is None or float(ppm) > max_ppm):
            continue
        discount = row.get("discount_pct")
        if min_discount is not None and (discount is None or float(discount) < min_discount):
            continue
        if max_discount is not None and (discount is None or float(discount) > max_discount):
            continue
        if has_url and not row.get("url"):
            continue
        if exclude_suspect and row.get("data_quality") != "תקין":
            continue
        filtered.append(row)

    sort = (q.get("sort") or "score").strip()
    filtered.sort(key=_SALE_SORTS.get(sort, _SALE_SORTS["score"]),
                  reverse=sort in _SALE_SORTS_DESC)
    return filtered


def _brokerage_counts(rows: list[dict]) -> dict:
    return {label: sum(1 for row in rows if row.get("brokerage") == label)
            for label in ("תיווך", "ללא תיווך", "לא ידוע")}


def _data_version() -> dict:
    """A cheap summary of what the stores hold, for the page to poll.

    The dashboard reloaded a table only when the operator pressed a button, so
    a harvest or contact sweep finishing left the screen showing figures from
    before it ran, with nothing on screen admitting it. This endpoint exists so
    the page can notice by itself.

    Everything here is a COUNT/MAX over an indexed column and nothing here
    annotates, scores or joins - the whole point is that it can be called every
    few seconds without dragging the ~10s whole-corpus pass in behind it.
    `busy` tells the page whether a background job is still writing, so it can
    poll briskly while work is happening and back off once it stops.
    """
    version: dict = {}
    busy = False
    # One connection, and no `ensure_schema`. The feeds' own `counts()` helpers
    # each open the 699 MB database and re-run the schema guard, which costs
    # ~0.9s a call however small the table is - five of them made this endpoint
    # take 4.2s, which is not something a page can poll. By the time anyone
    # polls a data version the tables exist; if they somehow do not, the
    # per-source try/except degrades to a constant rather than failing.
    conn = db.get_conn()
    try:
        for name, sql in (
            ("yad2", "SELECT COUNT(*) a, MAX(last_seen_at) b FROM yad2_listings"),
            ("onmap", """SELECT COUNT(*) a, MAX(created_at) b, COUNT(NULLIF(TRIM(phone), '')) c,
                                MAX(contact_checked_at) d
                           FROM onmap_listings WHERE delisted_at IS NULL"""),
            ("facebook", "SELECT COUNT(*) a, MAX(last_seen_at) b FROM facebook_listings"),
            # Every listing carries a monthly payment and a district momentum
            # score derived from these two tables, so a finance or CBS refresh
            # changes what the annotated rows should say while leaving the
            # listing stores untouched. Without them in the fingerprint the
            # cache would keep serving payments computed at the previous
            # month's rate - the same failure that hid 2,510 ONMAP phones
            # behind a key that only watched row counts.
            ("rates", "SELECT COUNT(*) a, MAX(period) b FROM boi_rates"),
            ("cbs", """SELECT COUNT(*) a, MAX(period) b FROM cbs_price_index
                        WHERE period_type = 'month'"""),
            # The amenity layer is display-only, but it is written into the
            # same cached rows, so a re-import must invalidate them too.
            ("amenities", "SELECT COUNT(*) a FROM transit_stops"),
            # The rent board moves independently of the sale board, and every
            # yield on screen is built from it.
            ("rentals", """SELECT COUNT(*) a, MAX(last_seen_at) b
                             FROM onmap_rentals WHERE delisted_at IS NULL"""),
        ):
            try:
                version[name] = list(conn.execute(sql).fetchone())
            except sqlite3.Error as exc:
                version[name] = ["error", type(exc).__name__]
    finally:
        conn.close()
    # Whether a job is running is in-process memory. The public `state()`
    # helpers would answer it too, but each one opens the database and runs the
    # schema guard to attach row counts this endpoint has already collected, so
    # the flag is read straight off the module dict instead.
    for module, names in (("yad2_feed", ("_STATE",)),
                          ("onmap_feed", ("_STATE", "_CONTACT_STATE")),
                          ("facebook_feed", ("_STATE",))):
        try:
            mod = importlib.import_module(module)
        except Exception:
            continue
        for name in names:
            snapshot = getattr(mod, name, None)
            if isinstance(snapshot, dict):
                busy = busy or bool(snapshot.get("running"))
    with _sync_lock:
        busy = busy or bool(_sync_state.get("running"))
    return {"version": json.dumps(version, ensure_ascii=False, sort_keys=True),
            "busy": busy, "detail": version}


#: Annotated row sets, keyed by a cheap fingerprint of the store behind them.
#:
#: Scoring is a whole-corpus operation - every row's benchmark comes from the
#: other rows - so it cannot be done per page. Over 61,478 Yad2 ads it costs
#: ~10 seconds, and the sale tab issues a request per page turn, per filter
#: change and per poll. The result depends only on what is in the store, so it
#: is computed once per harvest and reused until the store changes.
_ANNOTATED_CACHE: dict[str, tuple] = {}
_ANNOTATE_LOCK = threading.Lock()

#: Where each source records what it has previously asked for a listing. This
#: is the only evidence of a seller cutting their price, and it exists only
#: because a harvest ran yesterday and wrote down what the board said.
_PRICE_HISTORY = {"yad2": ("yad2_price_history", "token"),
                  "onmap": ("onmap_price_history", "id"),
                  "facebook": ("facebook_price_history", "listing_id")}


def _context_fingerprint() -> tuple:
    """What the *non-listing* layers hold, for the annotation cache key.

    Every cached row now carries a monthly payment from `boi_rates`, a district
    momentum score from `cbs_price_index`, distances from `transit_stops` and a
    gross yield from `onmap_rentals`. All four are written by background jobs
    that touch no *sale* listing table, so a
    key built from the listing stores alone would go on serving last month's
    mortgage rate forever - exactly the failure mode that kept 2,510 harvested
    phones invisible until the fingerprint learned to watch every column a
    background job writes.

    Three COUNT/MAX reads over indexed columns on one connection, and it runs
    once per cache check rather than per row.
    """
    conn = db.get_conn()
    try:
        out = []
        for sql in ("SELECT COUNT(*), MAX(period) FROM boi_rates",
                    "SELECT COUNT(*), MAX(period) FROM cbs_price_index",
                    "SELECT COUNT(*) FROM transit_stops",
                    "SELECT COUNT(*), MAX(last_seen_at) FROM onmap_rentals"):
            try:
                out.append(tuple(conn.execute(sql).fetchone()))
            except sqlite3.Error:
                # A layer that has never been imported is a stable "absent",
                # not an error that keeps rebuilding the cache every request.
                out.append(("missing",))
        return tuple(out)
    finally:
        conn.close()


def _annotated_cached(name: str, fingerprint, load, source: str) -> list[dict]:
    fingerprint = (fingerprint, _context_fingerprint())
    hit = _ANNOTATED_CACHE.get(name)
    if hit and hit[0] == fingerprint:
        return hit[1]
    with _ANNOTATE_LOCK:
        hit = _ANNOTATED_CACHE.get(name)
        if hit and hit[0] == fingerprint:
            return hit[1]
        annotated = _annotate_live_rows(load(), source=source)
        # The size-adjusted valuation runs inside the same cache entry: it fits
        # an elasticity and builds peer distributions over the whole row set, so
        # it is a whole-corpus operation for the same reason the scoring is, and
        # doing it per request would cost ~20s every page turn.
        import listing_analytics
        history = {}
        deflate = None
        table, key = _PRICE_HISTORY.get(name, (None, None))
        conn = db.get_conn()
        try:
            if table:
                try:
                    history = listing_analytics.price_history_index(conn, table, key)
                except sqlite3.Error:
                    history = {}
            # Price movement is scored in real terms. Built here, over the same
            # connection, because the multiplier is a property of the CBS
            # series and not of any one listing.
            deflate = listing_analytics.cpi_deflator(conn)
        finally:
            conn.close()
        # The planning layer runs first: `listing_analytics` reads its output as
        # a scoring component, and both are whole-corpus passes that belong in
        # the same cache entry.
        try:
            import listing_planning
            listing_planning.annotate(annotated)
        except Exception as exc:            # shapely absent, geometry unreadable
            print(f"planning layer unavailable: {type(exc).__name__}: {exc}")
        # Building concentration and seller portfolio also feed the scoring, so
        # they run before it and inside the same cache entry.
        # The parcel layer resolves a listing to gush/helka, which is the key
        # every other register in this database is filed under - rights-holder
        # hints, appraisal rulings, building age. Tel Aviv only; see
        # listing_parcel for why.
        try:
            import listing_parcel
            listing_parcel.annotate(annotated)
        except Exception as exc:
            print(f"parcel layer unavailable: {type(exc).__name__}: {exc}")
        # Cross-board contact linking runs *before* the seller grouping, so a
        # number borrowed from ONMAP also counts toward that advertiser's
        # portfolio. Yad2 publishes no phone at all, so without this step its
        # 61,478 rows contribute nothing to seller identity.
        try:
            import listing_contact_link
            conn = db.get_conn()
            try:
                index = listing_contact_link.phone_index(conn)
            finally:
                conn.close()
            linked = listing_contact_link.annotate(annotated, index)
            if linked["linked"]:
                print(f"contact link [{name}]: {linked['linked']} rows matched "
                      f"a phone published on another board")
        except Exception as exc:
            print(f"contact link unavailable: {type(exc).__name__}: {exc}")
        # Yad2 does not publish its own phone in the harvested rows. Any
        # number attached here came from another board's matching unit and must
        # not be shown as though it were published on this listing.
        if name.lower() == "yad2":
            for row in annotated:
                if row.get("phone_source") == "onmap":
                    row.pop("phone", None)
                    row.pop("phone_source", None)
                    row.pop("phone_basis", None)
        # District price momentum and the monthly cost of each asking price.
        # Runs before the scoring because `market_momentum` reads its output.
        try:
            import listing_market
            conn = db.get_conn()
            try:
                market_ctx = listing_market.context(conn)
            finally:
                conn.close()
            listing_market.annotate(annotated, market_ctx)
        except Exception as exc:
            print(f"market layer unavailable: {type(exc).__name__}: {exc}")
        # Transit and school proximity. Attached for display and filtering
        # only - see listing_market for the measurement that kept it out of
        # the scoring.
        try:
            import listing_amenities
            listing_amenities.annotate(annotated)
        except Exception as exc:
            print(f"amenity layer unavailable: {type(exc).__name__}: {exc}")
        # Rental yield. Runs before the scoring because `gross_yield` is a buy
        # component, and the rent index is a whole-corpus build of its own -
        # one elasticity fit and 15 city medians over the rent harvest.
        try:
            import listing_yield
            listing_yield.annotate(annotated)
        except Exception as exc:
            print(f"yield layer unavailable: {type(exc).__name__}: {exc}")
        import listing_seller
        listing_seller.annotate(annotated)
        try:
            listing_analytics.annotate(annotated, history=history, deflate=deflate)
        except Exception as exc:
            print(f"analytics layer unavailable: {type(exc).__name__}: {exc}")
        _ANNOTATED_CACHE[name] = (fingerprint, annotated)
        return annotated


def _live_sale_listings(q: dict) -> dict:
    """Sale listings from the *stored* Yad2 harvest - never from the network.

    Reading this endpoint used to trigger a scrape whenever the in-memory cache
    held fewer rows than the 18,000 target, which is to say almost always. The
    UI polls while ``loading`` is true, so the two fed each other and the table
    loaded forever. Rows now come from ``yad2_feed``'s SQLite store and the
    harvest is a job with a terminal state.

    One exception remains, and it is bounded: if the store is empty and no run
    has ever finished, the first request kicks off exactly one pass so a fresh
    install is not a dead screen. A failed run is *not* retried here - it is
    reported, and refreshing is the user's call.
    """
    import yad2_feed

    # `limit` bounds how much of the *store* is read. It used to be clamped to
    # 18,000 - the harvest job's target - so a store holding 61,478 rows served
    # 18,000 of them while the source picker, which counts the store directly,
    # advertised the real number. The two disagreed on screen. The harvest
    # target and the read size are separate concerns; only the former is capped.
    requested = int(q.get("limit") or 0)
    status = yad2_feed.state()
    stored = yad2_feed.counts()
    if not stored["total"] and status["status"] == "idle":
        status = yad2_feed.start(target=HARVEST_TARGET)
    fetching = bool(status.get("running"))
    fetched_at = status.get("finished_at")
    batches = status.get("pages_fetched", 0)
    fetch_error = status.get("error", "")

    # The ONMAP phone count is part of the *Yad2* fingerprint because Yad2 rows
    # now borrow contact numbers from ONMAP for the same unit. A cache keyed on
    # the Yad2 store alone would keep serving phone-less rows every time the
    # contact sweep found more - the same failure that hid 2,510 numbers behind
    # the ONMAP fingerprint until it learned to watch the columns being written.
    try:
        import onmap_feed
        linkable = onmap_feed.counts().get("enriched")
    except Exception:
        linkable = None
    annotated_rows = _annotated_cached(
        "yad2", (stored["total"], stored["last_seen"], linkable),
        lambda: yad2_feed.rows(), "Yad2")
    if requested > 0:
        annotated_rows = annotated_rows[:requested]
    filtered = _filter_sale_rows(annotated_rows, q)
    brokerage_counts = _brokerage_counts(filtered)
    rows = annotated_rows
    return {"source": "יד 2 דרך ממשק מקומי", "source_url": "https://www.yad2.co.il/realestate/forsale",
            "total": len(filtered), "available": len(rows), "batches": batches,
            "cached_at": fetched_at, "rows": filtered, "loading": fetching,
            # What a *harvest pass* aims for. The UI renders this as the
            # denominator of "טוען יד 2: 3,000 / …", so it has to stay the job's
            # target: reporting the number of rows read instead made a running
            # harvest look like it was chasing 61,478 when it aims for 18,000.
            "target": status.get("target") or HARVEST_TARGET,
            "read_limit": requested or len(rows), "error": fetch_error,
            "brokerage_counts": brokerage_counts,
            # The harvest's terminal state, so the UI can say "done", "failed"
            # or "never run" instead of spinning on an ambiguous boolean.
            "harvest": {k: status.get(k) for k in
                        ("status", "pages_fetched", "pages_failed", "rows_new",
                         "rows_stored", "started_at", "finished_at", "stale")}}


def _onmap_sale_listings(q: dict) -> dict:
    """Sale listings from the stored ONMAP harvest (`onmap_feed`).

    ONMAP was wired into the source picker - which is why the dropdown offered
    "על המפה (2,839)" - but never into the rows, so selecting it returned an
    empty table and "all sources" silently excluded every one of its listings.

    Its stored columns line up with the Yad2 ones except for two names, so the
    rows are renamed here and then run through exactly the same annotation,
    filtering and sorting path. That also means each ONMAP listing arrives with
    its full `images` array - the source publishes up to ~25 photos per ad,
    which is the richest gallery of any feed we hold.
    """
    import onmap_feed

    def load():
        rows = []
        for item in onmap_feed.rows():
            row = dict(item)
            # `area_sqm` and `id` are ONMAP's names for what the shared scoring
            # and dedupe code calls `sqm` and `token`.
            row["sqm"] = row.pop("area_sqm", None)
            row["token"] = row.get("id")
            rows.append(row)
        return rows

    requested = int(q.get("limit") or 0)
    counts = onmap_feed.counts()
    annotated = _annotated_cached(
        "onmap", (counts.get("total"), counts.get("last_created"),
                  counts.get("enriched"), counts.get("last_contact")),
        load, "onmap")
    if requested > 0:
        annotated = annotated[:requested]
    rows = annotated
    filtered = _filter_sale_rows(annotated, q)
    return {"source": "onmap", "source_url": "https://www.onmap.co.il",
            "total": len(filtered), "available": len(rows), "rows": filtered,
            "stored": counts.get("total", 0),
            "last_created": counts.get("last_created"),
            "brokerage_counts": _brokerage_counts(filtered)}


def _facebook_sale_listings(q: dict) -> dict:
    """Sale listings from the stored Facebook Marketplace harvest."""
    import facebook_feed

    def load():
        rows = []
        for item in facebook_feed.rows():
            row = dict(item)
            row["token"] = row.get("listing_id")
            row["price"] = row.get("price_value")
            row["city"] = row.get("city_name")
            row["neighborhood"] = row.get("location")
            row["images_json"] = json.dumps([row.get("image_url")]) if row.get("image_url") else None
            row["ad_type"] = "private"
            row["agency"] = None
            rows.append(row)
        return rows

    requested = int(q.get("limit") or 0)
    fb_counts = facebook_feed.counts()
    annotated = _annotated_cached(
        "facebook", (fb_counts.get("total"), fb_counts.get("last_seen")),
        load, "Facebook")
    if requested > 0:
        annotated = annotated[:requested]
    rows = annotated
    filtered = _filter_sale_rows(annotated, q)
    return {"source": "Facebook Marketplace", "source_url": "https://www.facebook.com/marketplace",
            "total": len(filtered), "available": len(rows), "rows": filtered,
            "stored": fb_counts.get("total", 0),
            "last_seen": fb_counts.get("last_seen"),
            "brokerage_counts": _brokerage_counts(filtered)}


#: Compiled once: the dedupe key is built for every row of every source on
#: every request, which is ~69,000 substitutions per page turn.
_DEDUPE_STRIP = re.compile(r"[^0-9a-zא-ת]+")


def _norm_key(value) -> str:
    return _DEDUPE_STRIP.sub("", str(value or "").lower())


def _listing_dedupe_keys(row: dict, *, live: bool = False) -> list[tuple]:
    """Every identity one sale listing can be recognised by.

    This returns a *list* because a listing has two independent identities and
    checking only the first is what made cross-source deduplication a no-op.
    The old version returned the publisher's ID and stopped there. Every Yad2
    and ONMAP row has an ID, so the property fingerprint below was reached only
    by rows that had neither - which is to say almost none. The screen said
    "כל המקורות מאוחדים ללא כפילויות · 0 כפילויות שהוסרו" while 248 ONMAP ads
    were the same flat as a Yad2 ad, matching to the shekel on price, rooms and
    m². The count was honest about the code and wrong about the world.

    1. **The publisher's ID, scoped to its source.** Two feeds' ID spaces are
       unrelated - a Yad2 token and an ONMAP id are both short alphanumerics -
       so an unscoped ID key risks collapsing two unrelated flats on a
       coincidence. It catches the same ad seen twice within one feed.
    2. **An exact property fingerprint**, which is what actually catches the
       same flat published on two boards. Only built when it is specific
       enough to mean something: street, price, rooms and area must all be
       present. A row missing them keeps its ID identity alone rather than
       matching every other under-described row in its city.

    The fingerprint deduplicates **across sources only** - see `_merge_source`.
    Within one board an identical fingerprint is not proof of a duplicate: a
    developer can list seven identical units in one project at one price, and
    the same flat is often advertised by two agencies, which is two leads to
    call rather than one row to hide. Between boards it is proof enough: the
    same street, the same price to the shekel, the same rooms and the same m²
    is one property published twice.
    """
    # Memoised on the row. Keys are built from stored fields that do not change
    # between requests, and rebuilding them for 69,305 rows on every page turn
    # cost 303,000 regex substitutions and five seconds - more than the rest of
    # the request put together.
    cached = row.get("_dedupe_keys")
    if cached is not None:
        return cached

    keys = []
    token = row.get("token") if live else row.get("external_id")
    if token:
        keys.append(("id", _norm_key(row.get("source")), _norm_key(token)))

    city = row.get("city") if live else row.get("locality")
    street = row.get("street") or row.get("address")
    price = row.get("price")
    rooms = row.get("rooms")
    area = row.get("sqm") if live else row.get("area_m2")
    if street and price and rooms and area:
        keys.append(("property", _norm_key(city), _norm_key(row.get("neighborhood")),
                     _norm_key(street), str(price), str(rooms), str(area)))
    row["_dedupe_keys"] = keys
    return keys


def _combined_sale_listings(q: dict) -> dict:
    """Merge every sale feed we hold: AD/Madlan/Komo, Yad2 and ONMAP.

    Existing publisher rows are retained as the canonical record when a
    matching Yad2 or ONMAP fingerprint is found. The source databases are never
    modified; this is a presentation-time union with duplicate accounting.

    Three faults lived here, and each one hid the next:

    * Yad2 was read through a hard ``limit=18000`` while its store held 61,478
      ads, so the table served under a third of the stock it advertised.
    * ONMAP was counted for the source picker but never queried, so its 2,839
      listings appeared in the dropdown and nowhere else.
    * ``only_legacy`` swallowed every non-Yad2 filter value, so picking
      "על המפה" asked the AD/Madlan/Komo database for a source it has
      never heard of, and got an empty table back.

    Paging happens here now rather than in the browser. The union is ~69,000
    rows carrying up to 25 image URLs each; serialising all of it on every poll
    is tens of megabytes of JSON to render the 80 rows actually on screen.
    """
    import external_listings_view as external

    source_filter = (q.get("source") or "").strip()
    is_yad2 = bool(source_filter
                   and re.search(r"yad2|יד\s*2", source_filter, re.I))
    is_onmap = bool(source_filter
                    and re.fullmatch(r"onmap|על\s*המפה",
                                     source_filter, re.I))
    is_facebook = bool(source_filter
                       and re.search(r"facebook|פייסבוק", source_filter, re.I))
    # No filter means all three. Any other value selects exactly one row set.
    want_legacy = not source_filter or not (is_yad2 or is_onmap or is_facebook)
    want_live = not source_filter or is_yad2
    want_onmap = not source_filter or is_onmap
    want_facebook = not source_filter or is_facebook

    # The size-adjusted valuation is computed by `listing_analytics` over the
    # Yad2 and ONMAP row sets. The AD/Madlan/Komo feed is scored by its own
    # older path and carries none of these fields, so it cannot answer these
    # questions - and returning it unfiltered would put 4,988 rows that do not
    # meet the filter on a screen the user narrowed on purpose. It is dropped
    # instead, and `unscored_sources` says so rather than leaving a gap.
    analytics_filters = [k for k in ("min_peer_gap", "max_peer_gap", "min_gap_ils",
                                     "min_buy", "min_sell", "min_dom", "max_dom",
                                     "min_plan_density", "min_address_listings",
                                     "private_cluster", "cheapest_in_building",
                                     "owner_known", "pre_1980", "has_appraisal",
                                     "has_phone", "motivated",
                                     "price_cut", "measured")
                         if q.get(k) not in (None, "", "0")]
    if analytics_filters and want_legacy:
        want_legacy = False
        legacy_excluded = analytics_filters
    else:
        legacy_excluded = []

    legacy = {"rows": [], "total": 0, "sources": []}
    if want_legacy:
        legacy = external.listings(
            locality=q.get("locality", ""), neighborhood=q.get("neighborhood", ""),
            street=q.get("street", ""),
            # A source filter naming Yad2 or ONMAP must not be forwarded: this
            # database holds neither, so passing it through returns nothing for
            # a filter that was never meant for it.
            source="" if (is_yad2 or is_onmap) else q.get("source", ""),
            min_price=as_float(q.get("min_price"), "min_price"),
            max_price=as_float(q.get("max_price"), "max_price"),
            min_area=as_float(q.get("min_area"), "min_area"),
            max_area=as_float(q.get("max_area"), "max_area"),
            min_rooms=as_float(q.get("min_rooms"), "min_rooms"),
            max_rooms=as_float(q.get("max_rooms"), "max_rooms"),
            min_ppm=as_float(q.get("min_ppm"), "min_ppm"),
            max_ppm=as_float(q.get("max_ppm"), "max_ppm"),
            date_from=(q.get("date_from") or "").strip(),
            date_to=(q.get("date_to") or "").strip(),
            min_discount_pct=as_float(q.get("min_discount"), "min_discount"),
            max_discount_pct=as_float(q.get("max_discount"), "max_discount"),
            has_url=q.get("has_url") == "1",
            exclude_suspect=q.get("exclude_suspect") == "1",
            sort=(q.get("sort") or "score").strip(), limit=10000, offset=0)

    # `limit`/`offset` page the *result*. The row sets themselves are read in
    # full, so the totals and the dedupe still see the whole stock.
    row_query = {k: v for k, v in q.items() if k not in ("limit", "offset")}
    live = {"rows": [], "total": 0, "batches": 0}
    if want_live:
        live = _live_sale_listings(row_query)
    onmap = {"rows": [], "total": 0}
    if want_onmap:
        onmap = _onmap_sale_listings(row_query)
    facebook = {"rows": [], "total": 0}
    if want_facebook:
        facebook = _facebook_sale_listings(row_query)

    legacy_rows = list(legacy.get("rows") or [])
    # The AD/Madlan/Komo rows sit outside the peer-scoring path on purpose -
    # they carry none of its inputs. The monthly payment and the district
    # momentum need neither a peer index nor coordinates, only a price and a
    # town, so they *can* be answered here and are: a price column that shows
    # a payment on 64,000 rows and a blank on the other 4,988 reads as missing
    # data rather than as a different kind of row.
    if legacy_rows:
        try:
            import listing_market
            conn = db.get_conn()
            try:
                market_ctx = listing_market.context(conn)
            finally:
                conn.close()
            for row in legacy_rows:
                # This feed names the town `locality` and the area `area_m2`;
                # both layers key on `city` and `sqm`.
                row.setdefault("city", row.get("locality"))
                row.setdefault("sqm", row.get("area_m2"))
            listing_market.annotate(legacy_rows, market_ctx)
            # A yield cannot be produced for these rows and the row should say
            # so. This feed publishes **no property type on any row**, and the
            # rent benchmark is residential-only - so with no way to tell a
            # flat from a plot, the refusal is right. Running the layer anyway
            # is what turns a blank column into a stated reason, which is the
            # difference between "we did not look" and "this source cannot
            # answer".
            import listing_yield
            listing_yield.annotate(legacy_rows)
        except Exception as exc:
            print(f"market layer unavailable for legacy rows: "
                  f"{type(exc).__name__}: {exc}")
    # Phones already learned from an ad page (the gallery fetch reads both in
    # one request) are joined back onto the rows here, so a number found once
    # shows in the table from then on instead of only inside the lightbox.
    if legacy_rows:
        try:
            import listing_gallery
            known = listing_gallery.known_phones(
                [row.get("url") for row in legacy_rows if row.get("url")])
            for row in legacy_rows:
                phone = known.get(row.get("url"))
                if phone:
                    row["phone"] = phone
        except (ImportError, sqlite3.Error):
            pass
    brokerage_filter = (q.get("brokerage") or "").strip()
    for row in legacy_rows:
        # external.listings already classified these from the row's own
        # evidence, including "לא ידוע" where the source published
        # nothing. The previous default here overwrote that with
        # "ללא תיווך" for every row lacking an agency field - which
        # is most of them - and so told the user ~4,900 listings had no broker
        # on the strength of a field that was never populated. Only fill in a
        # state that is genuinely absent.
        row["brokerage"] = row.get("brokerage") or "לא ידוע"
    if brokerage_filter:
        legacy_rows = [row for row in legacy_rows
                       if row.get("brokerage") == brokerage_filter]

    # Legacy rows are canonical, so they seed the seen-set. Yad2 is deduped
    # against them, and ONMAP against both.
    #
    # `seen` maps each identity to the source that claimed it, because the two
    # kinds of identity are not equally conclusive. An ID repeat is always a
    # duplicate. A property fingerprint repeat is a duplicate only when it
    # comes from a *different* board - within one board, seven identical units
    # in a new project share a fingerprint, and so do two agencies advertising
    # the same flat, which is two leads rather than one row to hide.
    seen: dict[tuple, str] = {}

    def claim(row, keys, source):
        for key in keys:
            seen.setdefault(key, source)

    for row in legacy_rows:
        claim(row, _listing_dedupe_keys(row), str(row.get("source") or "legacy"))

    duplicates = 0
    dup_by_source: dict[str, int] = {}

    def merge(rows, source):
        nonlocal duplicates
        kept = []
        for row in rows or []:
            if brokerage_filter and row.get("brokerage") != brokerage_filter:
                continue
            keys = _listing_dedupe_keys(row, live=True)
            is_dup = any(
                key in seen and (key[0] == "id" or seen[key] != source)
                for key in keys)
            if is_dup:
                duplicates += 1
                dup_by_source[source] = dup_by_source.get(source, 0) + 1
                continue
            claim(row, keys, source)
            kept.append(row)
        return kept

    live_rows = merge(live.get("rows"), "Yad2")
    onmap_rows = merge(onmap.get("rows"), "onmap")
    facebook_rows = merge(facebook.get("rows"), "Facebook")

    source_facets = [dict(item) for item in (legacy.get("sources") or [])]
    if not any(re.search(r"yad2|יד\s*2", str(item.get("source") or ""), re.I)
               for item in source_facets):
        import yad2_feed
        stored = yad2_feed.counts()
        source_facets.append({"source": "Yad2", "listings": stored["total"],
                              "last_fetched_at": stored["last_seen"]})
    if not any((item.get("source") or "").lower() == "onmap"
               for item in source_facets):
        import onmap_feed
        on = onmap_feed.counts()
        source_facets.append({"source": "onmap", "listings": on["total"],
                              "last_fetched_at": on["last_created"]})
    if not any((item.get("source") or "").lower() == "facebook"
               for item in source_facets):
        import facebook_feed
        fb = facebook_feed.counts()
        source_facets.append({"source": "Facebook Marketplace", "listings": fb["total"],
                              "last_fetched_at": fb["last_seen"]})

    limit = max(1, min(int(q.get("limit") or 80), 500))
    offset = max(0, int(q.get("offset") or 0))

    def page(rows):
        return rows[offset:offset + limit]

    return {
        "legacy_rows": page(legacy_rows), "live_rows": page(live_rows),
        "onmap_rows": page(onmap_rows), "facebook_rows": page(facebook_rows),
        "legacy_total": len(legacy_rows), "live_total": len(live_rows),
        "onmap_total": len(onmap_rows), "facebook_total": len(facebook_rows),
        "total": len(legacy_rows) + len(live_rows) + len(onmap_rows) + len(facebook_rows),
        "total_before_dedupe": (len(legacy_rows) + len(live.get("rows") or [])
                                + len(onmap.get("rows") or [])
                                + len(facebook.get("rows") or [])),
        "duplicates_removed": duplicates,
        # Per source, so the screen can say *why* ONMAP shows fewer rows here
        # than its own store holds instead of looking like it lost listings.
        "duplicates_by_source": dup_by_source,
        "limit": limit, "offset": offset,
        # Sources a filter could not be applied to, so the screen can explain
        # their absence instead of the user wondering where they went.
        "legacy_excluded_by": legacy_excluded,
        # The server pages the rows now, so the browser must not slice again.
        "paged": True,
        "batches": live.get("batches", 0), "sources": source_facets,
        "source_url": live.get("source_url"),
        "loading": bool(live.get("loading")),
        "target": live.get("target", HARVEST_TARGET),
        "brokerage_counts": live.get("brokerage_counts", {}),
        "fetch_error": live.get("error", ""),
        "harvest": live.get("harvest") or {},
    }


def _finance_counts() -> dict:
    """Rate-table counts, or zeroes if the import has never run.

    Each of these opens and closes its own connection because `_live_sources`
    is a status page: one source failing to answer must not take the list of
    the other twelve with it.
    """
    try:
        import finance_client
        conn = db.get_conn()
        try:
            return finance_client.counts(conn)
        finally:
            conn.close()
    except Exception:
        return {}


def _finance_status() -> str:
    return "connected" if _finance_counts().get("observations") else "not imported"


def _finance_detail() -> str:
    try:
        import finance_client
        conn = db.get_conn()
        try:
            rate = finance_client.mortgage_rate(conn)
        finally:
            conn.close()
    except Exception:
        return "run the finance job"
    if not rate:
        return "run the finance job"
    return f"משכנתא {rate['rate']}% · {rate['period']}"


def _rental_counts() -> dict:
    try:
        import onmap_rent
        return onmap_rent.counts()
    except Exception:
        return {}


def _rental_status() -> str:
    return "connected" if _rental_counts().get("usable") else "not imported"


def _rental_detail() -> str:
    counts = _rental_counts()
    if not counts.get("usable"):
        return "run the rentals job"
    dropped = (counts.get("total") or 0) - (counts.get("usable") or 0)
    return (f'{counts["usable"]} השכרות ב-{counts.get("cities", 0)} ערים'
            + (f' · {dropped} מחוץ לטווח שכ״ד סביר' if dropped else ""))


def _amenity_counts() -> dict:
    try:
        import amenity_client
        conn = db.get_conn()
        try:
            return amenity_client.counts(conn)
        finally:
            conn.close()
    except Exception:
        return {}


def _amenity_status() -> str:
    return "connected" if _amenity_counts().get("transit") else "not imported"


def _amenity_detail() -> str:
    counts = _amenity_counts()
    if not counts.get("transit"):
        return "run the amenities job"
    tiers = counts.get("tiers", {})
    rail = sum(tiers.get(t, 0) for t in ("rail", "light_rail", "brt", "hub"))
    return (f'{rail} תחנות רכבת/רק״ל/מטרונית · '
            f'{tiers.get("bus", 0)} תחנות אוטובוס · '
            f'{counts.get("schools", 0)} מוסדות חינוך')


def _live_sources() -> dict:
    import facebook_feed
    import onmap_feed
    import yad2_feed
    yad2 = yad2_feed.counts()
    onmap = onmap_feed.counts()
    onmap_contact = onmap_feed.contact_state()
    fb = facebook_feed.counts()
    return {"sources": [
        {"name": "Yad2 מכירה", "status": "connected",
         "endpoint": f"{YAD2_API_BASE}{yad2_feed.API_PATH}",
         "records": yad2["total"],
         "detail": f'{yad2["with_agency"]} with agency'},
        {"name": "PlanWatch / SQLite", "status": "connected", "endpoint": "/api/*",
         "records": None, "detail": "local cache + joins"},
        {"name": "בנק ישראל — ריבית ומשכנתאות", "status": _finance_status(),
         "endpoint": "edge.boi.gov.il/sdmx · boi.org.il/PublicApi",
         "records": _finance_counts().get("observations"),
         "detail": _finance_detail()},
        {"name": "על המפה — שכירות", "status": _rental_status(),
         "endpoint": "phoenix.onmap.co.il · option=rent",
         "records": _rental_counts().get("usable"),
         "detail": _rental_detail()},
        {"name": "תחבורה ציבורית ומוסדות חינוך", "status": _amenity_status(),
         "endpoint": "data.gov.il · bus_stops · coordinates",
         "records": _amenity_counts().get("transit"),
         "detail": _amenity_detail()},
        {"name": "data.gov.il", "status": "connected", "endpoint": "open data",
         "records": None, "detail": "open registries"},
        {"name": "GovMap", "status": "key required", "endpoint": "PLANWATCH_GOVMAP_KEY",
         "records": None, "detail": "parcel geometry lookup"},
        {"name": "הלמ״ס", "status": "connected", "endpoint": "market_client",
         "records": None, "detail": "market indices"},
        {"name": "רמ״י / מכרזים", "status": "connected", "endpoint": "tenders_client",
         "records": None, "detail": "open tenders"},
        {"name": "GIS עירוני", "status": "partial", "endpoint": "municipal_client",
         "records": None, "detail": "local permit/age layers"},
        {"name": "Google Places", "status": "key required", "endpoint": "GOOGLE_MAPS_API_KEY",
         "records": None, "detail": "business lookup"},
        {"name": "Schuna", "status": "partner key required", "endpoint": "/api/v1/intelligence",
         "records": None, "detail": "partner intel"},
        {"name": "ONMAP", "status": "not verified", "endpoint": "no public API found",
         "records": onmap["total"],
         "detail": f'{onmap["enriched"]} phones · {onmap_contact["already_checked"]} checked'},
        {"name": "Facebook Marketplace", "status": "connected",
         "endpoint": "playwright + storage_state",
         "records": fb["total"],
         "detail": f'{fb["total"]} listings · last seen: {fb.get("last_seen", "never")}'},
    ]}

#: Cap on polygons handed to the map in one request - the full 36,700 would be
#: ~110 MB of GeoJSON and would hang the browser.
MAP_FEATURE_CAP = 1200
#: A feature-count cap alone is not enough: 1,200 *large* plans (e.g. national
#: master plans) measured 81 MB, which also hangs the browser. Whichever limit
#: is reached first wins, and the response says it was truncated.
MAP_BYTE_BUDGET = 8_000_000

SORTABLE = {
    "object_id", "pl_number", "pl_name", "jurisdiction_name", "county_name",
    "entity_subtype", "station", "short_status", "area_dunam", "housing_units",
    "last_update", "depositing_date",
}


class BadRequest(ValueError):
    """Caller sent something unusable - answer 400, not 500."""


def as_int(value, name: str, default=None, low=None, high=None):
    """Parse an integer query param, or raise BadRequest with a clear message."""
    if value in (None, ""):
        return default
    try:
        out = int(float(value))
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number (got {value!r})") from None
    if low is not None:
        out = max(low, out)
    if high is not None:
        out = min(high, out)
    return out


def as_float(value, name: str, default=None):
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number (got {value!r})") from None


def required_float(params: dict, name: str) -> float:
    if name not in params or params[name] == "":
        raise BadRequest(f"{name} is required")
    return as_float(params[name], name)

# ---------------------------------------------------------------- sync worker

_sync_state: dict = {
    "running": False, "mode": None, "lines": [], "summary": None,
    "error": None, "started_at": None, "stopping": False,
}
_sync_lock = threading.Lock()
#: Set by POST /api/sync/stop; iter_plans checks it between pages.
_sync_cancel = threading.Event()


class _LineCollector(io.TextIOBase):
    """Captures sync.py's progress prints so the browser can poll them."""

    def write(self, text: str) -> int:
        for chunk in text.replace("\r", "\n").splitlines():
            chunk = chunk.strip()
            if chunk:
                with _sync_lock:
                    _sync_state["lines"].append(chunk)
                    del _sync_state["lines"][:-400]  # keep the tail bounded
        return len(text)


_tabu_state: dict = {"running": False, "rows": 0, "done": None, "error": None,
                     "cancelled": False, "what": None}
_tabu_lock = threading.Lock()
#: Set by POST /api/tabu/stop; the importer checks it on page boundaries.
_tabu_cancel = threading.Event()


def _run_tabu_import(datasets: list[str] | None = None) -> None:
    """
    Background import. `datasets` None/["tabu"] = the land registry; otherwise
    keys from open_data_client (localities / contractors / appraisers).
    """
    import appraisal_client as ac
    import land_registry_client as lr
    import market_client as mk
    import open_data_client as od
    import urban_renewal_client as ur

    wanted = datasets or ["tabu"]
    _tabu_cancel.clear()
    with _tabu_lock:
        _tabu_state.update(running=True, rows=0, done=None, error=None,
                           cancelled=False, what=", ".join(wanted))
    conn = db.get_conn()
    try:
        def progress(*args):
            # lr.import_all sends (n); od.refresh sends (key, n)
            n = args[-1]
            with _tabu_lock:
                _tabu_state["rows"] = n
                if len(args) == 2:
                    _tabu_state["what"] = args[0]

        total = 0
        for key in wanted:
            if _tabu_cancel.is_set():
                break
            if key == "tabu":
                total += lr.import_all(conn, progress=progress,
                                       should_stop=_tabu_cancel.is_set)
            elif key == "renewal":
                total += ur.import_all(conn, progress=progress,
                                       should_stop=_tabu_cancel.is_set)
            elif key == "market":
                total += mk.import_all(conn, progress=progress,
                                       should_stop=_tabu_cancel.is_set)
            elif key == "registry":
                total += ac.import_all(conn, progress=progress,
                                       should_stop=_tabu_cancel.is_set)
            elif key == "index":
                import entity_index as ei
                res = ei.rebuild(conn, progress=progress,
                                 should_stop=_tabu_cancel.is_set)
                total += res.get("total", 0)
            elif key == "owners":
                import owners_client as oc
                res = oc.rebuild(conn, progress=progress,
                                 should_stop=_tabu_cancel.is_set)
                total += res.get("total", 0)
            elif key == "municipal":
                import municipal_client as mn
                total += mn.import_all(conn, progress=progress,
                                       should_stop=_tabu_cancel.is_set)
            elif key in ("muni_parcels", "muni_permits", "muni_dangerous",
                         "muni_buildings"):
                import municipal_client as mn
                total += mn.IMPORTERS[key](conn, progress=progress,
                                           should_stop=_tabu_cancel.is_set)
            elif key == "tenders":
                import tenders_client as tc
                total += tc.import_all(conn, progress=progress,
                                       should_stop=_tabu_cancel.is_set)
            elif key == "documents":
                import document_client as dc
                stats = dc.fetch_batch(conn, limit=400, progress=progress,
                                       should_stop=_tabu_cancel.is_set)
                total += stats.get("attempted", 0)
            elif key in ac.IMPORTERS:
                total += ac.IMPORTERS[key](conn, progress=progress,
                                           should_stop=_tabu_cancel.is_set)
            elif key in mk.IMPORTERS:
                total += mk.IMPORTERS[key](conn, progress=progress,
                                           should_stop=_tabu_cancel.is_set)
            else:
                total += od.refresh(conn, key, progress=progress,
                                    should_stop=_tabu_cancel.is_set)
        with _tabu_lock:
            _tabu_state.update(done=total, rows=total,
                               cancelled=_tabu_cancel.is_set())
    except Exception:
        with _tabu_lock:
            _tabu_state["error"] = traceback.format_exc(limit=4)
    finally:
        conn.close()
        with _tabu_lock:
            _tabu_state["running"] = False


def _run_sync(mode: str, since: str | None, notify_bootstrap: bool) -> None:
    import contextlib

    import sync

    _sync_cancel.clear()
    with _sync_lock:
        _sync_state.update(running=True, mode=mode, lines=[], summary=None,
                           error=None, started_at=db.now_iso(), stopping=False)
    try:
        with contextlib.redirect_stdout(_LineCollector()):
            summary = sync.sync_plans(mode=mode, since=since,
                                      notify_bootstrap=notify_bootstrap,
                                      should_stop=_sync_cancel.is_set)
        with _sync_lock:
            _sync_state["summary"] = summary
    except Exception:
        with _sync_lock:
            _sync_state["error"] = traceback.format_exc(limit=4)
    finally:
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["stopping"] = False


# -------------------------------------------------------- scheduler worker

_sched_state: dict = {
    "running": False, "job": None, "started_at": None, "finished_at": None,
    "result": None, "error": None, "trigger": None, "auto": False,
    "next_run_at": None, "cycles": 0,
}
_sched_lock = threading.Lock()
#: Set by POST /api/scheduler/stop; every job checks it between steps.
_sched_cancel = threading.Event()
#: Signalled to make the sleeping auto-loop wake and run a cycle immediately.
_sched_wake = threading.Event()
#: Set once at startup so the auto thread is never started twice.
_sched_thread: threading.Thread | None = None

#: The auto cycle interval. Each job additionally honours its own
#: `scheduler.INTERVAL_HOURS`, so a 12-hour tick does not re-pull a weekly
#: reference feed - it just gives every job a chance to become due.
AUTO_CYCLE_HOURS = 12


def _sched_run_cycle(only=None, force=False, trigger="manual") -> None:
    """Run one scheduler cycle in this thread, publishing progress as it goes."""
    import scheduler

    _sched_cancel.clear()
    with _sched_lock:
        if _sched_state["running"]:
            return
        _sched_state.update(running=True, job=None, started_at=db.now_iso(),
                            finished_at=None, result=None, error=None,
                            trigger=trigger)
    conn = db.get_conn()
    try:
        # Report the job in flight so a 40-minute cycle is observable rather
        # than looking hung. `run_cycle` itself only returns at the very end.
        jobs = [j for j in scheduler.ORDER if not only or j in only]
        out = {"jobs": [], "ok": 0, "failed": 0, "skipped": 0}
        for job in jobs:
            if _sched_cancel.is_set():
                out["stopped"] = True
                break
            with _sched_lock:
                _sched_state["job"] = job
            result = scheduler.run_job(conn, job, force=force,
                                       should_stop=_sched_cancel.is_set)
            out["jobs"].append(result)
            if result.get("skipped"):
                out["skipped"] += 1
            elif result.get("ok"):
                out["ok"] += 1
            else:
                out["failed"] += 1
        with _sched_lock:
            _sched_state["result"] = out
            _sched_state["cycles"] += 1
    except Exception:
        with _sched_lock:
            _sched_state["error"] = traceback.format_exc(limit=4)
    finally:
        conn.close()
        with _sched_lock:
            _sched_state.update(running=False, job=None,
                                finished_at=db.now_iso())


def _sched_auto_loop() -> None:
    """Run a cycle every ``AUTO_CYCLE_HOURS``, and on demand.

    Waiting on an Event rather than sleeping means a manual "run now" wakes the
    loop instantly and resets the clock, so a button press and the timer can
    never run two cycles over the same database at once.
    """
    import time as _time
    period = AUTO_CYCLE_HOURS * 3600
    # Give the HTTP server a moment to bind before a heavy first cycle, so the
    # dashboard is reachable while the initial import runs.
    _sched_wake.wait(30)
    _sched_wake.clear()
    while True:
        with _sched_lock:
            _sched_state["auto"] = True
            _sched_state["next_run_at"] = db.iso_in(seconds=period)
        _sched_run_cycle(trigger="auto")
        deadline = _time.monotonic() + period
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            if _sched_wake.wait(min(remaining, 60)):
                _sched_wake.clear()
                break


def _sched_start_auto() -> None:
    global _sched_thread
    if _sched_thread and _sched_thread.is_alive():
        return
    _sched_thread = threading.Thread(target=_sched_auto_loop, daemon=True,
                                     name="planwatch-scheduler")
    _sched_thread.start()


# ------------------------------------------------------------------- handler


class Handler(BaseHTTPRequestHandler):
    server_version = "PlanWatch"

    user_id = 0
    crm = CRMAPI(db.DB_PATH)

    def log_message(self, fmt, *args):  # quieter console
        if "/api/sync/status" not in self.path:
            sys.stderr.write(f"  {self.command} {self.path}\n")

    # -- plumbing ---------------------------------------------------------

    def _send(self, payload, status=200, ctype="application/json") -> None:
        body = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if ctype == "application/json" else payload)
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _download(self, body: bytes, filename: str, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    @staticmethod
    def _query(path: str) -> dict:
        parsed = urllib.parse.urlparse(path)
        return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    # -- auth ---------------------------------------------------------------

    def _authorised(self) -> bool:
        """Whether this request carries a valid credential, per DEPLOY.md §3.

        `main()` refuses to start in `render` mode without `BASIC_AUTH` set,
        so by the time a request reaches this method the only way it is empty
        is local/private-network mode with nothing configured - the no-auth
        posture this server has always had, unchanged.

        Compared with `hmac.compare_digest`, not `==`. A plain string
        comparison returns at the first differing character, and on a public
        endpoint that timing difference is enough to recover a password one
        byte at a time.
        """
        if not BASIC_AUTH:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            got = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(got, BASIC_AUTH)

    def _require_auth(self) -> bool:
        """True and silent when the request may proceed; sends 401 and
        returns False otherwise. Every caller must return immediately on
        False rather than falling through to the route it guards."""
        if self._authorised():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="PlanWatch"')
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        return False

    def _require_user_auth(self) -> bool:
        """Gate for USER_ROUTES: whatever `_require_auth` already accepts
        (the admin dashboard's own calls - operator Basic Auth, or nothing
        at all when BASIC_AUTH is unset in local mode, exactly as before),
        OR a valid end-user bearer token. USER_ROUTES holds pre-existing
        endpoints dashboard.html has always called this way (search,
        sale-listings, deals, ...) as well as being reachable from the new
        public webapp/bots - `_authorised()` alone must keep working for the
        first group, or every one of those tabs breaks the moment an
        endpoint moves into this set. The Basic-Auth branch is also what
        lets the Telegram/WhatsApp bots (bots/telegram, bots/whatsapp) in:
        trusted server-side processes carrying PLANWATCH_BASIC_AUTH like any
        other internal caller, not a public browser, so no end-user account
        of their own is needed. True and (for the bearer-token path) sets
        `self.user_id` when the request may proceed; sends 401 and returns
        False otherwise (same contract as `_require_auth`)."""
        self.user_id = getattr(self, "user_id", 0)
        if self._authorised():
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self._send({"error": "login required"}, 401)
            return False
        conn = db.get_conn()
        try:
            user_id = accounts.resolve_session(conn, header[7:].strip())
        finally:
            conn.close()
        if user_id is None:
            self._send({"error": "invalid or expired session"}, 401)
            return False
        self.user_id = user_id
        return True

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        try:
            # Liveness, answered before auth and before anything opens the
            # database.
            #
            # Every other route goes through `_api_get`, which opens the 702 MB
            # store and runs the schema guard - ~0.9 s a call. A platform health
            # check runs every few seconds forever, so pointing one at a route
            # that does that turns monitoring into load. This says only "the
            # process is up and serving", which is exactly what a health check
            # is for, and deliberately reports nothing about the data: it is
            # the one route that must stay reachable without credentials, so it
            # must not become a way to read anything. See DEPLOY.md.
            if route == "/api/health":
                return self._send({"ok": True, "service": "planwatch"})
            if route in PUBLIC_ROUTES:
                return self._api_get(route, self._query(self.path))
            if route in USER_ROUTES or route.startswith("/api/plan-summary/"):
                if not self._require_user_auth():
                    return
                return self._api_get(route, self._query(self.path))
            if not self._require_auth():
                return
            if route in ("/", "/index.html"):
                if not PAGE.exists():
                    return self._send({"error": f"{PAGE.name} is missing"}, 500)
                return self._send(PAGE.read_bytes(), ctype="text/html")
            if route.startswith("/api/"):
                return self._api_get(route, self._query(self.path))
            self._send({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except BadRequest as exc:
            self._send({"error": str(exc)}, 400)
        except Exception:
            self._fail(route)

    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        try:
            if route in PUBLIC_ROUTES:
                return self._api_post(route, self._body())
            if route in USER_ROUTES:
                if not self._require_user_auth():
                    return
                return self._api_post(route, self._body())
            # Every other POST writes something - a scheduler run, a tabu
            # extract, a subscription. None of them is exempt like
            # /api/user/login is.
            if not self._require_auth():
                return
            self._api_post(route, self._body())
        except BrokenPipeError:
            pass
        except BadRequest as exc:
            self._send({"error": str(exc)}, 400)
        except Exception:
            self._fail(route)

    def _fail(self, route: str) -> None:
        """
        Log the traceback server-side; send the client a short message.
        Shipping `traceback.format_exc()` in the response body leaked absolute
        source paths to anything that could reach the port.
        """
        sys.stderr.write(f"\n!! error handling {route}\n{traceback.format_exc()}\n")
        self._send({"error": "internal error - see the server console for details"},
                   500)

    # -- GET api ----------------------------------------------------------

    def _api_get(self, route: str, q: dict) -> None:
        conn = db.get_conn()
        try:
            if route == "/api/user/me":
                user = accounts.get_user(conn, self.user_id)
                if user is None:
                    return self._send({"error": "user no longer exists"}, 401)
                return self._send(user)
            if route == "/api/public/bot-links":
                # Non-secret (a phone number, a bot @username) and needed
                # before login - webapp/index.html's chat-search card shows
                # these to logged-out visitors too - so this is intentionally
                # in PUBLIC_ROUTES, not USER_ROUTES.
                return self._send(accounts.get_settings(conn))
            if route == "/api/admin/users":
                return self._send({"users": accounts.list_users(conn)})
            if route == "/api/admin/settings":
                # Basic-Auth only (default gate below, not USER_ROUTES) -
                # this is the one place the Telegram bot token is readable,
                # see accounts.get_admin_settings.
                return self._send(accounts.get_admin_settings(conn))
            if route == "/api/admin/bots-status":
                # No secrets in the response - just enough for the
                # "הגדרות מנהל" tab in dashboard.html to say what is and is
                # not configured, the same posture credentialed_client's
                # status() already uses for GovMap/Tabu keys.
                import alerts
                smtp = alerts.smtp_config()
                telegram_configured = bool(
                    os.environ.get("PLANWATCH_TELEGRAM_TOKEN")
                    or accounts.get_admin_settings(conn).get("telegram_bot_token"))
                last_test = None
                if WHATSAPP_TEST_RESULT_PATH.exists():
                    try:
                        last_test = json.loads(WHATSAPP_TEST_RESULT_PATH.read_text("utf-8"))
                    except (OSError, ValueError):
                        last_test = None
                return self._send({
                    "telegram_configured": telegram_configured,
                    "whatsapp_linked": (WHATSAPP_AUTH_DIR / "creds.json").exists(),
                    "whatsapp_qr_pending": WHATSAPP_QR_PATH.exists(),
                    "whatsapp_disconnect_pending": WHATSAPP_DISCONNECT_FLAG.exists(),
                    "whatsapp_test_pending": WHATSAPP_TEST_FLAG.exists(),
                    "whatsapp_last_test": last_test,
                    "email_configured": bool(smtp.get("host")),
                    "email_from": smtp.get("sender"),
                })
            if route == "/api/admin/whatsapp-qr":
                # bots/whatsapp/bot.js writes a fresh PNG here each time it
                # needs linking; deleted once it connects. Basic-Auth gated
                # like the rest of the admin dashboard - the operator opens
                # this URL once and scans it with their phone.
                if not WHATSAPP_QR_PATH.exists():
                    return self._send(
                        {"error": "no QR pending - already linked, or the WhatsApp bot has not started"},
                        404)
                return self._send(WHATSAPP_QR_PATH.read_bytes(), ctype="image/png")
            if route == "/api/bootstrap":
                return self._send(self._bootstrap(conn))
            if route == "/api/live-sale-listings":
                # This route returns rows, not a page, so a bare call must stay
                # bounded - the store is 61k ads with up to 30 image URLs each.
                # Callers wanting the whole stock ask for it explicitly; the
                # sale tab goes through /api/combined-sale-listings, which pages.
                return self._send(_live_sale_listings({"limit": "400", **q}))
            if route == "/api/combined-sale-listings":
                return self._send(_combined_sale_listings(q))
            if route == "/api/listing-gallery":
                # Per listing, on explicit request - the same posture as
                # /api/listing-contact. AD and Komo are harvested from search
                # cards, which carry one photo; the rest of the gallery lives on
                # the ad page and is fetched only when someone opens the photo.
                import listing_gallery
                return self._send(listing_gallery.gallery(
                    q.get("url", ""), q.get("source", "")))
            if route == "/api/live-sources":
                return self._send(_live_sources())
            if route == "/api/onmap/harvest":
                import onmap_feed
                return self._send({**onmap_feed.state(), **onmap_feed.counts()})
            if route == "/api/onmap/contacts":
                # The advertiser's number lives on ONMAP's per-property record,
                # not in its search feed - 20 of 20 sampled listings had one.
                import onmap_feed
                return self._send(onmap_feed.contact_state())
            if route == "/api/listing-contact/onmap":
                import onmap_feed
                return self._send(onmap_feed.fetch_contact(q.get("id", "")))
            if route == "/api/yad2/harvest":
                import yad2_feed
                return self._send({**yad2_feed.state(), **yad2_feed.counts()})
            if route == "/api/facebook/harvest":
                import facebook_feed
                return self._send({**facebook_feed.state(), **facebook_feed.counts()})
            if route == "/api/facebook/listings":
                import facebook_feed
                limit = as_int(q.get("limit"), "limit", 100, low=1, high=500)
                return self._send({
                    "listings": facebook_feed.rows(limit=limit),
                    "total": facebook_feed.counts().get("total", 0),
                })
            if route == "/api/listing-contact":
                # One listing, on explicit request. There is deliberately no
                # bulk variant of this endpoint - see yad2_contact's header.
                import yad2_contact
                token = (q.get("token") or "").strip()
                if not token:
                    raise BadRequest("token is required")
                return self._send(yad2_contact.resolve(
                    token, refresh=q.get("refresh") == "1"))
            if route == "/api/listing-contact/coverage":
                import yad2_contact
                return self._send(yad2_contact.coverage())
            if route == "/api/leads":
                import lead_intel
                return self._send(lead_intel.leads(
                    locality=(q.get("locality") or "").strip(),
                    neighborhood=(q.get("neighborhood") or "").strip(),
                    source=(q.get("source") or "").strip(),
                    lead_type=(q.get("lead_type") or "").strip(),
                    min_days=as_float(q.get("min_days"), "min_days"),
                    max_days=as_float(q.get("max_days"), "max_days"),
                    min_score=as_float(q.get("min_score"), "min_score"),
                    price_cut_only=q.get("price_cut_only") == "1",
                    private_only=q.get("private_only") == "1",
                    min_price=as_float(q.get("min_price"), "min_price"),
                    max_price=as_float(q.get("max_price"), "max_price"),
                    exclude_suspect=q.get("exclude_suspect") == "1",
                    sort=(q.get("sort") or "lead_score").strip(),
                    limit=as_int(q.get("limit"), "limit", 80, low=1, high=500),
                    offset=as_int(q.get("offset"), "offset", 0, low=0)))
            if route == "/api/plans":
                return self._send(self._plans(conn, q))
            if route.startswith("/api/plan/"):
                return self._send(self._plan(conn, int(route.rsplit("/", 1)[1])))
            if route.startswith("/api/plan-summary/"):
                return self._send(self._plan_summary(conn, int(route.rsplit("/", 1)[1])))
            if route == "/api/at":
                return self._send(self._at(conn, q))
            if route == "/api/parcel":
                return self._send(self._parcel(conn, q))
            if route == "/api/parcel-at":
                # Single-point גוש/חלקה lookup, for the webapp's listing-detail
                # modal: ONMAP rows already carry gush/helka, but Yad2 rows
                # only carry lat/lon - this is the on-demand fallback so a
                # buyer viewing one card can still see the parcel it sits on,
                # without eagerly resolving it for the whole result page (see
                # _with_parcel_refs, the same idea for plan search results).
                import govmap_client
                hit = govmap_client.parcel_at_point(
                    required_float(q, "lat"), required_float(q, "lon"), timeout=8)
                return self._send(hit or {"gush": None, "helka": None})
            if route == "/api/live-deals":
                import live_deals_view as ldv
                gush = (q.get("gush") or "").strip()
                helka = (q.get("helka") or "").strip()
                if not gush or not helka:
                    raise BadRequest("gush and helka are required")
                return self._send(ldv.deals_for_parcel(
                    gush, helka,
                    min_price=as_float(q.get("min_price"), "min_price"),
                    max_price=as_float(q.get("max_price"), "max_price"),
                    date_from=(q.get("date_from") or "").strip() or None,
                    date_to=(q.get("date_to") or "").strip() or None,
                    limit=as_int(q.get("limit"), "limit", 100, low=1, high=500),
                ))
            if route == "/api/search":
                return self._send(self._search(conn, q))
            if route == "/api/opendata":
                return self._send(self._open_data(conn, q))
            if route == "/api/google-places":
                return self._send(self._google_places(q))
            if route == "/api/renewal":
                return self._send(self._renewal(conn, q))
            if route == "/api/find":
                return self._send(self._find(conn, q))
            if route == "/api/market":
                import opportunity
                return self._send(opportunity.market_snapshot(conn))
            if route == "/api/finance":
                return self._send(self._finance(conn, q))
            if route == "/api/amenities":
                return self._send(self._amenities(conn, q))
            if route == "/api/rentals":
                return self._send(self._rentals(conn, q))
            if route == "/api/resolve-address":
                import parcel_area
                return self._send(parcel_area.resolve_address(q.get("q") or ""))
            if route == "/api/opportunities":
                return self._send(self._opportunities(conn, q))
            if route == "/api/tama38":
                import opportunity
                return self._send(opportunity.tama38_hotspots(
                    conn, limit=as_int(q.get("limit"), "limit", 25, low=1, high=100)))
            if route == "/api/deals":
                return self._send(self._deals(conn, q))
            if route == "/api/transactions":
                return self._send(self._transactions(q))
            if route == "/api/strategy":
                import strategy
                role = (q.get("role") or "investor").strip()
                # An unrecognised role silently fell back to "investor", so a
                # typo in the query answered 200 with the wrong report. Name it.
                if role not in strategy.ROLE_VIEWS:
                    raise BadRequest(
                        f"unknown role {role!r}; expected one of "
                        f"{', '.join(sorted(strategy.ROLE_VIEWS))}")
                try:
                    return self._send(strategy.locality_strategy(
                        conn, q.get("locality", ""), role))
                except ValueError as exc:
                    # strategy.py validates its own input; a bad locality is a
                    # 400, not the 500 an escaping ValueError produced.
                    raise BadRequest(str(exc)) from exc
            if route == "/api/localities":
                import opportunity
                return self._send(opportunity.locality_ranking(
                    conn, limit=as_int(q.get("limit"), "limit", 40, low=1, high=200)))
            if route == "/api/plan-detail":
                return self._send(self._plan_detail(conn, q))
            if route == "/api/landuse-at":
                import plan_detail_client as pdc
                return self._send({
                    "lat": required_float(q, "lat"), "lon": required_float(q, "lon"),
                    "cells": pdc.landuse_at_point(
                        conn, required_float(q, "lon"), required_float(q, "lat"),
                        limit=as_int(q.get("limit"), "limit", 20, low=1, high=100)),
                })
            if route == "/api/designations":
                import plan_detail_client as pdc
                return self._send({"rows": pdc.designation_catalog(
                    conn, limit=as_int(q.get("limit"), "limit", 60, low=1, high=300))})
            if route == "/api/appraisals":
                return self._send(self._appraisals(conn, q))
            if route == "/api/progress":
                import appraisal_client as ac
                gush = (q.get("gush") or "").strip()
                if gush:
                    return self._send({"gush": gush, "helka": q.get("helka"),
                                       "rows": ac.progress_for_parcel(
                                           conn, gush, q.get("helka"))})
                return self._send(ac.progress_summary(conn))
            if route == "/api/compare":
                import compare
                locality = (q.get("locality") or "").strip()
                if locality:
                    return self._send(compare.compare_locality(conn, locality))
                return self._send(compare.compare_all(
                    conn,
                    min_rows=as_int(q.get("min_rows"), "min_rows", 1, low=1),
                    limit=as_int(q.get("limit"), "limit", 200, low=1, high=600)))
            if route == "/api/compare-parcel":
                import compare
                gush = (q.get("gush") or "").strip()
                helka = (q.get("helka") or "").strip()
                if not gush or not helka:
                    raise BadRequest("gush and helka are required")
                return self._send(compare.parcel_comparison(conn, gush, helka))
            if route == "/api/dormant":
                import dormant
                which = (q.get("kind") or "").strip()
                limit = as_int(q.get("limit"), "limit", 60, low=1, high=300)
                offset = as_int(q.get("offset"), "offset", 0, low=0)
                if not which:
                    return self._send({"summary": dormant.summary(conn),
                                       "detectors": list(dormant.DETECTORS)})
                if which not in dormant.DETECTORS:
                    raise BadRequest(
                        f"kind must be one of {sorted(dormant.DETECTORS)}")
                fn = dormant.DETECTORS[which]
                kwargs = {"limit": limit, "offset": offset}
                if which == "approved_no_permit":
                    kwargs["locality"] = q.get("locality", "")
                    kwargs["min_units"] = as_int(q.get("min_units"),
                                                 "min_units", 1, low=0)
                elif which == "declared_stalled":
                    kwargs["min_years"] = as_float(q.get("min_years"),
                                                   "min_years",
                                                   dormant.MIN_STALLED_YEARS)
                return self._send(fn(conn, **kwargs))
            if route == "/api/estates":
                import dormant
                return self._send(dormant.estate_candidates(
                    conn,
                    limit=as_int(q.get("limit"), "limit", 60, low=1, high=300),
                    offset=as_int(q.get("offset"), "offset", 0, low=0)))
            if route == "/api/crossref":
                import dormant
                return self._send(dormant.crossref_new(
                    conn, limit=as_int(q.get("limit"), "limit", 40,
                                       low=1, high=200)))
            if route == "/api/alerts":
                import alerts
                return self._send(alerts.status(conn))
            if route == "/api/lookup":
                import entity_index as ei
                text = (q.get("q") or "").strip()
                if not text:
                    return self._send({"total": 0, "groups": [],
                                       "counts": ei.counts(conn)})
                return self._send(ei.search(
                    conn, text, kind=(q.get("kind") or "").strip() or None,
                    limit=as_int(q.get("limit"), "limit", 60, low=1, high=300),
                    offset=as_int(q.get("offset"), "offset", 0, low=0)))
            if route == "/api/crosswalk":
                import entity_index as ei
                name = (q.get("name") or "").strip()
                if not name:
                    raise BadRequest("name is required")
                return self._send(ei.crosswalk(
                    conn, name,
                    limit=as_int(q.get("limit"), "limit", 200, low=1, high=800)))
            if route == "/api/sources-status":
                import credentialed_client as cc
                return self._send({
                    "credentialed": cc.status(),
                    "note": "מקורות בהרשאה. מדווח רק אם מפתח קיים, לא את "
                            "ערכו. כל שאר המערכת רצה על מקורות פתוחים.",
                })
            if route == "/api/audit":
                import credentialed_client as cc
                return self._send({"rows": cc.audit_log(
                    conn, limit=as_int(q.get("limit"), "limit", 100,
                                       low=1, high=500))})
            if route == "/api/owners":
                import owners_client as oc
                # Validate limit up front, whichever branch runs. Parsing it
                # only inside the `name` branch meant `?limit=zz` answered 200
                # on the other two - a bad parameter must always be a 400.
                limit = as_int(q.get("limit"), "limit", 100, low=1, high=500)
                gush = (q.get("gush") or "").strip()
                name = (q.get("name") or "").strip()
                if gush:
                    return self._send(oc.owner_dossier(
                        conn, gush, (q.get("helka") or "").strip() or None))
                if name:
                    return self._send(oc.parcels_for_owner(
                        conn, name, limit=limit))
                return self._send({"counts": oc.counts(conn)})
            if route == "/api/municipal":
                import municipal_client as mn
                return self._send({
                    "counts": mn.counts(conn),
                    "ages": mn.building_ages(conn),
                    "tama38_permits": mn.tama38_permits(
                        conn, limit=as_int(q.get("limit"), "limit", 60,
                                           low=1, high=300)),
                })
            if route == "/api/parcel-polygon":
                import municipal_client as mn
                gush = (q.get("gush") or "").strip()
                helka = (q.get("helka") or "").strip()
                if not gush or not helka:
                    raise BadRequest("gush and helka are required")
                poly = mn.parcel_polygon(conn, gush, helka)
                return self._send({"gush": gush, "helka": helka,
                                   "parcel": poly,
                                   "note": None if poly else
                                   "אין פוליגון חלקה במאגר העירוני "
                                   "(כיסוי: תל אביב-יפו בלבד)."})
            if route == "/api/tenders":
                return self._send(self._tenders(conn, q))
            if route == "/api/tender-lots":
                import tenders_client as tc
                mid = as_int(q.get("michraz_id"), "michraz_id")
                if not mid:
                    raise BadRequest("michraz_id is required")
                return self._send({"michraz_id": mid,
                                   "lots": tc.lots_for_tender(conn, mid),
                                   "url": tc.tender_url(mid)})
            if route == "/api/land-prices":
                import tenders_client as tc
                return self._send(tc.price_benchmarks(
                    conn, locality=q.get("locality", ""),
                    min_lots=as_int(q.get("min_lots"), "min_lots", 1, low=1)))
            if route == "/api/documents":
                return self._send(self._documents(conn, q))
            if route == "/api/document-file":
                return self._document_file(conn, q)
            if route == "/api/document-text":
                return self._send(self._document_text(conn, q))
            if route == "/api/data-quality":
                return self._send(self._data_quality(conn))
            if route == "/api/planners":
                import appraisal_client as ac
                return self._send({"rows": ac.planners_in(
                    conn, locality=q.get("locality", ""),
                    speciality=q.get("speciality", ""),
                    limit=as_int(q.get("limit"), "limit", 60, low=1, high=300))})
            if route == "/api/tabu/status":
                with _tabu_lock:
                    state = dict(_tabu_state)
                import appraisal_client as ac
                import land_registry_client as lr
                import market_client as mk
                import open_data_client as od
                import plan_detail_client as pdc
                import urban_renewal_client as ur
                state["local_rows"] = lr.local_count(conn)
                state["opendata"] = dict(od.counts(conn),
                                         renewal=ur.count_local(conn))
                state["market"] = mk.counts(conn)
                import document_client as dc
                state["registry"] = dict(ac.counts(conn), **pdc.counts(conn))
                import tenders_client as tc
                state["documents"] = dc.counts(conn)
                import municipal_client as mn
                state["tenders"] = tc.counts(conn)
                import owners_client as oc
                state["municipal"] = mn.counts(conn)
                import entity_index as ei
                state["owners"] = oc.counts(conn)
                state["index"] = ei.counts(conn)
                return self._send(state)
            if route == "/api/scheduler":
                import scheduler
                with _sched_lock:
                    live = dict(_sched_state)
                return self._send({
                    "live": live,
                    "auto_cycle_hours": AUTO_CYCLE_HOURS,
                    **scheduler.status(
                        conn, limit=as_int(q.get("limit"), "limit", 20,
                                           low=1, high=200)),
                })
            if route == "/api/geometries":
                return self._send(self._geometries(conn, q))
            if route == "/api/timeline":
                return self._send(self._timeline(conn, q))
            if route == "/api/breakdown":
                return self._send(self._breakdown(conn, q))
            if route == "/api/history":
                return self._send(self._history(conn, q))
            if route == "/api/notifications":
                return self._send(self._notifications(conn, q))
            if route == "/api/subscriptions":
                return self._send(self._subscriptions(conn))
            if route == "/api/sync/status":
                with _sync_lock:
                    return self._send(dict(_sync_state))
            if route == "/api/data-version":
                return self._send(_data_version())
            if route == "/api/unified/sources":
                return self._send(self._unified_sources())
            if route == "/api/unified/health":
                return self._send(self._unified_health())
            if route == "/api/unified/validate":
                return self._send(self._unified_validate(payload))
            if route == "/api/listings":
                return self._send(self._api_listings(conn, q))
            if route == "/api/listings/stats":
                return self._send(self._api_listing_stats(conn))
            if route == "/api/local-listings":
                return self._send(self._api_local_listings(conn, q))
            if route == "/api/integrations":
                return self._send(self._api_integrations(conn))
            if route == "/api/source-health":
                return self._send(self._api_source_health())
            if route == "/api/runs":
                return self._send(self._api_runs(conn, q))
            if route == "/api/alerts":
                return self._send(self._api_alerts(conn, q))
            if route == "/api/export":
                return self._send(self._export(conn, q))

            # CRM routes
            if route.startswith("/api/crm/"):
                if not self._require_user_auth():
                    return
                user_id = self.user_id
                if route == "/api/crm/leads":
                    return self._send(self.crm.list_leads(q, user_id))
                if route == "/api/crm/pipelines":
                    return self._send(self.crm.list_pipelines(user_id))
                if route == "/api/crm/tasks":
                    return self._send(self.crm.list_tasks(q, user_id))
                if route == "/api/crm/stats":
                    return self._send(self.crm.get_stats(user_id))
                if route.startswith("/api/crm/leads/"):
                    lead_id = int(route.rsplit("/", 1)[1])
                    return self._send(self.crm.get_lead(lead_id, user_id))
                if route.startswith("/api/crm/pipelines/"):
                    pipeline_id = int(route.rsplit("/", 1)[1])
                    return self._send(self.crm.get_pipeline(pipeline_id, user_id))
                if route.startswith("/api/crm/tasks/"):
                    task_id = int(route.rsplit("/", 1)[1])
                    return self._send(self.crm.get_task(task_id, user_id))

            self._send({"error": "unknown endpoint"}, 404)
        finally:
            conn.close()

    def _api_post(self, route: str, payload: dict) -> None:
        if route == "/api/user/login":
            username = (payload.get("username") or "").strip()
            password = payload.get("password") or ""
            if not username or not password:
                raise BadRequest("username and password are required")
            conn = db.get_conn()
            try:
                user_id = accounts.authenticate(conn, username, password)
                if user_id is None:
                    return self._send({"error": "invalid username or password"}, 401)
                token = accounts.create_session(conn, user_id)
                user = accounts.get_user(conn, user_id)
                return self._send({"token": token, **user})
            finally:
                conn.close()

        if route == "/api/user/logout":
            header = self.headers.get("Authorization", "")
            conn = db.get_conn()
            try:
                if header.startswith("Bearer "):
                    accounts.delete_session(conn, header[7:].strip())
                return self._send({"ok": True})
            finally:
                conn.close()

        if route == "/api/admin/users":
            username = (payload.get("username") or "").strip()
            password = payload.get("password") or ""
            if not username or not password:
                raise BadRequest("username and password are required")
            conn = db.get_conn()
            try:
                try:
                    user_id = accounts.create_user(
                        conn, username, password, permissions=payload.get("permissions"))
                except sqlite3.IntegrityError:
                    raise BadRequest("that username already exists") from None
                return self._send({"id": user_id, "username": username}, 201)
            finally:
                conn.close()

        if route == "/api/admin/users/permissions":
            user_id = as_int(payload.get("id"), "id")
            permissions = payload.get("permissions")
            if not isinstance(permissions, dict):
                raise BadRequest("permissions must be an object")
            conn = db.get_conn()
            try:
                accounts.set_permissions(conn, user_id, permissions)
                return self._send({})
            finally:
                conn.close()

        if route == "/api/admin/settings":
            conn = db.get_conn()
            try:
                accounts.set_settings(conn, payload)
                return self._send(accounts.get_admin_settings(conn))
            finally:
                conn.close()

        if route == "/api/admin/whatsapp-disconnect":
            # dashboard.py never holds the live Baileys socket - only
            # bots/whatsapp/bot.js does - so this cannot log out directly.
            # It drops a flag file that process polls for (see this file's
            # WHATSAPP_DISCONNECT_FLAG and bot.js's DISCONNECT_FLAG_PATH);
            # the bot calls sock.logout(), wipes its auth state and exits so
            # the next scan starts clean. If the bot process is not running,
            # the flag just sits there - reported back so the UI can say so
            # rather than claiming success it cannot verify.
            WHATSAPP_AUTH_DIR.mkdir(parents=True, exist_ok=True)
            WHATSAPP_DISCONNECT_FLAG.write_text(db.now_iso(), encoding="utf-8")
            return self._send({"requested": True,
                                "note": "בקשת ניתוק נשלחה לתהליך הבוט. "
                                        "אם הבוט לא רץ כרגע, הבקשה תבוצע כשיופעל."})

        if route == "/api/admin/whatsapp-test":
            # Same flag-file mechanism: asks the running bot to send a
            # WhatsApp message to itself, so the operator gets real proof the
            # link still works end-to-end rather than just "creds.json
            # exists". Result lands in WHATSAPP_TEST_RESULT_PATH; the caller
            # polls /api/admin/bots-status for whatsapp_last_test.
            if not (WHATSAPP_AUTH_DIR / "creds.json").exists():
                raise BadRequest("הוואטסאפ לא מקושר עדיין - אין מה לבדוק")
            WHATSAPP_TEST_RESULT_PATH.unlink(missing_ok=True)
            WHATSAPP_TEST_FLAG.write_text(db.now_iso(), encoding="utf-8")
            return self._send({"requested": True})

        if route == "/api/admin/users/reset-password":
            user_id = as_int(payload.get("id"), "id")
            password = payload.get("password") or ""
            if not password:
                raise BadRequest("password is required")
            conn = db.get_conn()
            try:
                accounts.reset_password(conn, user_id, password)
                return self._send({})
            finally:
                conn.close()

        if route == "/api/admin/users/deactivate":
            user_id = as_int(payload.get("id"), "id")
            conn = db.get_conn()
            try:
                accounts.set_active(conn, user_id, active=bool(payload.get("active", False)))
                return self._send({})
            finally:
                conn.close()

        if route == "/api/yad2/harvest":
            # Refreshing the Yad2 stock is an explicit act, never a side effect
            # of rendering a table. Calling this while a pass runs is a no-op.
            import yad2_feed
            return self._send(yad2_feed.start(
                target=as_int(payload.get("target"), "target", 18000,
                              low=1, high=18000),
                workers=as_int(payload.get("workers"), "workers",
                               yad2_feed.DEFAULT_WORKERS, low=1, high=8)))

        if route == "/api/facebook/harvest":
            import facebook_feed
            return self._send(facebook_feed.start(
                target=as_int(payload.get("target"), "target", 500,
                              low=1, high=5000)))

        if route == "/api/facebook/harvest/stop":
            import facebook_feed
            return self._send(facebook_feed.stop())

        if route == "/api/yad2/feed-harvest":
            # The real search feed - the only source carrying private sellers.
            # Needs the Node service up; yad2_feed says so if it is not.
            import yad2_feed
            return self._send(yad2_feed.start_feed(
                max_pages_per_region=as_int(
                    payload.get("max_pages_per_region"),
                    "max_pages_per_region", None, low=1),
                resume=payload.get("resume", True) is not False))

        if route == "/api/onmap/harvest":
            import onmap_feed
            return self._send(onmap_feed.start())
        if route == "/api/onmap/contacts":
            import onmap_feed
            return self._send(onmap_feed.start_contacts(
                limit=as_int(payload.get("limit"), "limit", 0, low=0, high=5000)
                      or None))

        if route == "/api/yad2/harvest/stop":
            import yad2_feed
            return self._send(yad2_feed.stop())

        if route == "/api/leads/note":
            import lead_intel
            # A field the caller did not send is left alone; a field sent empty
            # is a deliberate clear. Collapsing the two would make it
            # impossible to remove a task once typed.
            fields = {name: str(payload[name]).strip()
                      for name in ("stage", "owner", "note", "next_action",
                                   "due_date")
                      if name in payload}
            try:
                return self._send(lead_intel.save_note(
                    (payload.get("listing_key") or "").strip(), **fields))
            except ValueError as exc:
                raise BadRequest(str(exc)) from None

        if route == "/api/alerts/watch":
            import alerts
            conn = db.get_conn()
            try:
                return self._send(alerts.watch_all(
                    conn, subscription_id=as_int(payload.get("subscription_id"),
                                                 "subscription_id"),
                    dry_run=bool(payload.get("dry_run"))))
            finally:
                conn.close()

        if route == "/api/alerts/deliver":
            import alerts
            conn = db.get_conn()
            try:
                return self._send(alerts.deliver_pending(
                    conn, client_id=(payload.get("client_id") or "").strip() or None,
                    dry_run=bool(payload.get("dry_run"))))
            finally:
                conn.close()

        if route == "/api/alerts/seed":
            import alerts
            conn = db.get_conn()
            try:
                return self._send(alerts.seed_baseline(
                    conn, subscription_id=as_int(payload.get("subscription_id"),
                                                 "subscription_id")))
            finally:
                conn.close()

        if route == "/api/alerts/channel":
            import alerts
            client = (payload.get("client_id") or "").strip()
            if not client:
                raise BadRequest("client_id is required")
            conn = db.get_conn()
            try:
                return self._send(alerts.set_channel(
                    conn, client,
                    channel=(payload.get("channel") or "console").strip(),
                    target=(payload.get("target") or "").strip() or None))
            except ValueError as exc:
                raise BadRequest(str(exc)) from None
            finally:
                conn.close()

        if route == "/api/alerts/run":
            import alerts
            conn = db.get_conn()
            try:
                return self._send(alerts.run_once(
                    conn, dry_run=bool(payload.get("dry_run"))))
            finally:
                conn.close()

        if route == "/api/tabu/extract":
            # Official land-registry extract. POST, not GET: it is a
            # credentialed, audited and typically billable action, and must not
            # be triggerable by a link or a prefetch.
            import credentialed_client as cc
            gush = str(payload.get("gush") or "").strip()
            helka = str(payload.get("helka") or "").strip()
            if not gush or not helka:
                raise BadRequest("gush and helka are required")
            conn = db.get_conn()
            try:
                return self._send(cc.fetch_extract(
                    conn, gush, helka,
                    sub_helka=str(payload.get("sub_helka") or "").strip() or None,
                    actor=str(payload.get("actor") or "dashboard")))
            except cc.NotConfigured as exc:
                return self._send({"error": str(exc), "configured": False}, 501)
            except Exception as exc:
                return self._send(
                    {"error": f"extract request failed: {type(exc).__name__}"}, 502)
            finally:
                conn.close()

        if route == "/api/tabu/import":
            with _tabu_lock:
                if _tabu_state["running"]:
                    return self._send({"error": "an import is already running"}, 409)
            datasets = payload.get("datasets") or ["tabu"]
            threading.Thread(target=_run_tabu_import, args=(datasets,),
                             daemon=True).start()
            return self._send({"started": True, "datasets": datasets})

        if route == "/api/tabu/stop":
            _tabu_cancel.set()
            return self._send({"stopping": True})

        if route == "/api/scheduler/run":
            # The manual "run now" button. Runs the same cycle the timer runs,
            # so there is one code path and no second definition of "a run".
            import scheduler
            with _sched_lock:
                if _sched_state["running"]:
                    return self._send(
                        {"error": "a scheduler cycle is already running",
                         "job": _sched_state["job"]}, 409)
            only = payload.get("only") or None
            if only is not None:
                if isinstance(only, str):
                    only = [j.strip() for j in only.split(",") if j.strip()]
                unknown = [j for j in only if j not in scheduler.JOBS]
                if unknown:
                    raise BadRequest(
                        f"unknown job(s): {', '.join(unknown)}; "
                        f"expected from {', '.join(scheduler.ORDER)}")
                only = set(only)
            # `force` skips the per-job interval check, which is what a user
            # pressing "run now" almost always means for a specific job.
            threading.Thread(
                target=_sched_run_cycle,
                kwargs={"only": only, "force": bool(payload.get("force")),
                        "trigger": "manual"},
                daemon=True).start()
            return self._send({"started": True,
                               "only": sorted(only) if only else None,
                               "force": bool(payload.get("force"))})

        if route == "/api/scheduler/stop":
            _sched_cancel.set()
            return self._send({"stopping": True})

        if route == "/api/sync/stop":
            _sync_cancel.set()
            with _sync_lock:
                _sync_state["stopping"] = True
            return self._send({"stopping": True})

        if route == "/api/sync":
            with _sync_lock:
                if _sync_state["running"]:
                    return self._send({"error": "a sync is already running"}, 409)
            mode = "full" if payload.get("mode") == "full" else "incremental"
            threading.Thread(
                target=_run_sync,
                args=(mode, payload.get("since") or None,
                      bool(payload.get("notify_bootstrap"))),
                daemon=True,
            ).start()
            return self._send({"started": True, "mode": mode})

        conn = db.get_conn()
        try:
            if route == "/api/notifications/deliver":
                # A bare `int(i)` here answered 500 "internal error" for a
                # malformed id, and iterating a plain string turned "abc" into
                # three ids. Both are caller mistakes, so both are 400s that
                # say what was wrong.
                raw_ids = payload.get("ids", [])
                if not isinstance(raw_ids, (list, tuple)):
                    raise BadRequest(
                        f"ids must be a list of numbers (got {type(raw_ids).__name__})")
                ids = [as_int(i, "ids") for i in raw_ids]
                if ids:
                    marks = ",".join("?" for _ in ids)
                    conn.execute(
                        f"UPDATE notifications SET delivered=1 WHERE id IN ({marks})",
                        ids)
                    conn.commit()
                return self._send({"delivered": len(ids)})

            if route == "/api/subscriptions":
                lat = payload.get("lat")
                lon = payload.get("lon")
                has_coords = lat not in (None, "") and lon not in (None, "")
                has_parcel = (payload.get("gush") or "").strip() and \
                             (payload.get("helka") or "").strip()
                if not has_coords and not has_parcel:
                    # Also catches an unparseable/empty body - never write a
                    # junk row that can neither be matched nor resolved.
                    return self._send(
                        {"error": "subscription needs lat+lon or gush+helka"}, 400)
                sub_id = db.add_subscription(
                    conn,
                    client_id=(payload.get("client_id") or "").strip() or "default",
                    gush=(payload.get("gush") or "").strip() or None,
                    helka=(payload.get("helka") or "").strip() or None,
                    label=(payload.get("label") or "").strip() or None,
                    lat=float(lat) if lat not in (None, "") else None,
                    lon=float(lon) if lon not in (None, "") else None,
                    radius_m=float(payload["radius_m"]) if payload.get("radius_m") else None,
                )
                return self._send({"id": sub_id})

            if route == "/api/subscriptions/delete":
                # `int(payload["id"])` raised KeyError on an empty body, which
                # surfaced as a 500 - a caller mistake reported as a server
                # fault. as_int gives the 400 with a usable message instead.
                sid = as_int(payload.get("id"), "id")
                if sid is None:
                    raise BadRequest("id is required")
                conn.execute("DELETE FROM notifications WHERE subscription_id=?", (sid,))
                conn.execute("DELETE FROM subscription_plan_matches WHERE subscription_id=?", (sid,))
                conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
                conn.commit()
                return self._send({"deleted": sid})

            if route == "/api/subscriptions/resolve":
                import gush_helka_resolver as ghr
                try:
                    count = ghr.resolve_pending_subscriptions(conn, verbose=False)
                    return self._send({"resolved": count})
                except ghr.ResolverNotConfigured as exc:
                    return self._send({"error": str(exc)}, 400)

            if route == "/api/backfill":
                import contextlib

                import sync
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    added = sync.backfill_matches(conn)
                return self._send({"added": added, "log": buf.getvalue().splitlines()})

            if route == "/api/unified/run":
                return self._send(self._unified_run(payload))

            # CRM POST routes
            if route.startswith("/api/crm/"):
                if not self._require_user_auth():
                    return
                user_id = self.user_id
                if route == "/api/crm/leads":
                    return self._send(self.crm.create_lead(payload, user_id))
                if route.startswith("/api/crm/leads/"):
                    lead_id = int(route.rsplit("/", 1)[1])
                    if route.endswith("/transition"):
                        new_status = payload.get("new_status") or payload.get("status")
                        note = payload.get("note")
                        return self._send(self.crm.transition_lead(lead_id, new_status, user_id, note))
                    if route.endswith("/assign"):
                        assigned_to = payload.get("assigned_to")
                        return self._send(self.crm.assign_lead(lead_id, assigned_to, user_id))
                    return self._send(self.crm.update_lead(lead_id, payload, user_id))
                if route == "/api/crm/tasks":
                    return self._send(self.crm.create_task(payload, user_id))
                if route.startswith("/api/crm/tasks/"):
                    task_id = int(route.rsplit("/", 1)[1])
                    if route.endswith("/complete"):
                        return self._send(self.crm.complete_task(task_id, user_id))
                    if route.endswith("/cancel"):
                        return self._send(self.crm.cancel_task(task_id, user_id))
                    return self._send(self.crm.update_task(task_id, payload, user_id))
                if route == "/api/crm/pipelines":
                    return self._send(self.crm.create_pipeline(payload, user_id))
                if route.startswith("/api/crm/pipelines/"):
                    pipeline_id = int(route.rsplit("/", 1)[1])
                    if route.endswith("/advance"):
                        return self._send(self.crm.advance_lead_stage(pipeline_id, user_id))
                    return self._send(self.crm.get_pipeline(pipeline_id, user_id))

            self._send({"error": "unknown endpoint"}, 404)
        finally:
            conn.close()

    # -- queries ----------------------------------------------------------

    @staticmethod
    def _normalize_listing(row: dict) -> dict:
        """Map a raw listing row to the standard dashboard contract.

        The normalized_listings table and the scored sale feeds use different
        field names for the same facts; this normalizer gives the UI one shape
        to render regardless of which source the row came from.
        """
        is_active = row.get("is_active", 1)
        return {
            "id": row.get("canonical_id") or row.get("id") or row.get("token"),
            "title": row.get("title") or row.get("normalized_title") or "",
            "address": row.get("address_text") or row.get("street") or "",
            "city": row.get("city") or row.get("locality") or "",
            "price": row.get("price"),
            "area": row.get("area_sqm") or row.get("sqm"),
            "rooms": row.get("rooms"),
            "status": "active" if is_active else "inactive",
            "source": row.get("source") or "",
            "updatedAt": row.get("updated_at") or row.get("last_seen_at"),
        }

    @staticmethod
    def _dashboard_kpis(conn) -> dict:
        """KPI summary for the dashboard tiles.

        Each value degrades to 0/None if its source is unavailable, so a
        missing feed never blanks the whole KPI row.
        """
        kpis = {
            "totalListings": 0,
            "activeListings": 0,
            "newRequests": 0,
            "hotLeads": 0,
            "closedDeals": 0,
            "marketSpread": None,
        }
        try:
            from storage.sqlite_local import NormalizedStore
            store = NormalizedStore(db.DB_PATH)
            counts = store.count()
            kpis["totalListings"] = counts.get("total", 0)
            kpis["activeListings"] = counts.get("total", 0)
            kpis["newRequests"] = counts.get("new_today", 0)
        except Exception:
            pass
        try:
            import lead_intel
            leads = lead_intel.leads(conn, limit=1)
            kpis["hotLeads"] = leads.get("total", 0)
        except Exception:
            pass
        try:
            import opportunity
            market = opportunity.market_snapshot(conn)
            kpis["marketSpread"] = market.get("price_spread")
        except Exception:
            pass
        return kpis

    @staticmethod
    def _dashboard_filters(conn) -> dict:
        """Filter dropdown options for the dashboard.

        Cities and property types come from the normalized store; deal types
        are a fixed enum. Each query is guarded so a missing table degrades
        to an empty list rather than a 500.
        """
        filters = {
            "cities": [],
            "propertyTypes": [],
            "priceRanges": [],
            "dealTypes": ["forsale", "rent"],
        }
        try:
            cities = [r[0] for r in conn.execute(
                "SELECT DISTINCT city FROM normalized_listings "
                "WHERE city IS NOT NULL AND city <> '' "
                "ORDER BY city LIMIT 100"
            ).fetchall()]
            filters["cities"] = cities
        except sqlite3.Error:
            pass
        try:
            property_types = [r[0] for r in conn.execute(
                "SELECT DISTINCT property_type FROM normalized_listings "
                "WHERE property_type IS NOT NULL AND property_type <> '' "
                "ORDER BY property_type LIMIT 50"
            ).fetchall()]
            filters["propertyTypes"] = property_types
        except sqlite3.Error:
            pass
        try:
            price_ranges = [dict(r) for r in conn.execute(
                """SELECT MIN(price) min_price, MAX(price) max_price,
                          ROUND(AVG(price)) avg_price
                   FROM normalized_listings
                   WHERE price IS NOT NULL""").fetchall()]
            if price_ranges and price_ranges[0].get("min_price") is not None:
                filters["priceRanges"] = price_ranges[0]
        except sqlite3.Error:
            pass
        return filters

    @staticmethod
    def _bootstrap(conn) -> dict:
        def distinct(col, limit=400):
            return [r[0] for r in conn.execute(
                f"""SELECT {col} FROM plans WHERE {col} IS NOT NULL AND {col} <> ''
                    GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT ?""", (limit,))]

        stats = db.stats(conn)
        # NOTE: no SUM(area_dunam) here on purpose - the source has corrupt
        # outliers (a single plan claiming 125M dunam, ~6x all of Israel), so
        # a total would be garbage. Housing units are bucket-120 only (יח"ד).
        totals = conn.execute(
            """SELECT COALESCE(SUM(housing_units),0),
                      COALESCE(SUM(CASE WHEN station LIKE '%אישור%'
                                        THEN housing_units END),0)
               FROM plans""").fetchone()
        runs = conn.execute(
            """SELECT id, mode, plans_seen, plans_new, status_changes,
                      notifications, ok, started_at, finished_at, error
               FROM sync_runs ORDER BY id DESC LIMIT 10""").fetchall()
        return {
            "stats": stats,
            "housing_units": int(totals[0]),
            "housing_units_approved": int(totals[1]),
            "db_path": str(db.DB_PATH),
            "jurisdictions": distinct("jurisdiction_name"),
            "counties": distinct("county_name", 40),
            "statuses": distinct("short_status", 60),
            "subtypes": distinct("entity_subtype", 60),
            "runs": [dict(r) for r in runs],
            "milestones": [{"field": f, "label": l} for f, l in report.MILESTONES],
            "kpis": Handler._dashboard_kpis(conn),
            "filters": Handler._dashboard_filters(conn),
        }

    @staticmethod
    def _filter_sql(q: dict) -> tuple[str, list]:
        """Same filter semantics as report.py, driven by query-string params."""
        args = argparse.Namespace(
            jurisdiction=q.get("jurisdiction") or None,
            county=q.get("county") or None,
            status=q.get("status") or None,
            text=q.get("text") or None,
            since=q.get("since") or None,
            min_units=as_int(q.get("min_units"), "min_units"),
            with_geometry=q.get("with_geometry") == "1",
        )
        where, params = report._filters(args)
        if q.get("subtype"):
            where += " AND entity_subtype = ?"
            params.append(q["subtype"])
        # map-tab targeted search (mirrors the official site's search modes)
        if q.get("min_dunam"):
            where += " AND area_dunam >= ?"
            params.append(as_float(q["min_dunam"], "min_dunam"))
        if q.get("landuse"):
            where += " AND landuse LIKE ?"
            params.append(f"%{q['landuse']}%")
        return where, params

    def _plans(self, conn, q: dict) -> dict:
        where, params = self._filter_sql(q)
        sort = q.get("sort") if q.get("sort") in SORTABLE else "last_update"
        order = "ASC" if (q.get("order") or "").lower() == "asc" else "DESC"
        limit = as_int(q.get("limit"), "limit", 50, low=1, high=500)
        offset = as_int(q.get("offset"), "offset", 0, low=0)

        total = conn.execute(f"SELECT COUNT(*) FROM plans WHERE {where}",
                             params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT object_id, pl_number, pl_name, jurisdiction_name,
                       county_name, entity_subtype, station, short_status,
                       area_dunam, housing_units, last_update, depositing_date, pl_url
                FROM plans WHERE {where}
                ORDER BY {sort} IS NULL, {sort} {order}, object_id {order}
                LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "rows": [dict(r) for r in rows]}

    @staticmethod
    def _with_parcel_refs(rows: list[dict]) -> list[dict]:
        """Best-effort גוש/חלקה for each plan row, so the webapp can show it
        inline instead of sending end users to the government plan page just
        to learn which parcel a plan sits on.

        Plans carry a bbox (min/max lon/lat), not a registered parcel - so
        this resolves the bbox centre against GovMap's cadastre layer
        (`govmap_client.parcel_at_point`, the same lookup `/api/parcel` uses
        for a typed גוש/חלקה). Capped at 8 rows and best-effort: a lookup
        failure (no network, point over unregistered land) just leaves
        gush/helka unset rather than failing the whole search.
        """
        import govmap_client
        for row in rows[:8]:
            min_lon, min_lat = row.pop("min_lon", None), row.pop("min_lat", None)
            max_lon, max_lat = row.pop("max_lon", None), row.pop("max_lat", None)
            if None in (min_lon, min_lat, max_lon, max_lat):
                continue
            try:
                hit = govmap_client.parcel_at_point(
                    (min_lat + max_lat) / 2, (min_lon + max_lon) / 2, timeout=8)
            except Exception:
                hit = None
            if hit:
                row["gush"] = hit.get("gush")
                row["helka"] = hit.get("helka")
        for row in rows[8:]:
            row.pop("min_lon", None), row.pop("min_lat", None)
            row.pop("max_lon", None), row.pop("max_lat", None)
        return rows

    @staticmethod
    def _plan_summary(conn, object_id: int) -> dict:
        """A lightweight, end-user-facing plan dossier for the webapp's
        in-app detail view - the alternative to sending visitors to the
        government plan page (pl_url) in a new tab. Deliberately narrower
        than `_plan()`: no geometry (can run ~1.6MB), no operator
        subscriptions - just what a home buyer/investor wants to read.
        """
        row = conn.execute(
            """SELECT object_id, pl_number, pl_name, jurisdiction_name,
                      plan_area_name, station, short_status, area_dunam,
                      landuse, objectives, housing_units, last_update,
                      depositing_date, open_date, pl_url,
                      min_lon, min_lat, max_lon, max_lat
               FROM plans WHERE object_id=?""", (object_id,)).fetchone()
        if row is None:
            return {"error": f"no plan {object_id}"}
        plan = dict(row)
        [plan] = Handler._with_parcel_refs([plan])
        log = conn.execute(
            """SELECT old_station, new_station, detected_at FROM plan_status_log
               WHERE object_id=? ORDER BY id""", (object_id,)).fetchall()
        import urban_renewal_client as ur
        return {"plan": plan, "log": [dict(r) for r in log],
                "renewal": ur.for_plan(conn, plan.get("pl_number"))}

    @staticmethod
    def _plan(conn, object_id: int) -> dict:
        row = conn.execute("SELECT * FROM plans WHERE object_id=?",
                           (object_id,)).fetchone()
        if row is None:
            return {"error": f"no plan {object_id}"}
        plan = dict(row)
        geometry = json.loads(plan.pop("geometry_json")) if plan.get("geometry_json") else None
        log = conn.execute(
            """SELECT old_station, new_station, detected_at FROM plan_status_log
               WHERE object_id=? ORDER BY id""", (object_id,)).fetchall()
        subs = conn.execute(
            """SELECT s.id, s.label, s.gush, s.helka, m.match_method, m.matched_at
               FROM subscription_plan_matches m
               JOIN subscriptions s ON s.id = m.subscription_id
               WHERE m.object_id=?""", (object_id,)).fetchall()
        import urban_renewal_client as ur
        return {"plan": plan, "geometry": geometry,
                "log": [dict(r) for r in log],
                "subscriptions": [dict(r) for r in subs],
                "renewal": ur.for_plan(conn, plan.get("pl_number"))}

    @staticmethod
    def _at(conn, q: dict) -> dict:
        import sync
        from shapely.geometry import Point

        lat = required_float(q, "lat")
        lon = required_float(q, "lon")
        radius = as_float(q.get("radius"), "radius")
        # Geometry is only needed when the caller draws the hits; the parcel
        # view and the map popup do not, and sending it cost ~1.6 MB per call.
        with_geom = q.get("geometry") == "1"
        pad_lon, pad_lat = sync._radius_pads(radius, lat)
        candidates = db.plans_covering_point(conn, lon, lat,
                                             pad_lon=pad_lon, pad_lat=pad_lat)
        point = Point(lon, lat)

        hits = []
        for plan in candidates:
            ok, method = sync._point_hits_plan(point, plan, radius)
            if not ok:
                continue
            geom = sync._load_geometry(plan)
            distance = 0.0 if method == "point_in_polygon" else sync._distance_m(geom, point, lat)
            row = {k: plan[k] for k in (
                "object_id", "pl_number", "pl_name", "jurisdiction_name",
                "short_status", "station", "area_dunam", "housing_units",
                "last_update", "pl_url")}
            row.update(match_method=method, distance_m=round(distance, 1))
            if with_geom:
                row["geometry"] = json.loads(plan["geometry_json"])
            hits.append(row)
        hits.sort(key=lambda r: r["distance_m"])
        return {"lat": lat, "lon": lon, "radius": radius,
                "candidates": len(candidates), "hits": hits}

    #: National plan numbers: 507-0584706 / 101-1472943. Checked FIRST, because
    #: "507-0584706" also matches the bare NNN-NNN parcel shape below and would
    #: otherwise be misread as "גוש 507 חלקה 05847".
    _PLAN_RE = re.compile(r"\b\d{3}-\d{6,8}\b")
    #: Explicit parcel wording - unambiguous, wins even inside longer text.
    _PARCEL_WORDED_RE = re.compile(
        r"גוש\s*(\d{1,6})\D{0,12}?(\d{1,5})")
    #: Bare "6941/23" or "6941-23". Only consulted when the text is NOT a plan
    #: number; the helka side is capped at 4 digits since real חלקה numbers are
    #: small, which keeps 6-8 digit plan tails from matching.
    _PARCEL_BARE_RE = re.compile(r"\b(\d{3,6})\s*[/\\-]\s*(\d{1,4})\b")

    @classmethod
    def _parse_parcel(cls, text: str):
        """(gush, helka) if the text names a parcel, else None."""
        worded = cls._PARCEL_WORDED_RE.search(text)
        if worded:
            return worded.group(1), worded.group(2)
        if cls._PLAN_RE.search(text):
            return None          # it is a plan number, not גוש/חלקה
        bare = cls._PARCEL_BARE_RE.search(text)
        return (bare.group(1), bare.group(2)) if bare else None

    def _search(self, conn, q: dict) -> dict:
        """
        One box, many sources. Classifies the query and fans out:
          * parcel pattern      -> the full parcel view (GovMap + registry + plans)
          * plan number         -> that plan
          * everything else     -> plans by name, localities, addresses (GovMap),
                                   contractors and appraisers in that locality
        Returns a `groups` list so the UI can render whatever came back.
        """
        text = (q.get("q") or "").strip()
        if not text:
            return {"query": "", "groups": []}

        groups: list[dict] = []
        parcel_hit = self._parse_parcel(text)
        plan_hit = self._PLAN_RE.search(text)

        # 1. a parcel reference wins - it is the most specific thing a user types
        if parcel_hit:
            gush, helka = parcel_hit
            groups.append({"kind": "parcel", "title": f"גוש {gush} חלקה {helka}",
                           "gush": gush, "helka": helka})

        # 2. exact-ish plan match
        if plan_hit:
            rows = conn.execute(
                """SELECT object_id, pl_number, pl_name, jurisdiction_name,
                          short_status, station, housing_units, area_dunam, pl_url,
                          min_lon, min_lat, max_lon, max_lat
                   FROM plans WHERE pl_number LIKE ? LIMIT 10""",
                (f"%{plan_hit.group(0)}%",)).fetchall()
            if rows:
                groups.append({"kind": "plans", "title": "תוכניות לפי מספר",
                               "rows": self._with_parcel_refs([dict(r) for r in rows])})

        # 3. plans by free text. Every word must appear somewhere in the plan
        #    (name / number / objectives / jurisdiction), in any order - a plain
        #    LIKE on the whole phrase misses "פינוי בינוי התחדשות" entirely.
        words = [w for w in re.split(r"\s+", text) if len(w) > 1][:6]
        if words:
            clause = " AND ".join(
                ["(pl_name LIKE ? OR pl_number LIKE ? OR objectives LIKE ? "
                 "OR jurisdiction_name LIKE ?)"] * len(words))
            params = [p for w in words for p in (f"%{w}%",) * 4]
            rows = conn.execute(
                f"""SELECT object_id, pl_number, pl_name, jurisdiction_name,
                           short_status, station, housing_units, area_dunam, pl_url,
                           min_lon, min_lat, max_lon, max_lat
                    FROM plans WHERE {clause}
                    ORDER BY last_update DESC LIMIT 12""", params).fetchall()
            if rows:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM plans WHERE {clause}", params).fetchone()[0]
                groups.append({"kind": "plans", "title": f"תוכניות ({total:,} תואמות)",
                               "rows": self._with_parcel_refs([dict(r) for r in rows]),
                               "total": total})

        # 4. is it a locality? if so add its context + who works there
        import open_data_client as od
        loc = None
        try:
            loc = od.locality(conn, text)
        except Exception:
            loc = None
        if loc:
            plans_here = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(housing_units),0) FROM plans "
                "WHERE jurisdiction_name LIKE ?", (f"%{loc['name']}%",)).fetchone()
            groups.append({"kind": "locality", "title": f"ישוב: {loc['name']}",
                           "locality": loc, "plans": plans_here[0],
                           "units": plans_here[1]})
            try:
                cons = od.contractors_in(conn, loc["name"], limit=12)
                if cons:
                    groups.append({"kind": "contractors",
                                   "title": f"קבלנים רשומים ב{loc['name']}",
                                   "rows": cons})
                apps = od.appraisers_in(conn, loc["name"], limit=10)
                if apps:
                    groups.append({"kind": "appraisers",
                                   "title": f"שמאי מקרקעין ב{loc['name']}",
                                   "rows": apps})
            except Exception:
                pass

        # 5. GovMap: addresses, settlements, parcels by free text
        if not parcel_hit:
            try:
                import govmap_client
                hits = govmap_client.search(text)[:8]
                if hits:
                    groups.append({"kind": "places", "title": "מקומות וכתובות (GovMap)",
                                   "rows": hits})
            except Exception as exc:
                groups.append({"kind": "note",
                               "title": f"חיפוש GovMap נכשל: {exc}"})
        return {"query": text, "groups": groups}

    #: Text classifiers for the property finder's "kind" filter.
    #: TAMA 38 needs the SPELLINGS, not a bare "38": matching '%38%' pulled in
    #: 550 plans, most of them street numbers ("חפץ חיים 38") or plan numbers
    #: ("רח/מק/38/1050"). The gershayim form תמ"א is the common one in practice.
    _TAMA38 = " OR ".join(
        f"{col} LIKE '%{pat}%'"
        for col in ("p.pl_name", "p.objectives")
        for pat in ("תמא/38", "תמא 38", 'תמ"א 38', 'תמ"א/38', "38א")
    )
    _KIND_CLAUSES = {
        "pinui": "(p.pl_name LIKE '%פינוי%בינוי%' OR p.objectives LIKE '%פינוי בינוי%'"
                 " OR p.pl_name LIKE '%פינוי ובינוי%')",
        "tama38": f"({_TAMA38})",
        "renewal": "(p.pl_name LIKE '%התחדשות%' OR p.objectives LIKE '%התחדשות עירונית%')",
        "dira": "p.entity_subtype LIKE '%מועדפת לדיור%'",
    }

    @staticmethod
    def _finance(conn, q: dict) -> dict:
        """The cost of the money, and what it does to one price.

        `price` turns the page into a calculator: pass it and the payment,
        deposit and required income come back for that figure at the market
        rate, with `ltv` and `years` overridable so a reader can put their own
        terms in rather than argue with the defaults.
        """
        import finance_client
        import market_client

        out = finance_client.snapshot(conn)
        out["costs"] = market_client.cost_trends(conn)
        price = as_int(q.get("price"), "price", 0, low=0, high=500_000_000)
        rate = (out.get("mortgage") or {}).get("rate")
        if price and rate is not None:
            ltv = as_int(q.get("ltv_pct"), "ltv_pct", 75, low=10, high=100)
            years = as_int(q.get("years"), "years", finance_client.DEFAULT_TERM_YEARS,
                           low=4, high=40)
            out["quote"] = finance_client.affordability(
                price, rate, ltv=ltv / 100.0, years=years,
                pti=(out.get("mortgage") or {}).get("pti_actual"))
        return out

    @staticmethod
    def _amenities(conn, q: dict) -> dict:
        """What is near a point: stops by tier, and schools.

        Without `lat`/`lon` it reports what the layer holds, which is how the
        sources tab shows whether the import has ever run.
        """
        import amenity_client
        import listing_amenities

        out = {"counts": amenity_client.counts(conn),
               "radii": {"rail_max_m": listing_amenities.RAIL_MAX_M,
                         "bus_m": listing_amenities.BUS_RADIUS_M,
                         "school_m": listing_amenities.SCHOOL_RADIUS_M},
               "scored": False,
               "note": ("מרחקים בקו אווירי, לתצוגה וסינון בלבד — נמדדו מול "
                        "61,478 מודעות ולא נמצאה השפעה עקבית על מחיר או על "
                        "משך שיווק, ולכן אינם רכיב בניקוד")}
        try:
            lat = float(q.get("lat"))
            lon = float(q.get("lon"))
        except (TypeError, ValueError):
            return out
        row = {"lat": lat, "lon": lon}
        listing_amenities.annotate([row])
        out["at"] = {k: v for k, v in row.items() if k != "lat" and k != "lon"}
        return out

    @staticmethod
    def _rentals(conn, q: dict) -> dict:
        """The rent benchmark behind every yield, and the sample under it.

        Exposed so a yield can be argued with. A gross yield is a ratio of two
        numbers and only one of them is on the ad; this endpoint is the other
        one, with the depth of the sample it came from, because "3.2%" without
        "from 45 rentals in Haifa" is a number nobody can check.
        """
        import listing_yield
        import onmap_rent

        index = listing_yield.build_index()
        out = {
            "counts": onmap_rent.counts(conn),
            "harvest": {k: v for k, v in onmap_rent._STATE.items()},
            "gross_only": True,
            "note": ("תשואה ברוטו בלבד — לא מנוכים ניהול, תקופות ריקות, "
                     "תחזוקה, מס רכישה או ועד בית. נטו בישראל נמוך בדרך כלל "
                     "ב-1 עד 1.5 נקודות"),
        }
        if index is None:
            out["cities"] = []
            return out
        out["size_elasticity"] = round(index.beta, 3)
        out["national_ppm_month"] = (round(index.national, 1)
                                     if index.national else None)
        out["min_city_sample"] = listing_yield.MIN_CITY_RENTS
        out["cities"] = sorted(
            ({"city": city, "rent_per_m2_month": round(entry["ppm"], 1),
              "sample": entry["sample"],
              # The size range the sample covers, because it is the difference
              # between a yield that is scored and one that is only shown.
              "area_range": [round(entry["min_area"]), round(entry["max_area"])]}
             for city, entry in index.cities.items()),
            key=lambda r: -r["rent_per_m2_month"])
        price = as_int(q.get("price"), "price", 0, low=0, high=500_000_000)
        area = as_float(q.get("area"), "area")
        city = (q.get("city") or "").strip()
        if price and area and city:
            # A caller asking "what would an 85 m² flat in Beer Sheva yield"
            # has already said it is a dwelling. Overridable, so the same
            # endpoint can be asked about a cottage.
            row = {"price": price, "sqm": area, "city": city,
                   "property_type": (q.get("property_type") or "דירה").strip()}
            listing_yield.annotate([row], index)
            out["quote"] = {k: v for k, v in row.items()
                            if k not in ("price", "sqm", "city")}
        return out

    @staticmethod
    def _renewal(conn, q: dict) -> dict:
        """Declared urban-renewal complexes, filtered."""
        import urban_renewal_client as ur
        return {
            "summary": ur.summary(conn),
            "rows": ur.search(
                conn,
                locality=q.get("locality", ""), track=q.get("track", ""),
                status=q.get("status", ""), text=q.get("q", ""),
                min_added=as_int(q.get("min_added"), "min_added"),
                limit=as_int(q.get("limit"), "limit", 100, low=1, high=500),
                offset=as_int(q.get("offset"), "offset", 0, low=0),
            ),
        }

    def _find(self, conn, q: dict) -> dict:
        """
        איתור נכסים - the deal-finder. One query across everything we hold:
        plan status/size/units, urban-renewal declaration and its added units,
        and (optionally) registry ownership type for the covering parcels.

        Every filter is optional; they AND together. Sorted by whichever signal
        the caller says matters (`sort`).
        """
        import urban_renewal_client as ur

        clauses, params = ["p.geometry_json IS NOT NULL"], []
        if q.get("locality"):
            clauses.append("p.jurisdiction_name LIKE ?")
            params.append(f"%{q['locality']}%")
        if q.get("county"):
            clauses.append("p.county_name LIKE ?")
            params.append(f"%{q['county']}%")
        if q.get("status"):
            clauses.append("(p.short_status LIKE ? OR p.station LIKE ?)")
            params += [f"%{q['status']}%"] * 2
        if q.get("landuse"):
            clauses.append("p.landuse LIKE ?")
            params.append(f"%{q['landuse']}%")
        if q.get("min_units"):
            clauses.append("p.housing_units >= ?")
            params.append(as_int(q["min_units"], "min_units"))
        if q.get("max_units"):
            clauses.append("p.housing_units <= ?")
            params.append(as_int(q["max_units"], "max_units"))
        if q.get("min_dunam"):
            clauses.append("p.area_dunam >= ?")
            params.append(as_float(q["min_dunam"], "min_dunam"))
        if q.get("max_dunam"):
            clauses.append("p.area_dunam <= ?")
            params.append(as_float(q["max_dunam"], "max_dunam"))
        if q.get("since"):
            clauses.append("p.last_update >= ?")
            params.append(q["since"])
        if q.get("text"):
            clauses.append("(p.pl_name LIKE ? OR p.objectives LIKE ? OR p.pl_number LIKE ?)")
            params += [f"%{q['text']}%"] * 3

        # Renewal filters need the complex table. Silently dropping them would
        # be worse than failing: asking for "declared complexes only" and
        # getting every plan back reads as a correct answer but is not one.
        has_renewal = ur.count_local(conn) > 0
        renewal_only = q.get("renewal") == "1"
        renewal_filters = [k for k in ("renewal", "track", "min_added") if q.get(k)]
        if renewal_filters and not has_renewal:
            return {"total": 0, "rows": [], "has_renewal": False,
                    "renewal_only": renewal_only,
                    "unavailable_filters": renewal_filters,
                    "note": "מאגר ההתחדשות העירונית לא יובא, ולכן הסינון לפי "
                            "מתחמים לא ניתן להפעלה. ייבא אותו ב'מקורות נתונים'."}
        if renewal_only:
            clauses.append("u.mitham_id IS NOT NULL")
        if q.get("track"):
            clauses.append("u.track = ?")
            params.append(q["track"])
        if q.get("min_added"):
            clauses.append("CAST(u.units_added AS INTEGER) >= ?")
            params.append(as_int(q["min_added"], "min_added"))

        kind_clause = self._KIND_CLAUSES.get(q.get("kind"))
        if kind_clause:
            clauses.append(kind_clause)

        sort = {
            "units": "p.housing_units DESC",
            "added": "CAST(u.units_added AS INTEGER) DESC",
            "area": "p.area_dunam DESC",
            "updated": "p.last_update DESC",
        }.get(q.get("sort", "updated"), "p.last_update DESC")
        limit = as_int(q.get("limit"), "limit", 60, low=1, high=300)
        offset = as_int(q.get("offset"), "offset", 0, low=0)

        # With no complex table the renewal columns must still resolve, so join
        # a one-row all-NULL stub that never matches.
        join = ("LEFT JOIN urban_renewal u ON u.plan_number = p.pl_number"
                if has_renewal else
                "LEFT JOIN (SELECT NULL mitham_id, NULL units_added,"
                " NULL units_existing, NULL units_planned, NULL track,"
                " NULL declared_at, NULL permits, NULL name) u ON 1=0")

        where = " AND ".join(clauses)
        total = conn.execute(
            f"SELECT COUNT(*) FROM plans p {join} WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT p.object_id, p.pl_number, p.pl_name, p.jurisdiction_name,
                       p.county_name, p.short_status, p.station, p.entity_subtype,
                       p.area_dunam, p.housing_units, p.landuse, p.last_update,
                       p.pl_url, p.min_lon, p.min_lat, p.max_lon, p.max_lat,
                       u.mitham_id, u.name AS mitham_name, u.units_existing,
                       u.units_added, u.units_planned, u.track, u.declared_at,
                       u.permits
                FROM plans p {join}
                WHERE {where}
                ORDER BY {sort}, p.object_id
                LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()

        # Note: registry ownership is deliberately NOT joined here. It is keyed
        # by גוש/חלקה, and a plan polygon covers many parcels - there is no
        # single "ownership of a plan". Use the גוש/חלקה tab for that.
        return {"total": total, "rows": [dict(r) for r in rows],
                "limit": limit, "offset": offset,
                "has_renewal": has_renewal, "renewal_only": renewal_only}

    @staticmethod
    def _plan_detail(conn, q: dict) -> dict:
        """
        The plan's CONTENTS, not just its outline: land-use cells, designation
        mix, and point/line/polygon entities.

        `fetch=1` pulls it live from the Xplan service on demand, because the
        national land-use layer is far too large to import eagerly for every
        plan. Once fetched it is cached in `plan_landuse` / `plan_entities`.
        """
        import plan_detail_client as pdc

        mp_id = as_int(q.get("mp_id"), "mp_id")
        pl_number = (q.get("pl_number") or "").strip()
        if not mp_id and not pl_number:
            raise BadRequest("mp_id or pl_number is required")
        if not mp_id:
            row = conn.execute(
                "SELECT mp_id FROM plans WHERE pl_number = ? LIMIT 1",
                (pl_number,)).fetchone()
            if row is None:
                return {"error": f"no plan {pl_number}"}
            mp_id = row["mp_id"]
        if not pl_number:
            row = conn.execute(
                "SELECT pl_number FROM plans WHERE mp_id = ? LIMIT 1",
                (mp_id,)).fetchone()
            pl_number = row["pl_number"] if row else None

        fetched = None
        cells = pdc.landuse_for_plan(conn, mp_id=mp_id)
        if not cells and q.get("fetch") == "1":
            fetched = pdc.fetch_plan(conn, mp_id)
            cells = pdc.landuse_for_plan(conn, mp_id=mp_id)

        return {
            "mp_id": mp_id, "pl_number": pl_number,
            "cached": bool(cells) and fetched is None,
            "fetched": fetched,
            "landuse": pdc.landuse_mix(conn, mp_id=mp_id),
            "cells": cells,
            "entities": pdc.entities_for_plan(conn, mp_id=mp_id),
            "document_url": pdc.mavat_plan_url(pl_number),
            "note": None if cells else
                    "תוכן התכנית לא נשלף עדיין. הוסף fetch=1 כדי לשלוף מהשירות.",
        }

    def _document_file(self, conn, q: dict) -> None:
        """
        Stream a ruling PDF through PlanWatch so it renders INSIDE the app.

        The point is that the user never leaves the system: the browser gets the
        PDF from 127.0.0.1 and shows it in an <iframe>, instead of being sent to
        openapi.gov.il. `inline` (not `attachment`) is what makes the browser
        render rather than download.

        Only ids already present in `appraisals` are fetchable - the URL comes
        from the database, never from the query string, so this cannot be used
        as an open proxy to fetch arbitrary hosts.
        """
        appraisal_id = as_int(q.get("appraisal_id"), "appraisal_id")
        if not appraisal_id:
            raise BadRequest("appraisal_id is required")
        row = conn.execute("SELECT link FROM appraisals WHERE id=?",
                           (appraisal_id,)).fetchone()
        if row is None or not row["link"]:
            return self._send({"error": f"no document for {appraisal_id}"}, 404)

        import document_client as dc
        try:
            resp = dc._sess().get(row["link"], timeout=(20, 180))
            resp.raise_for_status()
        except Exception as exc:
            return self._send({"error": f"upstream fetch failed: {exc}"}, 502)
        if resp.content[:5] != b"%PDF-":
            return self._send({"error": "upstream did not return a PDF"}, 502)

        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(resp.content)))
        self.send_header("Content-Disposition",
                         f'inline; filename="ruling-{appraisal_id}.pdf"')
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(resp.content)

    @staticmethod
    def _document_text(conn, q: dict) -> dict:
        """
        The ruling's extracted text, for reading in-app without a PDF viewer.
        Fetches and parses on demand if not already stored.
        """
        import document_client as dc

        appraisal_id = as_int(q.get("appraisal_id"), "appraisal_id")
        if not appraisal_id:
            raise BadRequest("appraisal_id is required")
        doc = dc.doc_for_appraisal(conn, appraisal_id)
        if doc is None or q.get("refresh") == "1":
            doc = dc.fetch_one(conn, appraisal_id, force=q.get("refresh") == "1")
            doc = dc.doc_for_appraisal(conn, appraisal_id) or doc
        appraisal = conn.execute(
            """SELECT header, committee, decision_date, appraisal_type,
                      appraiser, block_raw, plot_raw
               FROM appraisals WHERE id=?""", (appraisal_id,)).fetchone()
        return {"appraisal_id": appraisal_id,
                "appraisal": dict(appraisal) if appraisal else None,
                "document": doc,
                "pdf_url": f"/api/document-file?appraisal_id={appraisal_id}"}

    @staticmethod
    def _tenders(conn, q: dict) -> dict:
        """
        RAMI land tenders - actual state-sold property, with prices.
        `open=1` restricts to tenders still accepting bids.
        """
        import tenders_client as tc

        if q.get("open") == "1" and not q.get("locality"):
            rows = tc.open_now(conn, limit=as_int(q.get("limit"), "limit", 60,
                                                  low=1, high=300))
            return {"total": len(rows), "rows": rows, "only_open": True}
        return tc.search(
            conn, locality=q.get("locality", ""),
            only_open=q.get("open") == "1",
            min_units=as_int(q.get("min_units"), "min_units"),
            max_price=as_float(q.get("max_price_m2"), "max_price_m2"),
            purpose=as_int(q.get("purpose"), "purpose"),
            since=q.get("since") or None,
            limit=as_int(q.get("limit"), "limit", 100, low=1, high=500),
            offset=as_int(q.get("offset"), "offset", 0, low=0))

    @staticmethod
    def _documents(conn, q: dict) -> dict:
        """
        Ruling documents (PDF) behind the appraisals, plus the cross-validation
        report against the CSV metadata.

        `appraisal_id` -> that one document. `validate=1` -> the agreement
        report. Otherwise coverage counts and the amount distribution.
        """
        import document_client as dc

        appraisal_id = as_int(q.get("appraisal_id"), "appraisal_id")
        if appraisal_id:
            doc = dc.doc_for_appraisal(conn, appraisal_id)
            if doc is None:
                return {"error": f"no document stored for appraisal {appraisal_id}",
                        "hint": "ייבא מסמכים דרך 'מקורות נתונים' (documents)"}
            return {"document": doc}
        if q.get("validate") == "1":
            return dc.verify_against_csv(conn)
        return {"counts": dc.counts(conn), "amounts": dc.amount_stats(conn)}

    @staticmethod
    def _data_quality(conn) -> dict:
        """
        Cross-source agreement report. Every number here is a JOIN that either
        lands or does not; a falling rate means a feed changed shape.
        """
        import document_client as dc

        def one(sql, *params):
            return conn.execute(sql, params).fetchone()[0]

        def has(table):
            return bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone())

        SQ = ("replace(replace(replace({},' ',''),char(9),''),char(160),'')")
        checks = []

        if has("urban_renewal"):
            total = one("SELECT COUNT(*) FROM urban_renewal WHERE plan_number IS NOT NULL")
            linked = one("SELECT COUNT(*) FROM urban_renewal u WHERE EXISTS"
                         " (SELECT 1 FROM plans p WHERE p.pl_number_sq = "
                         + SQ.format("u.plan_number") + ")")
            checks.append({"check": "מתחמי התחדשות → תכניות", "matched": linked,
                           "total": total, "key": "pl_number (מנורמל)"})
        if has("rami_inventory"):
            total = one("SELECT COUNT(*) FROM rami_inventory WHERE plan_number IS NOT NULL")
            linked = one("SELECT COUNT(*) FROM rami_inventory r WHERE EXISTS"
                         " (SELECT 1 FROM plans p WHERE p.pl_number_sq = "
                         + SQ.format("r.plan_number") + ")")
            checks.append({"check": 'מלאי תכנוני רמ"י → תכניות', "matched": linked,
                           "total": total, "key": "pl_number (מנורמל)"})
        if has("appraisal_parcels"):
            total = one("SELECT COUNT(*) FROM appraisal_parcels WHERE helka IS NOT NULL")
            linked = one("SELECT COUNT(*) FROM appraisal_parcels p WHERE p.helka"
                         " IS NOT NULL AND EXISTS (SELECT 1 FROM tabu_assets t"
                         " WHERE t.gush=p.gush AND t.helka=p.helka)")
            checks.append({"check": "חלקות שמאות → פנקסי המקרקעין",
                           "matched": linked, "total": total, "key": "גוש+חלקה"})
        if has("building_progress"):
            # Denominator is rows that HAVE both a gush and a helka. Counting
            # rows where the source published no parcel at all made this look
            # 34% broken when the join itself was fine.
            total = one("SELECT COUNT(*) FROM building_progress"
                        " WHERE helka IS NOT NULL AND gush IS NOT NULL")
            linked = one("SELECT COUNT(*) FROM building_progress b"
                         " WHERE b.helka IS NOT NULL AND b.gush IS NOT NULL"
                         " AND EXISTS (SELECT 1 FROM tabu_assets t"
                         " WHERE t.gush=b.gush AND t.helka=b.helka)")
            checks.append({"check": "התקדמות בנייה → פנקסי המקרקעין",
                           "matched": linked, "total": total,
                           "key": "גוש+חלקה (רק שורות עם שניהם)"})
        if has("plan_landuse"):
            total = one("SELECT COUNT(*) FROM plan_landuse")
            # CAST is load-bearing: plans.mp_id is TEXT but plan_landuse.mp_id
            # is INTEGER, and SQLite will not use an index across mismatched
            # affinity. Without it this degrades to SCAN idx_plans_mp_id once
            # per landuse row - 50s of the endpoint's runtime, for the same
            # answer the indexed SEARCH returns in milliseconds.
            orphan = one("SELECT COUNT(*) FROM plan_landuse l WHERE NOT EXISTS"
                         " (SELECT 1 FROM plans p"
                         " WHERE p.mp_id = CAST(l.mp_id AS TEXT))")
            checks.append({"check": "תאי שטח → תכניות", "matched": total - orphan,
                           "total": total, "key": "mp_id"})

        for item in checks:
            item["pct"] = (round(item["matched"] / item["total"] * 100, 1)
                           if item["total"] else None)

        # Independent geometric cross-check: the plan's own area_dunam (layer 1)
        # versus the summed area of its land-use cells (layer 4).
        area = []
        if has("plan_landuse"):
            for row in conn.execute(
                """SELECT p.pl_number, p.area_dunam,
                          ROUND(SUM(l.shape_area_m2)/1000.0, 2) cells_dunam,
                          COUNT(*) cells
                   FROM plan_landuse l
                   JOIN plans p ON p.mp_id = CAST(l.mp_id AS TEXT)
                   WHERE p.area_dunam > 0 GROUP BY 1, 2 LIMIT 25"""):
                item = dict(row)
                item["diff_pct"] = round(
                    abs(item["area_dunam"] - item["cells_dunam"])
                    / item["area_dunam"] * 100, 2)
                area.append(item)

        return {"joins": checks, "area_agreement": area,
                "documents": dc.verify_against_csv(conn) if has("appraisal_docs") else None,
                "note": "כל שורה כאן היא JOIN שמצליח או נכשל. ירידה באחוז "
                        "פירושה שמקור שינה מבנה."}

    @staticmethod
    def _appraisals(conn, q: dict) -> dict:
        """
        Decisive appraisals (שמאות מכריעה). With gush -> that parcel's history;
        without -> committee activity ranking.
        """
        import appraisal_client as ac

        gush = (q.get("gush") or "").strip()
        if gush:
            rows = ac.appraisals_for_parcel(
                conn, gush, (q.get("helka") or "").strip() or None,
                limit=as_int(q.get("limit"), "limit", 50, low=1, high=300))
            return {"gush": gush, "helka": q.get("helka"),
                    "total": len(rows), "rows": rows,
                    "note": "הכרעת שמאי מכריע = מחלוקת על היטל השבחה, כלומר "
                            "עדות מתועדת לכך שתכנית יצרה כאן ערך."}
        return {"committees": ac.appraisal_activity(
            conn, limit=as_int(q.get("limit"), "limit", 40, low=1, high=200))}

    @staticmethod
    def _opportunities(conn, q: dict) -> dict:
        """Ranked urban-renewal candidates. See opportunity.py for the model."""
        import opportunity
        return opportunity.renewal_opportunities(
            conn,
            locality=q.get("locality", ""),
            track=q.get("track", ""),
            stage=q.get("stage", ""),
            min_existing=as_int(q.get("min_existing"), "min_existing"),
            min_multiplier=as_float(q.get("min_multiplier"), "min_multiplier"),
            # `max_permits` must distinguish "not supplied" from "=0", and 0 is
            # the whole point of the filter (window still open), so as_int's
            # default=None is load-bearing here.
            max_permits=as_int(q.get("max_permits"), "max_permits"),
            limit=as_int(q.get("limit"), "limit", 60, low=1, high=300),
            offset=as_int(q.get("offset"), "offset", 0, low=0),
        )

    @staticmethod
    def _transactions(q: dict) -> dict:
        """Concluded sale transactions (what was paid), read-only.

        The counterpart to /api/deals: that endpoint scores *asking* prices,
        this one reports prices actually paid, from the official CC-BY
        dataset. Residential filtering defaults ON - see the module constant
        for why an unfiltered average is meaningless.
        """
        import external_listings_view as external
        residential = (q.get("residential_only") or "1").strip().lower()
        return external.transactions(
            locality=q.get("locality", ""),
            street=q.get("street", ""),
            date_from=(q.get("date_from") or "").strip(),
            date_to=(q.get("date_to") or "").strip(),
            min_price=as_float(q.get("min_price"), "min_price"),
            max_price=as_float(q.get("max_price"), "max_price"),
            residential_only=residential not in ("0", "false", "no"),
            limit=as_int(q.get("limit"), "limit", 80, low=1, high=300),
            offset=as_int(q.get("offset"), "offset", 0, low=0),
            # `total` and the per-city summary are two extra full scans of a
            # 285k-row table and depend only on the filters, so a client that
            # is merely turning a page can ask for the rows alone and keep the
            # figures it already has. Defaults on, so the API stays complete
            # for any caller that does not know about the flag.
            include_facets=(q.get("facets") or "1").strip().lower()
                           not in ("0", "false", "no"),
        )

    @staticmethod
    def _deals(conn, q: dict) -> dict:
        """Scored listings from local feeds or the read-only scraper bridge."""
        import external_listings_view as external
        state = external.status()
        if state.get("available") and state.get("records"):
            return external.listings(
                locality=q.get("locality", ""),
                neighborhood=q.get("neighborhood", ""),
                street=q.get("street", ""),
                source=q.get("source", ""),
                min_price=as_float(q.get("min_price"), "min_price"),
                max_price=as_float(q.get("max_price"), "max_price"),
                min_area=as_float(q.get("min_area"), "min_area"),
                max_area=as_float(q.get("max_area"), "max_area"),
                min_rooms=as_float(q.get("min_rooms"), "min_rooms"),
                max_rooms=as_float(q.get("max_rooms"), "max_rooms"),
                min_ppm=as_float(q.get("min_ppm"), "min_ppm"),
                max_ppm=as_float(q.get("max_ppm"), "max_ppm"),
                date_from=(q.get("date_from") or "").strip(),
                date_to=(q.get("date_to") or "").strip(),
                min_discount_pct=as_float(
                    q.get("min_discount"), "min_discount"),
                max_discount_pct=as_float(
                    q.get("max_discount"), "max_discount"),
                has_url=q.get("has_url") == "1",
                exclude_suspect=q.get("exclude_suspect") == "1",
                sort=(q.get("sort") or "score").strip(),
                limit=as_int(q.get("limit"), "limit", 60, low=1, high=300),
                offset=as_int(q.get("offset"), "offset", 0, low=0),
            )
        # Same filter set as the external branch above. Anything accepted here
        # must be forwarded to both, or the identical query answers differently
        # depending on which feed happens to be loaded.
        import opportunity
        return opportunity.deals(
            conn,
            locality=q.get("locality", ""),
            neighborhood=q.get("neighborhood", ""),
            street=q.get("street", ""),
            source=q.get("source", ""),
            min_price=as_float(q.get("min_price"), "min_price"),
            max_price=as_float(q.get("max_price"), "max_price"),
            min_area=as_float(q.get("min_area"), "min_area"),
            max_area=as_float(q.get("max_area"), "max_area"),
            min_rooms=as_float(q.get("min_rooms"), "min_rooms"),
            max_rooms=as_float(q.get("max_rooms"), "max_rooms"),
            min_ppm=as_float(q.get("min_ppm"), "min_ppm"),
            max_ppm=as_float(q.get("max_ppm"), "max_ppm"),
            date_from=(q.get("date_from") or "").strip(),
            date_to=(q.get("date_to") or "").strip(),
            max_year_built=as_int(q.get("max_year_built"), "max_year_built"),
            min_discount_pct=as_float(q.get("min_discount"), "min_discount"),
            max_discount_pct=as_float(q.get("max_discount"), "max_discount"),
            has_url=q.get("has_url") == "1",
            exclude_suspect=q.get("exclude_suspect") == "1",
            sort=(q.get("sort") or "score").strip(),
            limit=as_int(q.get("limit"), "limit", 60, low=1, high=300),
            offset=as_int(q.get("offset"), "offset", 0, low=0),
        )

    def _open_data(self, conn, q: dict) -> dict:
        """Browse/search the auxiliary datasets directly."""
        import open_data_client as od
        which = q.get("set", "contractors")
        term = (q.get("q") or "").strip()
        limit = as_int(q.get("limit"), "limit", 50, low=1, high=300)
        if which == "contractors":
            rows = od.contractors_in(conn, term or "", limit=limit) if term else \
                [dict(r) for r in conn.execute(
                    """SELECT name, license, branch, grade, group_, phone, email, city
                       FROM od_contractors ORDER BY CAST(grade AS INTEGER) DESC
                       LIMIT ?""", (limit,))]
        elif which == "appraisers":
            rows = od.appraisers_in(conn, term or "", limit=limit) if term else \
                [dict(r) for r in conn.execute(
                    "SELECT name, license, city FROM od_appraisers LIMIT ?", (limit,))]
        elif which == "localities":
            like = f"%{term}%"
            rows = [dict(r) for r in conn.execute(
                """SELECT name, name_en, nafa, bureau, council, code
                   FROM od_localities WHERE (?='' OR name LIKE ?)
                   ORDER BY name LIMIT ?""", (term, like, limit))]
        else:
            raise BadRequest(
                f"unknown set {which!r}; expected contractors, appraisers "
                "or localities")
        return {"set": which, "rows": rows, "counts": od.counts(conn)}

    @staticmethod
    def _google_places(q: dict) -> dict:
        """Explicit, live Google business lookup; never called by smart search."""
        import google_places_client as gp
        locality = (q.get("locality") or "").strip()
        category = (q.get("category") or "appraiser").strip().lower()
        if len(locality) < 2:
            return {"configured": gp.enabled(), "error": "locality is required"}
        if category not in gp.PROFESSION_CATEGORIES:
            return {"configured": gp.enabled(), "error": "unknown professional category"}
        if not gp.enabled():
            return {
                "configured": False,
                "error": "Google Places is not configured; set GOOGLE_MAPS_API_KEY",
                "categories": gp.PROFESSION_CATEGORIES,
            }
        result = gp.professionals_by_category(locality, category, page_size=10)
        result.update(configured=True, category=category,
                      profession=gp.PROFESSION_CATEGORIES[category])
        return result

    def _parcel(self, conn, q: dict) -> dict:
        """
        Everything PlanWatch knows about one parcel, in one call:
        GovMap-resolved centroid (cached), plans covering/near it, and the
        registered assets from the national land-registry extract.
        """
        import gush_helka_resolver as ghr
        import land_registry_client as lr

        import parcel_area

        gush = (q.get("gush") or "").strip()
        helka = (q.get("helka") or "").strip()
        resolved = None
        # An address is the other way in. Most people know where a flat is and
        # not which block it sits on, and the parcel dossier - the richest
        # screen in the system - was reachable only by a number almost nobody
        # carries. GovMap's public search turns the address into gush/helka,
        # and the answer says so rather than pretending the user typed it.
        address = (q.get("address") or "").strip()
        if address and not (gush and helka):
            found = parcel_area.resolve_address(address)
            usable = next((c for c in found["candidates"] if c["usable"]), None)
            if not usable:
                raise BadRequest(
                    found.get("error")
                    or f"לא נמצאה חלקה לכתובת {address!r}")
            gush, helka = str(usable["gush"]), str(usable["helka"])
            resolved = {"from": address, "label": usable.get("label"),
                        "street": parcel_area.street_of(usable.get("label")),
                        # Everything else that matched, so the screen can offer
                        # them when the first hit is the wrong town.
                        "alternatives": [c for c in found["candidates"][:8]
                                         if c is not usable]}
        if not gush or not helka:
            # /api/live-deals already raises BadRequest for this exact
            # condition; answering 200 here made the same mistake look like a
            # success on one endpoint and a failure on the other.
            raise BadRequest("gush and helka are required")

        out: dict = {"gush": gush, "helka": helka,
                     "govmap_link": None, "tabu_link": lr.order_extract_link()}
        if resolved:
            out["resolved_from_address"] = resolved

        # 1. coordinates (cached in parcel_coords; GovMap on first ask)
        try:
            point = ghr.resolve_cached(conn, gush, helka)
        except Exception as exc:
            point = None
            out["resolve_error"] = str(exc)
        if point:
            out["lat"], out["lon"] = point
            import govmap_client
            out["govmap_link"] = govmap_client.map_link(gush, helka)

        # 2. plans covering / near the point (reuses the /api/at machinery)
        if point:
            radius = q.get("radius") or "150"
            at = self._at(conn, {"lat": str(point[0]), "lon": str(point[1]),
                                 "radius": radius})
            out["plans"] = at["hits"]
            out["radius"] = at["radius"]
        else:
            out["plans"] = []

        # 2b. urban-renewal complexes near the parcel
        if point:
            try:
                import urban_renewal_client as ur
                out["renewal"] = ur.near_point(conn, point[0], point[1],
                                               radius_m=as_float(q.get("radius"), "radius", 150))
            except Exception as exc:
                out["renewal"] = []
                out["renewal_error"] = str(exc)
        else:
            out["renewal"] = []

        # 2c. appraisal history and built-form progress for this parcel.
        # A decisive-appraisal hit is documentary evidence that a plan raised
        # this parcel's value enough for the levy to be disputed.
        try:
            import appraisal_client as ac
            out["appraisals"] = ac.appraisals_for_parcel(conn, gush, helka, limit=20)
            out["progress"] = ac.progress_for_parcel(conn, gush, helka, limit=20)
        except Exception as exc:
            out["appraisals"], out["progress"] = [], []
            out["appraisal_error"] = str(exc)

        # 2c-ii. The amounts a tribunal actually ruled on THIS parcel, read out
        # of the decision documents. Every other price signal in the system is
        # an asking price, a subsidy, or a locality average; this one is an
        # ordered sum on this specific parcel, so it is worth its own key.
        try:
            import compare as _compare
            out["ruled_amounts"] = _compare.ruled_amounts_for_parcel(
                conn, gush, helka)
        except Exception as exc:
            out["ruled_amounts"] = []
            out["ruled_amounts_error"] = str(exc)

        # 2d. state land sales on this parcel - real transacted prices.
        try:
            import tenders_client as tc
            out["tender_lots"] = tc.lots_for_parcel(conn, gush, helka, limit=20)
        except Exception as exc:
            out["tender_lots"] = []
            out["tender_error"] = str(exc)

        # 2e. municipal layers: the parcel's REAL polygon, building permits,
        # and any dangerous-building order. Tel Aviv only for now.
        try:
            import municipal_client as mn
            out["parcel_polygon"] = mn.parcel_polygon(conn, gush, helka)
            if point:
                out["permits"] = mn.permits_near(conn, point[0], point[1],
                                                 radius_m=120, limit=25)
                out["dangerous"] = mn.dangerous_near(conn, point[0], point[1],
                                                     radius_m=300, limit=10)
                out["renewal_pressure"] = mn.renewal_pressure(
                    conn, point[0], point[1], radius_m=300)
            else:
                out["permits"], out["dangerous"] = [], []
                out["renewal_pressure"] = None
        except Exception as exc:
            out["parcel_polygon"], out["permits"], out["dangerous"] = None, [], []
            out["municipal_error"] = str(exc)

        # 2f. published rights-holder traces. NOT title - see owners_client.
        try:
            import owners_client as oc
            out["owner_leads"] = oc.owner_dossier(conn, gush, helka)
        except Exception as exc:
            out["owner_leads"] = None
            out["owners_error"] = str(exc)

        # 2g. National statutory overlays from the public TAMA 1 ArcGIS
        # service.  These are constraints/risk signals, not title information;
        # keep a temporary source outage isolated from the core parcel result.
        if point:
            try:
                import national_constraints_client as ncc
                out["national_constraints"] = ncc.constraints_at(
                    point[0], point[1])
            except Exception as exc:
                out["national_constraints"] = None
                out["national_constraints_error"] = str(exc)
        else:
            out["national_constraints"] = None

        # 2h. Price context. Every signal here is labelled with WHAT it is:
        # tender prices are land paid to the state, מחיר למשתכן is subsidised,
        # and the CBS index is a district trend. None of them is a market
        # transaction price for this parcel - Israel publishes no such feed
        # (nadlan.gov.il gates its API behind a CAPTCHA), and inventing a
        # number from a locality average would be the one dishonest option.
        try:
            import compare as _compare
            comparison = _compare.parcel_comparison(conn, gush, helka)
            out["locality"] = comparison.get("locality")
            out["benchmarks"] = comparison.get("benchmarks")
            out["listings"] = comparison.get("listings") or []
            # Sales concluded on this exact parcel: the most direct answer to
            # "what is it worth", and available offline from the imported
            # official dataset (no live CAPTCHA-gated call needed).
            out["parcel_transactions"] = comparison.get("parcel_transactions") or []
            out["live_deals"] = {
                "available": True, "rows": [], "read_only": True,
                "gush": gush, "helka": helka,
                "note": "לחץ על הצגת עסקאות לקריאה חיה מהמאגר הציבורי.",
            }
        except Exception as exc:
            out["benchmarks"] = None
            out["benchmarks_error"] = str(exc)

        # 2h-ii. The two questions the parcel screen could not answer: what is
        # on the market around here, and what did the neighbours actually get.
        # Both come from stores this view already had and never asked. See
        # parcel_area for why one is a radius and the other is a street.
        area_radius = as_float(q.get("area_radius"), "area_radius",
                               parcel_area.DEFAULT_RADIUS_M)
        if point:
            try:
                out["area_listings"] = parcel_area.listings_near(
                    conn, point[0], point[1], radius_m=area_radius)
            except Exception as exc:
                out["area_listings"] = None
                out["area_listings_error"] = f"{type(exc).__name__}: {exc}"
        else:
            out["area_listings"] = None

        try:
            # A street is used **only** when the user typed the address, where
            # it is the street they asked about. The first version guessed it
            # from the commonest street among nearby listings, and for
            # דיזנגוף 100 that produced "רחוב פרוג" - a real street 65 m away
            # and not this parcel's - under a heading claiming to show that
            # street's deals. A wrong street is worse than no street: the
            # town-level rows are still there and are honestly labelled.
            # Either the server resolved the address itself, or the screen
            # resolved it and is telling us which street it picked. The second
            # case is the normal one: the address box resolves to gush/helka in
            # the browser and then asks for the parcel by number, so without
            # this the street the user typed is lost between the two calls and
            # every address search silently fell back to town-level deals.
            street = ((resolved or {}).get("street")
                      or parcel_area.street_of(q.get("street")))
            out["area_transactions"] = parcel_area.transactions_near(
                conn, locality=out.get("locality")
                or ((out.get("area_listings") or {}).get("rows") or [{}])[0].get("city"),
                street=street)
        except Exception as exc:
            out["area_transactions"] = None
            out["area_transactions_error"] = f"{type(exc).__name__}: {exc}"

        # 2i. What the plans actually PERMIT here: the land-use cell covering
        # this point, with its designation and area. A parcel's worth follows
        # from what may be built on it, so this belongs in the parcel view.
        if point:
            try:
                import plan_detail_client as pdc
                out["landuse_cells"] = pdc.landuse_at_point(
                    conn, point[1], point[0], limit=12)
            except Exception as exc:
                out["landuse_cells"] = []
                out["landuse_error"] = str(exc)
        else:
            out["landuse_cells"] = []

        # 2j. Every named entity the unified index ties to this parcel, so the
        # parcel view answers "who is connected here, and in how many
        # independent registers" without a second search.
        try:
            import entity_index as ei
            out["entities"] = ei.entities_for_parcel(conn, gush, helka)
        except Exception as exc:
            out["entities"] = []
            out["entities_error"] = str(exc)

        # 3. land-registry ownership: local import first, live API fallback
        try:
            rows = lr.local_ownership(conn, gush, helka)
            out["ownership_source"] = "local"
            if rows is None:
                rows = lr.ownership(gush, helka)
                out["ownership_source"] = "live"
            out["ownership"] = rows
            out["ownership_summary"] = lr.summarize(rows)
        except Exception as exc:
            out["ownership"] = []
            out["ownership_summary"] = {"assets": 0, "by_ownership": {}}
            out["ownership_error"] = str(exc)
        return out

    def _geometries(self, conn, q: dict) -> dict:
        where, params = self._filter_sql(q)
        where += " AND geometry_json IS NOT NULL"
        cap = as_int(q.get("limit"), "limit", MAP_FEATURE_CAP, low=1, high=MAP_FEATURE_CAP)
        total = conn.execute(f"SELECT COUNT(*) FROM plans WHERE {where}",
                             params).fetchone()[0]
        # Smallest-first, so a handful of country-sized master plans cannot eat
        # the whole byte budget and crowd out everything else.
        rows = conn.execute(
            f"""SELECT object_id, pl_number, pl_name, jurisdiction_name,
                       short_status, housing_units, area_dunam, last_update, geometry_json
                FROM plans WHERE {where}
                ORDER BY area_dunam ASC LIMIT ?""", params + [cap]).fetchall()

        features, budget_used, truncated = [], 0, False
        for r in rows:
            raw = r["geometry_json"]
            if budget_used + len(raw) > MAP_BYTE_BUDGET and features:
                truncated = True
                break
            budget_used += len(raw)
            features.append({
                "type": "Feature",
                "geometry": json.loads(raw),
                "properties": {k: r[k] for k in (
                    "object_id", "pl_number", "pl_name", "jurisdiction_name",
                    "short_status", "housing_units", "area_dunam", "last_update")},
            })
        return {"total": total, "shown": len(features),
                "capped": total > len(features), "truncated": truncated,
                "bytes": budget_used,
                "geojson": {"type": "FeatureCollection", "features": features}}

    @staticmethod
    def _timeline(conn, q: dict) -> dict:
        field = q.get("field", "depositing_date")
        if field not in {m[0] for m in report.MILESTONES}:
            field = "depositing_date"
        rows = conn.execute(
            f"""SELECT substr({field},1,4) year, COUNT(*) plans,
                       COALESCE(SUM(housing_units),0) units
                FROM plans WHERE {field} IS NOT NULL AND {field} <> ''
                GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        return {"field": field, "rows": [dict(r) for r in rows]}

    def _breakdown(self, conn, q: dict) -> dict:
        column = {"jurisdiction": "jurisdiction_name", "county": "county_name",
                  "status": "short_status", "subtype": "entity_subtype",
                  "station": "station"}.get(q.get("by", "jurisdiction"))
        if column is None:
            # A bad parameter is a 400 everywhere else in this API; answering
            # 200 with an error body made an unusable request look successful
            # to anything reading the status code.
            raise BadRequest(
                f"bad 'by' {q.get('by')!r}; expected one of "
                "jurisdiction, county, status, subtype, station")
        where, params = self._filter_sql(q)
        limit = as_int(q.get("limit"), "limit", 12, low=1, high=50)
        rows = conn.execute(
            f"""SELECT {column} label, COUNT(*) plans,
                       COALESCE(SUM(housing_units),0) units
                FROM plans WHERE {where} AND {column} IS NOT NULL AND {column} <> ''
                GROUP BY 1 ORDER BY plans DESC LIMIT ?""", params + [limit]).fetchall()
        return {"by": q.get("by", "jurisdiction"), "rows": [dict(r) for r in rows]}

    @staticmethod
    def _history(conn, q: dict) -> dict:
        limit = as_int(q.get("limit"), "limit", 100, low=1, high=500)
        rows = conn.execute(
            """SELECT l.id, l.object_id, l.old_station, l.new_station,
                      l.detected_at, p.pl_number, p.pl_name, p.pl_url
               FROM plan_status_log l JOIN plans p ON p.object_id = l.object_id
               ORDER BY l.id DESC LIMIT ?""", (limit,)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM plan_status_log").fetchone()[0]
        return {"total": total, "rows": [dict(r) for r in rows]}

    @staticmethod
    def _notifications(conn, q: dict) -> dict:
        delivered = q.get("delivered", "0")
        clause = "" if delivered == "all" else "WHERE n.delivered = ?"
        params: list = [] if delivered == "all" else [int(delivered)]
        rows = conn.execute(
            f"""SELECT n.id, n.kind, n.message, n.created_at, n.delivered,
                       n.object_id, s.label, s.client_id, s.gush, s.helka, p.pl_url
                FROM notifications n
                JOIN subscriptions s ON s.id = n.subscription_id
                LEFT JOIN plans p ON p.object_id = n.object_id
                {clause} ORDER BY n.created_at DESC LIMIT 300""", params).fetchall()
        return {"rows": [dict(r) for r in rows]}

    @staticmethod
    def _subscriptions(conn) -> dict:
        rows = conn.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM subscription_plan_matches m
                        WHERE m.subscription_id = s.id) matches,
                      (SELECT COUNT(*) FROM notifications n
                        WHERE n.subscription_id = s.id AND n.delivered = 0) pending
               FROM subscriptions s ORDER BY s.id"""
        ).fetchall()
        return {"rows": [dict(r) for r in rows]}

    def _export(self, conn, q: dict) -> None:
        where, params = self._filter_sql(q)
        fmt = q.get("format", "csv")
        if fmt == "geojson":
            where += " AND geometry_json IS NOT NULL"
        rows = conn.execute(f"SELECT * FROM plans WHERE {where}", params).fetchall()

        if fmt == "geojson":
            payload = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": json.loads(r["geometry_json"]),
                 "properties": {c: r[c] for c in report.LIST_COLUMNS}}
                for r in rows]}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            return self._download(body, "planwatch.geojson", "application/geo+json")

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(report.LIST_COLUMNS)
        for r in rows:
            writer.writerow([r[c] for c in report.LIST_COLUMNS])
        # utf-8-sig so Excel reads Hebrew instead of mojibake
        body = buf.getvalue().encode("utf-8-sig")
        self._download(body, "planwatch.csv", "text/csv")

    # -- unified architecture -------------------------------------------------

    def _unified_sources(self) -> dict:
        """List all sources with their health status."""
        sources = []
        for adapter_class in (Yad2SourceAdapter, FacebookSourceAdapter, OnmapSourceAdapter):
            try:
                adapter = adapter_class(db.DB_PATH)
                health = adapter.health()
                state = adapter.state()
                sources.append({
                    "name": adapter.source_name,
                    "display_name": adapter.display_name,
                    "health": health,
                    "state": state,
                })
            except Exception as exc:
                sources.append({
                    "name": adapter_class.__name__,
                    "error": str(exc),
                })
        return {"sources": sources}

    def _unified_health(self) -> dict:
        """Get health status for all sources."""
        monitor = HealthMonitor(db.DB_PATH)
        return {"health": [h.to_dict() for h in monitor.get_all_health()]}

    def _unified_validate(self, payload: dict) -> dict:
        """Validate a listing against business rules."""
        validator = ListingValidator()
        result = validator.validate(payload)
        return {
            "valid": result.is_valid,
            "errors": result.errors,
            "warnings": result.warnings,
            "normalized": result.normalized,
        }

    @staticmethod
    def _unified_run(payload: dict) -> dict:
        """Run a source job via the unified interface."""
        source_name = (payload.get("source") or "").strip().lower()
        target = int(payload.get("target") or 500)

        adapters = {
            "yad2": Yad2SourceAdapter,
            "facebook": FacebookSourceAdapter,
            "onmap": OnmapSourceAdapter,
        }

        adapter_class = adapters.get(source_name)
        if not adapter_class:
            return {"error": f"unknown source {source_name!r}; expected one of {sorted(adapters)}"}

        adapter = adapter_class(db.DB_PATH)
        conn = db.get_conn()
        try:
            run_state = adapter.run(conn, target=target)
            return {
                "source": source_name,
                "status": run_state.status.value,
                "rows_new": run_state.rows_new,
                "rows_updated": run_state.rows_updated,
                "error": run_state.error,
                "started_at": run_state.started_at,
                "finished_at": run_state.finished_at,
            }
        finally:
            conn.close()

    # -- unified API endpoints -------------------------------------------------

    def _api_listings(self, conn, q: dict) -> dict:
        """Listings from the normalized store, normalized to the dashboard contract."""
        source = (q.get("source") or "").strip() or None
        limit = as_int(q.get("limit"), "limit", 50, low=1, high=500)
        offset = as_int(q.get("offset"), "offset", 0, low=0)

        try:
            from storage.sqlite_local import NormalizedStore
            store = NormalizedStore(db.DB_PATH)
            rows = store.get_active_filtered(
                source=source,
                city=q.get("city"),
                property_type=q.get("propertyType") or q.get("property_type"),
                min_price=as_float(q.get("minPrice"), "minPrice") or as_float(q.get("min_price"), "min_price"),
                max_price=as_float(q.get("maxPrice"), "maxPrice") or as_float(q.get("max_price"), "max_price"),
                min_rooms=as_float(q.get("minRooms"), "minRooms") or as_float(q.get("min_rooms"), "min_rooms"),
                max_rooms=as_float(q.get("maxRooms"), "maxRooms") or as_float(q.get("max_rooms"), "max_rooms"),
                limit=limit, offset=offset)
            total = store.count_filtered(
                source=source,
                city=q.get("city"),
                property_type=q.get("propertyType") or q.get("property_type"),
                min_price=as_float(q.get("minPrice"), "minPrice") or as_float(q.get("min_price"), "min_price"),
                max_price=as_float(q.get("maxPrice"), "maxPrice") or as_float(q.get("max_price"), "max_price"),
                min_rooms=as_float(q.get("minRooms"), "minRooms") or as_float(q.get("min_rooms"), "min_rooms"),
                max_rooms=as_float(q.get("maxRooms"), "maxRooms") or as_float(q.get("max_rooms"), "max_rooms"),
            )
            normalized = [Handler._normalize_listing(row) for row in rows]
            return {"rows": normalized, "total": total.get("total", 0),
                    "limit": limit, "offset": offset}
        except Exception as exc:
            return {"rows": [], "total": 0, "error": str(exc)}

    def _api_local_listings(self, conn, q: dict) -> dict:
        """Local listings from the normalized store with totalCount and rows.

        Mirrors the contract the Node /api/local-listings endpoint served, but
        reads from the single source of truth (NormalizedStore) so the UI never
        sees two different row shapes for the same data.
        """
        source = (q.get("source") or "").strip() or None
        limit = as_int(q.get("limit"), "limit", 50, low=1, high=500)
        offset = as_int(q.get("offset"), "offset", 0, low=0)

        try:
            from storage.sqlite_local import NormalizedStore
            store = NormalizedStore(db.DB_PATH)
            rows = store.get_active_filtered(
                source=source,
                city=q.get("city"),
                property_type=q.get("propertyType") or q.get("property_type"),
                min_price=as_float(q.get("minPrice"), "minPrice") or as_float(q.get("min_price"), "min_price"),
                max_price=as_float(q.get("maxPrice"), "maxPrice") or as_float(q.get("max_price"), "max_price"),
                min_rooms=as_float(q.get("minRooms"), "minRooms") or as_float(q.get("min_rooms"), "min_rooms"),
                max_rooms=as_float(q.get("maxRooms"), "maxRooms") or as_float(q.get("max_rooms"), "max_rooms"),
                limit=limit, offset=offset)
            counts = store.count_filtered(
                source=source,
                city=q.get("city"),
                property_type=q.get("propertyType") or q.get("property_type"),
                min_price=as_float(q.get("minPrice"), "minPrice") or as_float(q.get("min_price"), "min_price"),
                max_price=as_float(q.get("maxPrice"), "maxPrice") or as_float(q.get("max_price"), "max_price"),
                min_rooms=as_float(q.get("minRooms"), "minRooms") or as_float(q.get("min_rooms"), "min_rooms"),
                max_rooms=as_float(q.get("maxRooms"), "maxRooms") or as_float(q.get("max_rooms"), "max_rooms"),
            )
            normalized = [Handler._normalize_listing(row) for row in rows]
            total = counts.get("total", 0)
            return {
                "rows": normalized,
                "totalCount": total,
                "total": total,
                "page": (offset // limit) + 1 if limit > 0 else 1,
                "totalPages": (total + limit - 1) // limit if limit > 0 else 1,
                "source": source or "all",
                "lastSeen": counts.get("last_seen"),
            }
        except Exception as exc:
            return {"rows": [], "totalCount": 0, "total": 0,
                    "page": 1, "totalPages": 1, "error": str(exc)}

    def _api_integrations(self, conn) -> dict:
        """Source integration status, sourced from the live sources monitor.

        Replaces the Node server's hardcoded integrations list so there is
        one source of truth for source status.
        """
        sources = _live_sources()
        integrations = []
        for src in sources.get("sources", []):
            integrations.append({
                "id": src.get("name", "").lower().replace(" ", "-"),
                "name": src.get("name", ""),
                "category": "listings" if "listing" in (src.get("name", "") + src.get("detail", "")).lower()
                            else "planning" if "plan" in (src.get("name", "") + src.get("detail", "")).lower()
                            else "market" if "price" in (src.get("name", "") + src.get("detail", "")).lower()
                            else "other",
                "status": src.get("status", "unknown"),
                "access": src.get("endpoint", ""),
                "description": src.get("detail", ""),
                "records": src.get("records"),
            })
        return {
            "updatedAt": db.now_iso(),
            "integrations": integrations,
        }

    def _api_listing_stats(self, conn) -> dict:
        """Stats about the normalized listings."""
        try:
            from storage.sqlite_local import NormalizedStore
            store = NormalizedStore(db.DB_PATH)
            return store.count()
        except Exception as exc:
            return {"error": str(exc)}

    def _api_source_health(self) -> dict:
        """Source health from the unified health monitor."""
        try:
            from jobs.health import HealthMonitor
            monitor = HealthMonitor(db.DB_PATH)
            return {"health": [h.to_dict() for h in monitor.get_all_health()]}
        except Exception as exc:
            return {"error": str(exc)}

    def _api_runs(self, conn, q: dict) -> dict:
        """Recent run history."""
        limit = as_int(q.get("limit"), "limit", 20, low=1, high=200)
        try:
            from jobs.scheduler import UnifiedScheduler
            scheduler = UnifiedScheduler(db.DB_PATH)
            return {"runs": scheduler.get_recent_runs(limit=limit)}
        except Exception as exc:
            return {"error": str(exc)}

    def _api_alerts(self, conn, q: dict) -> dict:
        """Active alerts."""
        try:
            from jobs.health import HealthMonitor
            monitor = HealthMonitor(db.DB_PATH)
            return {"alerts": monitor.get_active_alerts()}
        except Exception as exc:
            return {"error": str(exc)}


class SingleInstanceServer(ThreadingHTTPServer):
    """
    Refuses to start when the port is already served.

    `HTTPServer` sets `allow_reuse_address = True`, which on Windows means
    SO_REUSEADDR lets a SECOND process bind a port that is already in use -
    both processes then listen and the OS splits incoming connections between
    them. The visible symptom is brutal: a stale instance answers some of the
    requests, so newer endpoints return 404 at random while older ones work.
    Verified reproducible on this machine.
    """

    allow_reuse_address = False

    def server_bind(self):
        try:
            super().server_bind()
        except OSError as exc:
            raise SystemExit(
                f"\nPort {self.server_address[1]} is already in use "
                f"({exc.strerror or exc}).\n"
                f"Another PlanWatch dashboard is probably still running - stop it, "
                f"or start this one with --port <other>.\n"
            ) from exc


def _port_is_serving(host: str, port: int, timeout: float = 0.8) -> bool:
    """True when something already answers on host:port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def main() -> int:
    # PLANWATCH_DEPLOY changes two argparse defaults below (host, and whether
    # the scheduler thread starts) and gates a third thing that isn't a CLI
    # flag at all - see the fail-fast auth check just after parsing.
    render_mode = DEPLOY_MODE == "render"

    parser = argparse.ArgumentParser(description="PlanWatch dashboard server.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0" if render_mode else "127.0.0.1",
                        help="default is localhost-only unless PLANWATCH_DEPLOY=render; "
                             "auth is required in render mode, optional otherwise")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-scheduler", action="store_true",
                        help=f"do not run the automatic {AUTO_CYCLE_HOURS}-hourly "
                             "refresh (the button still works)")
    parser.add_argument("--scheduler", action="store_true",
                        help="force the automatic refresh on even under "
                             "PLANWATCH_DEPLOY=render, where it is off by "
                             "default because a deploy mid-run leaves the "
                             "database half-written - see DEPLOY.md §5.1 for "
                             "running the same jobs as an external cron instead")
    args = parser.parse_args()

    # A public deploy without a password is the exact failure DEPLOY.md §1.1
    # exists to prevent - refuse to serve a single request rather than trust
    # that whoever runs this remembers to set the variable. Same fail-fast
    # posture as the two checks just below it.
    if render_mode and not BASIC_AUTH:
        print("PLANWATCH_DEPLOY=render requires PLANWATCH_BASIC_AUTH to be set "
              "(\"user:pass\") - refusing to start unauthenticated on a public "
              "deploy. See DEPLOY.md §3.")
        return 2

    # Belt and braces: allow_reuse_address=False stops the double-bind, but an
    # explicit probe gives a far clearer message than a raw WinError.
    if _port_is_serving(args.host, args.port):
        print(f"Port {args.port} is already serving on {args.host}.")
        print("Stop the other dashboard first, or pass --port <other>.")
        return 2

    # Fail fast with a clear message rather than an empty page.
    conn = db.get_conn()
    plans = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    conn.close()

    url = f"http://{args.host}:{args.port}"
    print(f"PlanWatch dashboard  ->  {url}")
    print(f"  DB: {db.DB_PATH}  ({plans:,} plans)")
    if not plans:
        print("  The database is empty. Use the 'סנכרון מלא' button in the UI,"
              " or run: python sync.py --full")
    print(f"  Deploy mode: {DEPLOY_MODE}"
          + ("  (auth required)" if BASIC_AUTH else "  (no auth configured)"))
    print("  Ctrl+C to stop.\n")

    # Off by default in render mode: a background thread inside a container
    # that gets replaced mid-deploy is exactly how a scheduled write gets cut
    # halfway through. DEPLOY.md §5.1 runs the same 15 jobs as Render Cron
    # instead. --scheduler overrides this for anyone deploying without cron.
    scheduler_on = args.scheduler or (not args.no_scheduler and not render_mode)
    if not scheduler_on:
        reason = "--no-scheduler" if args.no_scheduler else "PLANWATCH_DEPLOY=render"
        print(f"  Automatic refresh: OFF ({reason}). The button still works.")
    else:
        print(f"  Automatic refresh: every {AUTO_CYCLE_HOURS}h "
              "(each feed also honours its own interval).")
        _sched_start_auto()

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    server = SingleInstanceServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
