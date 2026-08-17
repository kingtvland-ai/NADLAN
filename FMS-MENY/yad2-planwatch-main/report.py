"""
PlanWatch - reading the results
================================
The sync job fills the database; this is how you look at it. There is no web UI
yet, so everything here is a CLI query or an export.

    python report.py stats                     # overview
    python report.py pending                   # undelivered notifications (the outbox)
    python report.py at 32.0853 34.7818        # which plans cover this point
    python report.py at 32.0853 34.7818 -r 300 # ...and within 300 m
    python report.py plan 36707                # one plan: all fields + milestones
    python report.py history                   # status changes we have OBSERVED
    python report.py timeline                  # plans per year (source milestone dates)
    python report.py search --jurisdiction ירושלים --min-units 50
    python report.py export --format geojson --out plans.geojson --jurisdiction חיפה

Two kinds of "history" - the distinction matters
------------------------------------------------
1. MILESTONE DATES (already complete, back to 1985). The source carries
   per-plan dates: depositing_date, open_date, pl_date_advertise,
   pl_last_deposit_date, pl_rejection_date. `sync.py --full` already pulled
   every plan the service exposes, so this history needs no waiting - use
   `timeline` and `plan <id>`.

2. OBSERVED TRANSITIONS (accumulates from your first sync onward).
   `plan_status_log` records station changes that PlanWatch itself saw between
   two syncs. The service is a current-state snapshot with no time-travel
   query, so this cannot be backfilled - see `history`.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

import db

# Columns shown by the table views, and exported by --format csv.
LIST_COLUMNS = (
    "object_id", "pl_number", "pl_name", "jurisdiction_name", "county_name",
    "entity_subtype", "station", "short_status", "area_dunam", "housing_units",
    "last_update", "depositing_date", "pl_url",
)

MILESTONES = (
    ("open_date", "נפתחה"),
    ("depositing_date", "הפקדה"),
    ("pl_last_deposit_date", "הפקדה אחרונה"),
    ("pl_date_advertise", "פרסום"),
    ("pl_rejection_date", "דחייה"),
    ("last_update", "עדכון אחרון"),
)


def _filters(args) -> tuple[str, list]:
    """Build a shared WHERE clause from the standard filter flags."""
    clauses, params = ["1=1"], []
    if getattr(args, "jurisdiction", None):
        clauses.append("jurisdiction_name LIKE ?")
        params.append(f"%{args.jurisdiction}%")
    if getattr(args, "county", None):
        clauses.append("county_name LIKE ?")
        params.append(f"%{args.county}%")
    if getattr(args, "status", None):
        clauses.append("(station LIKE ? OR short_status LIKE ?)")
        params += [f"%{args.status}%", f"%{args.status}%"]
    if getattr(args, "text", None):
        clauses.append("(pl_name LIKE ? OR pl_number LIKE ? OR objectives LIKE ?)")
        params += [f"%{args.text}%"] * 3
    if getattr(args, "since", None):
        clauses.append("last_update >= ?")
        params.append(args.since)
    if getattr(args, "min_units", None):
        clauses.append("housing_units >= ?")
        params.append(args.min_units)
    if getattr(args, "with_geometry", False):
        clauses.append("geometry_json IS NOT NULL")
    return " AND ".join(clauses), params


def _print_table(rows, columns=("object_id", "pl_number", "station", "pl_name")):
    if not rows:
        print("  (no rows)")
        return
    widths = {c: max(len(c), *(len(str(r[c] or "")[:44]) for r in rows)) for c in columns}
    print("  " + "  ".join(c.ljust(widths[c]) for c in columns))
    print("  " + "  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  " + "  ".join(str(r[c] or "")[:44].ljust(widths[c]) for c in columns))


def cmd_stats(conn, args) -> None:
    print(f"DB: {db.DB_PATH}\n")
    for key, value in db.stats(conn).items():
        print(f"  {key:24} {value}")

    print("\n  data coverage:")
    for col in ("depositing_date", "open_date", "pl_date_advertise",
                "pl_last_deposit_date", "pl_rejection_date"):
        r = conn.execute(
            f"SELECT COUNT({col}), MIN({col}), MAX({col}) FROM plans"
        ).fetchone()
        span = f"{str(r[1])[:10]} .. {str(r[2])[:10]}" if r[0] else "-"
        print(f"    {col:22} {r[0]:>6} filled   {span}")

    print("\n  sync history:")
    runs = conn.execute(
        """SELECT id, mode, plans_seen, plans_new, status_changes, notifications,
           ok, started_at FROM sync_runs ORDER BY id DESC LIMIT 8"""
    ).fetchall()
    for r in runs:
        flag = "ok" if r["ok"] else "FAILED"
        print(f"    #{r['id']:<3} {r['started_at'][:19]}  {r['mode']:<11} "
              f"seen={r['plans_seen']:<6} new={r['plans_new']:<6} "
              f"changed={r['status_changes']:<4} notif={r['notifications']:<4} {flag}")


def cmd_pending(conn, args) -> None:
    rows = conn.execute(
        """SELECT n.id, n.kind, n.created_at, n.message, s.label, s.client_id
           FROM notifications n JOIN subscriptions s ON s.id = n.subscription_id
           WHERE n.delivered = 0 ORDER BY n.created_at DESC LIMIT ?""",
        (args.limit,),
    ).fetchall()
    print(f"{len(rows)} undelivered notification(s):\n")
    for r in rows:
        print(f"  #{r['id']} [{r['kind']}] {r['created_at'][:19]} "
              f"client={r['client_id']} ({r['label']})")
        print(f"      {r['message']}\n")
    if rows:
        print("Mark as delivered after your mail/dashboard layer sends them:")
        print("  UPDATE notifications SET delivered=1 WHERE id IN (...);")


def cmd_at(conn, args) -> None:
    """Which plans cover, or sit near, a coordinate."""
    import sync  # imported lazily: pulls in shapely

    from shapely.geometry import Point

    lat, lon = args.lat, args.lon
    pad_lon, pad_lat = sync._radius_pads(args.radius, lat)
    candidates = db.plans_covering_point(conn, lon, lat,
                                         pad_lon=pad_lon, pad_lat=pad_lat)
    point = Point(lon, lat)

    hits = []
    for plan in candidates:
        hit, method = sync._point_hits_plan(point, plan, args.radius)
        if not hit:
            continue
        geom = sync._load_geometry(plan)
        distance = 0.0 if method == "point_in_polygon" else sync._distance_m(geom, point, lat)
        hits.append((plan, method, distance))
    hits.sort(key=lambda t: t[2])

    scope = f" (or within {args.radius:.0f} m)" if args.radius else ""
    print(f"Point {lat}, {lon}{scope}")
    print(f"  {len(candidates)} bbox candidates -> {len(hits)} actual match(es)\n")
    for plan, method, distance in hits:
        where = "covers point" if method == "point_in_polygon" else f"{distance:.0f} m away"
        print(f"  #{plan['object_id']}  {plan['pl_number']}  [{plan['short_status'] or plan['station']}]  ({where})")
        print(f"      {plan['pl_name']}")
        print(f"      {plan['jurisdiction_name']}  {plan['area_dunam']} dunam  "
              f"units={plan['housing_units']}  updated {str(plan['last_update'])[:10]}")
        print(f"      {plan['pl_url']}\n")


def cmd_plan(conn, args) -> None:
    plan = conn.execute("SELECT * FROM plans WHERE object_id=?",
                        (args.object_id,)).fetchone()
    if plan is None:
        print(f"No plan with object_id={args.object_id}")
        return

    print(f"Plan #{plan['object_id']}  {plan['pl_number']}")
    print(f"  {plan['pl_name']}\n")
    for label, key in (("מחוז", "county_name"), ("רשות", "jurisdiction_name"),
                       ("מרחב תכנון", "plan_area_name"), ("סוג", "entity_subtype"),
                       ("סטטוס", "station"), ("סטטוס מקוצר", "short_status"),
                       ("שטח (דונם)", "area_dunam"), ('יח"ד', "housing_units"),
                       ("ייעודי קרקע", "landuse")):
        if plan[key] is not None:
            print(f"  {label:14} {plan[key]}")
    print(f"  {'קישור':14} {plan['pl_url']}")

    print("\n  milestones (from the source, not observed by us):")
    for key, label in MILESTONES:
        if plan[key]:
            print(f"    {str(plan[key])[:10]}   {label}")

    log = conn.execute(
        """SELECT old_station, new_station, detected_at FROM plan_status_log
           WHERE object_id=? ORDER BY id""", (args.object_id,)).fetchall()
    print(f"\n  transitions PlanWatch observed: {len(log)}")
    for r in log:
        print(f"    {r['detected_at'][:19]}   {r['old_station']} -> {r['new_station']}")

    if plan["objectives"]:
        print(f"\n  מטרות:\n    {plan['objectives'][:600]}")


def cmd_history(conn, args) -> None:
    sql = """SELECT l.*, p.pl_number, p.pl_name FROM plan_status_log l
             JOIN plans p ON p.object_id = l.object_id"""
    params: list = []
    if args.object_id:
        sql += " WHERE l.object_id=?"
        params.append(args.object_id)
    sql += " ORDER BY l.id DESC LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(sql, params).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM plan_status_log").fetchone()[0]
    print(f"Observed status transitions: {total} total, showing {len(rows)}\n")
    if not total:
        print("  None yet. This table only fills as sync.py runs over time -\n"
              "  the source exposes current state only, so it cannot be backfilled.\n"
              "  For dates the source DOES publish, use `report.py timeline`.")
        return
    for r in rows:
        print(f"  {r['detected_at'][:19]}  #{r['object_id']} {r['pl_number']}")
        print(f"      {r['old_station']} -> {r['new_station']}")
        print(f"      {(r['pl_name'] or '')[:70]}")


def cmd_timeline(conn, args) -> None:
    """Plans per year, from the source's own milestone dates."""
    field = args.field
    rows = conn.execute(
        f"""SELECT substr({field},1,4) y, COUNT(*) c, SUM(housing_units) u
            FROM plans WHERE {field} IS NOT NULL
            GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    total = sum(r["c"] for r in rows)
    print(f"Plans by year of {field} ({total:,} plans have this date):\n")
    peak = max((r["c"] for r in rows), default=1)
    for r in rows:
        bar = "#" * max(1, int(r["c"] / peak * 40))
        units = f'  {r["u"]:>8,} יח"ד' if r["u"] else ""
        print(f"  {r['y']}  {r['c']:>6,}  {bar}{units}")


def cmd_search(conn, args) -> None:
    where, params = _filters(args)
    total = conn.execute(f"SELECT COUNT(*) FROM plans WHERE {where}",
                         params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT * FROM plans WHERE {where}
            ORDER BY last_update DESC LIMIT ?""", params + [args.limit]).fetchall()
    print(f"{total:,} plans match; showing {len(rows)}\n")
    _print_table(rows, ("object_id", "pl_number", "jurisdiction_name",
                        "short_status", "housing_units", "pl_name"))


def cmd_export(conn, args) -> None:
    where, params = _filters(args)
    if args.format == "geojson":
        where += " AND geometry_json IS NOT NULL"
    rows = conn.execute(f"SELECT * FROM plans WHERE {where}", params).fetchall()

    if args.format == "csv":
        with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
            # utf-8-sig so Excel opens Hebrew correctly instead of mojibake
            writer = csv.writer(fh)
            writer.writerow(LIST_COLUMNS)
            for r in rows:
                writer.writerow([r[c] for c in LIST_COLUMNS])
    else:
        features = [
            {"type": "Feature",
             "geometry": json.loads(r["geometry_json"]),
             "properties": {c: r[c] for c in LIST_COLUMNS}}
            for r in rows
        ]
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"type": "FeatureCollection", "features": features},
                      fh, ensure_ascii=False)

    print(f"Wrote {len(rows):,} plans to {args.out} ({args.format}).")
    if args.format == "geojson":
        print("  Open it in QGIS or Google Earth to see the polygons on a map.")


def _add_filter_flags(parser) -> None:
    parser.add_argument("--jurisdiction", help="רשות מקומית (substring match)")
    parser.add_argument("--county", help="מחוז (substring match)")
    parser.add_argument("--status", help="station / short_status (substring)")
    parser.add_argument("--text", help="search pl_name, pl_number, objectives")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only plans with last_update on/after this date")
    parser.add_argument("--min-units", type=int, help='minimum יח"ד')


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Query and export PlanWatch results.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="overview, coverage and sync history")

    p = sub.add_parser("pending", help="undelivered notifications")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("at", help="plans covering / near a coordinate")
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("-r", "--radius", type=float, default=None,
                   help="also include plans within this many metres")

    p = sub.add_parser("plan", help="one plan in full, with milestones")
    p.add_argument("object_id", type=int)

    p = sub.add_parser("history", help="status transitions PlanWatch observed")
    p.add_argument("--object-id", type=int)
    p.add_argument("--limit", type=int, default=25)

    p = sub.add_parser("timeline", help="plans per year from source dates")
    p.add_argument("--field", default="depositing_date",
                   choices=[m[0] for m in MILESTONES],
                   help="which milestone date to bucket by")

    p = sub.add_parser("search", help="filter the plan table")
    _add_filter_flags(p)
    p.add_argument("--limit", type=int, default=25)

    p = sub.add_parser("export", help="write matching plans to CSV or GeoJSON")
    _add_filter_flags(p)
    p.add_argument("--format", choices=("csv", "geojson"), default="csv")
    p.add_argument("--out", required=True, help="output file path")

    args = parser.parse_args()
    handler = {
        "stats": cmd_stats, "pending": cmd_pending, "at": cmd_at,
        "plan": cmd_plan, "history": cmd_history, "timeline": cmd_timeline,
        "search": cmd_search, "export": cmd_export,
    }[args.cmd]

    conn = db.get_conn()
    try:
        handler(conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
