"""
PlanWatch - Sync job
====================
Run on a schedule (e.g. every 30-60 min via cron / Task Scheduler):

    python sync.py                  # incremental: only plans changed since last run
    python sync.py --full           # re-pull all ~36,700 plans
    python sync.py --since 2026-01-01
    python sync.py --backfill-matches   # re-match subscriptions against all plans

Each run:
  1. Streams the plan list from the Blue Lines service (paged).
  2. Upserts into the DB, recording newly-added plans and station changes.
  3. For every new/changed plan, finds which subscriptions (גוש/חלקה points)
     fall inside that plan's polygon and queues a notification.

Notes on גוש/חלקה matching
---------------------------
The Blue Lines service gives plan geometry but not cadastral (גוש/חלקה)
boundaries. To resolve "does this plan touch parcel גוש X חלקה Y" you need
either:
  (a) a lat/lon for the parcel -> point-in-polygon against each plan's
      geometry (what this module does), or
  (b) a full cadastral polygon layer -> polygon-polygon intersection, which is
      more precise for parcels that only partially overlap a plan.
This implements (a). Resolving גוש/חלקה -> lat/lon is pluggable; see
gush_helka_resolver.py, which depends on which cadastral source you license.

Matching strategy
-----------------
Naive matching is O(plans x subscriptions) with a full polygon test each time -
unusable at 36,700 polygons. Instead each plan's bounding box is indexed in
SQLite (see db.geometry_bbox), so a subscription point is first narrowed to a
few bbox candidates and only those get an exact shapely test.

Bootstrap suppression
---------------------
On the very first successful sync every plan is "new to us" - but a plan
approved in 2014 is not news to the client. Notifying on those would send a
burst of stale alerts (measured: 8 for a single Jerusalem parcel, several
already closed). So the first sync records matches silently and only later runs
notify about newly-published plans. `--notify-bootstrap` overrides this.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone

from shapely import affinity
from shapely.geometry import Point, shape

from services.external import blue_lines_client
import db

#: Metres per degree of latitude (WGS84, mid-latitudes). Longitude degrees
#: shrink by cos(latitude), which is why radius tests cannot use a single
#: degrees-per-metre constant for both axes - see _distance_m.
M_PER_DEG_LAT = 110_574
M_PER_DEG_LON_EQUATOR = 111_320


def sync_plans(mode: str = "incremental", since: str | None = None,
               page_size: int = blue_lines_client.MAX_PAGE_SIZE,
               dry_run: bool = False, notify_bootstrap: bool = False,
               should_stop=None) -> dict:
    """
    Pull plans and queue notifications. Returns a summary dict.

    `mode`: 'full' pulls everything; 'incremental' pulls only plans whose
    `last_update_date` is newer than the last successful run (minus a small
    safety overlap, since the source's timestamps are day-granular).

    `should_stop` - optional callable() -> bool for cancellation. Stopping is
    safe: pages already written stay committed, but the run is recorded as
    NOT ok, so the next incremental sync re-reads from the previous successful
    anchor rather than skipping the un-fetched tail.
    """
    conn = db.get_conn()
    where = "1=1"
    is_bootstrap = db.last_successful_sync(conn) is None

    if mode == "incremental":
        anchor = since or db.last_successful_sync(conn)
        if anchor:
            # Overlap by a day: last_update_date has day granularity, so an
            # exact cutoff can skip plans updated on the boundary day.
            cutoff = _parse_iso(anchor) - timedelta(days=2)
            where = blue_lines_client.updated_since_clause(cutoff)
        else:
            print("No previous successful sync - doing a full pull.")
            mode = "full"
    elif since:
        where = blue_lines_client.updated_since_clause(since)

    run_id = db.start_sync_run(conn, mode)
    print(f"Sync ({mode}) filter: {where}")
    if is_bootstrap and not notify_bootstrap:
        print("First sync: recording matches without notifying "
              "(use --notify-bootstrap to alert on pre-existing plans).")

    seen = new_count = 0
    #: object_id -> 'new_plan' | 'status_change'. Tracked explicitly rather
    #: than re-derived from the DB afterwards, which cannot distinguish
    #: "newly published" from "newly known to us".
    kinds: dict[int, str] = {}

    try:
        def progress(fetched, total):
            pct = f" ({fetched * 100 // total}%)" if total else ""
            print(f"  fetched {fetched:,}"
                  + (f" / {total:,}{pct}" if total else ""), end="\r", flush=True)

        with db.plans_writer(conn) as write:
            for plan in blue_lines_client.iter_plans(
                where=where, page_size=page_size, progress=progress,
                should_stop=should_stop,
            ):
                if plan["object_id"] is None:
                    continue
                seen += 1
                if dry_run:
                    continue
                is_new, status_changed, _old = write(plan)
                if is_new:
                    new_count += 1
                    kinds[plan["object_id"]] = "new_plan"
                elif status_changed:
                    kinds[plan["object_id"]] = "status_change"

        print()  # end the progress line
        cancelled = bool(should_stop and should_stop())
        changes = sum(1 for k in kinds.values() if k == "status_change")
        print(f"{'Stopped after' if cancelled else 'Synced'} {seen:,} plans. "
              f"New: {new_count:,}. Status changes: {changes:,}")

        notified = 0
        if dry_run:
            print("(dry run - nothing written, no notifications)")
        elif kinds:
            notified = match_and_notify(
                conn, kinds, notify=(notify_bootstrap or not is_bootstrap)
            )

        # A cancelled run is not "ok": leaving it ok would move the incremental
        # anchor forward past plans we never fetched.
        db.finish_sync_run(conn, run_id, seen=seen, new=new_count, changes=changes,
                           notifications=notified, ok=not cancelled,
                           error="cancelled by user" if cancelled else None)
        return {"seen": seen, "new": new_count, "changed": changes,
                "notified": notified, "cancelled": cancelled}
    except Exception as exc:
        db.finish_sync_run(
            conn, run_id, seen=seen, new=new_count,
            changes=sum(1 for k in kinds.values() if k == "status_change"),
            ok=False, error=repr(exc),
        )
        raise
    finally:
        conn.close()


def match_and_notify(conn, kinds: dict[int, str], notify: bool = True) -> int:
    """
    For each new/changed plan, find every subscription whose point falls inside
    it (or within its `radius_m`), record the match, and queue a notification.

    `kinds` maps object_id -> 'new_plan' | 'status_change'.
    `notify=False` records matches only (used for the bootstrap sync).
    Returns the notification count.
    """
    subs = conn.execute(
        """SELECT * FROM subscriptions
           WHERE active=1 AND lat IS NOT NULL AND lon IS NOT NULL"""
    ).fetchall()
    if not subs:
        print("No subscriptions with coordinates - nothing to match. "
              "(See gush_helka_resolver.py to turn גוש/חלקה into lat/lon.)")
        return 0

    notified = matched = 0

    for sub in subs:
        pad_lon, pad_lat = _radius_pads(sub["radius_m"], sub["lat"])
        candidates = db.plans_covering_point(
            conn, sub["lon"], sub["lat"], pad_lon=pad_lon, pad_lat=pad_lat
        )
        point = Point(sub["lon"], sub["lat"])

        for plan in candidates:
            if plan["object_id"] not in kinds:
                continue
            hit, method = _point_hits_plan(point, plan, sub["radius_m"])
            if not hit:
                continue

            conn.execute(
                """INSERT OR IGNORE INTO subscription_plan_matches
                   (subscription_id, object_id, matched_at, match_method)
                   VALUES (?,?,?,?)""",
                (sub["id"], plan["object_id"], db.now_iso(), method),
            )
            matched += 1
            if not notify:
                continue

            message = _build_message(sub, plan)
            db.create_notification(conn, sub["id"], plan["object_id"], message,
                                   kind=kinds[plan["object_id"]])
            notified += 1
            print("Queued notification:", message)

    conn.commit()
    if not notify and matched:
        print(f"Recorded {matched:,} matches without notifying (bootstrap).")
    return notified


def backfill_matches(conn) -> int:
    """
    Match every active subscription against ALL stored plans, not just the ones
    that changed this run. Use after adding a subscription, so the client's
    dashboard shows the plans already covering their parcel.

    Records matches only - it deliberately queues no notifications, since these
    are pre-existing plans rather than fresh news.
    """
    subs = conn.execute(
        """SELECT * FROM subscriptions
           WHERE active=1 AND lat IS NOT NULL AND lon IS NOT NULL"""
    ).fetchall()
    if not subs:
        print("No subscriptions with coordinates.")
        return 0

    found = 0
    for sub in subs:
        pad_lon, pad_lat = _radius_pads(sub["radius_m"], sub["lat"])
        point = Point(sub["lon"], sub["lat"])
        hits = 0
        for plan in db.plans_covering_point(conn, sub["lon"], sub["lat"],
                                            pad_lon=pad_lon, pad_lat=pad_lat):
            hit, method = _point_hits_plan(point, plan, sub["radius_m"])
            if not hit:
                continue
            hits += 1
            cur = conn.execute(
                """INSERT OR IGNORE INTO subscription_plan_matches
                   (subscription_id, object_id, matched_at, match_method)
                   VALUES (?,?,?,?)""",
                (sub["id"], plan["object_id"], db.now_iso(), method),
            )
            if cur.rowcount:
                found += 1
        label = sub["label"] or f"{sub['gush']}/{sub['helka']}"
        print(f"  {label}: {hits} plans cover this point")
    conn.commit()
    print(f"Backfill added {found:,} new matches.")
    return found


def _load_geometry(plan):
    """Parsed, repaired geometry for a plan row, or None."""
    if not plan["geometry_json"]:
        return None
    try:
        geom = shape(json.loads(plan["geometry_json"]))
    except (ValueError, TypeError, AttributeError, KeyError):
        return None
    if geom.is_empty:
        return None
    if not geom.is_valid:
        # Self-intersections are common in published planning polygons;
        # buffer(0) repairs them instead of letting predicates raise.
        geom = geom.buffer(0)
        if geom.is_empty:
            return None
    return geom


def _distance_m(geom, point: Point, lat: float) -> float:
    """
    Approximate metric distance from `point` to `geom`, in metres.

    Degrees are anisotropic: at Israel's latitude a degree of longitude is
    ~94.3 km while a degree of latitude is ~110.6 km. Comparing a raw degree
    distance against one degrees-per-metre constant made a 150 m radius behave
    like ~178 m north-south. Scaling longitude onto the latitude scale first
    makes the euclidean distance uniform, so one multiplication converts it.
    """
    xfact = M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat)) / M_PER_DEG_LAT
    scaled_geom = affinity.scale(geom, xfact=xfact, yfact=1.0, origin=(0, 0))
    scaled_point = affinity.scale(point, xfact=xfact, yfact=1.0, origin=(0, 0))
    return scaled_geom.distance(scaled_point) * M_PER_DEG_LAT


def _point_hits_plan(point: Point, plan, radius_m) -> tuple[bool, str]:
    """Exact test after the bbox prefilter. Returns (hit, match_method)."""
    geom = _load_geometry(plan)
    if geom is None:
        return False, ""

    if geom.covers(point):  # covers, not contains: a parcel on the edge counts
        return True, "point_in_polygon"
    if radius_m and _distance_m(geom, point, point.y) <= float(radius_m):
        return True, "radius"
    return False, ""


def _radius_pads(radius_m, lat: float) -> tuple[float, float]:
    """
    Per-axis degree padding for the bbox prefilter. Deliberately generous (it
    must not exclude anything the exact test would accept); `_distance_m` makes
    the final call.
    """
    if not radius_m:
        return 0.0, 0.0
    radius_m = float(radius_m)
    cos_lat = max(math.cos(math.radians(lat)), 0.1)  # guard near the poles
    return (radius_m / (M_PER_DEG_LON_EQUATOR * cos_lat),
            radius_m / M_PER_DEG_LAT)


def _build_message(sub, plan) -> str:
    parcel = sub["label"] or f"{sub['gush']}/{sub['helka']}"
    title = plan["pl_name"] or plan["pl_number"] or f"#{plan['object_id']}"
    status = plan["short_status"] or plan["station"] or "לא ידוע"
    parts = [f'עדכון עבור "{title}" ({parcel}): סטטוס - {status}']
    if plan["jurisdiction_name"]:
        parts.append(f'רשות: {plan["jurisdiction_name"]}')
    if plan["housing_units"]:
        parts.append(f'יח"ד: {plan["housing_units"]}')
    if plan["pl_url"]:
        parts.append(plan["pl_url"])
    return " | ".join(parts)


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="PlanWatch sync job.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--full", action="store_true",
                       help="re-pull every plan (~36,700)")
    group.add_argument("--incremental", action="store_true",
                       help="only plans changed since the last successful run (default)")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="explicit cutoff instead of the last-run timestamp")
    parser.add_argument("--page-size", type=int,
                        default=blue_lines_client.MAX_PAGE_SIZE,
                        help=f"rows per request (max {blue_lines_client.MAX_PAGE_SIZE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report, write nothing")
    parser.add_argument("--notify-bootstrap", action="store_true",
                        help="also alert on pre-existing plans during the first sync")
    parser.add_argument("--backfill-matches", action="store_true",
                        help="re-match subscriptions against all stored plans, then exit")
    args = parser.parse_args()

    if args.backfill_matches:
        conn = db.get_conn()
        try:
            backfill_matches(conn)
        finally:
            conn.close()
        return 0

    mode = "full" if args.full else "incremental"
    summary = sync_plans(mode=mode, since=args.since, page_size=args.page_size,
                         dry_run=args.dry_run,
                         notify_bootstrap=args.notify_bootstrap)
    print(f"Done: {summary}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(_cli())
