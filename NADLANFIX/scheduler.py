"""
PlanWatch - daily scheduler (תזמון יומי)
=========================================
Runs the whole pipeline unattended, in dependency order, with per-job state so a
failure in one job never silently stops the rest.

    python scheduler.py run            # one full cycle, now
    python scheduler.py run --only sync,alerts
    python scheduler.py daemon         # loop forever, sleeping between cycles
    python scheduler.py status         # last run of every job
    python scheduler.py install        # print the Windows/cron install command

Job order is a real dependency chain, not a preference
------------------------------------------------------
    sync        plans from the national service      (everything keys off these)
      -> renewal / market / registry / tenders / municipal   (independent feeds)
        -> documents        needs `appraisals` rows to have links to fetch
        -> owners           built FROM appraisals + tender winners
          -> index          built FROM owners + every other table
            -> grids        spatial helpers + post-import cross-reference
              -> alerts     needs municipal + tenders + renewal to have signals

Running `index` before `owners` produces an index missing 30k names, and it
fails silently - the index just comes out smaller. Hence `ORDER` is explicit and
`run_cycle` follows it.

Frequency: what actually changes
--------------------------------
Daily is right for `sync` (the source is day-resolution) and for `alerts`.
The heavy reference feeds barely move, so re-pulling 2.86M registry rows every
night is waste. `INTERVAL_HOURS` encodes this per job, and a job is skipped when
its last success is inside its interval - `--force` overrides.

Cancellation
------------
`run_cycle` takes `should_stop`. Every job checks it, so a daemon can be stopped
between jobs rather than mid-write.
"""

from __future__ import annotations

import time
import traceback

#: job -> (callable-name, minimum hours between runs). Order matters; see above.
ORDER = (
    "sync", "renewal", "market", "registry", "tenders", "municipal",
    "documents", "owners", "index", "grids", "alerts",
    "yad2", "facebook", "onmap",
)

INTERVAL_HOURS = {
    "sync": 12,          # source is day-resolution; twice a day is plenty
    "alerts": 6,         # cheap, and the point of the whole system
    "renewal": 24 * 7,   # ministry publishes in batches
    "market": 24 * 7,    # CBS is monthly, מחיר למשתכן is periodic
    "tenders": 24,        # new tenders appear daily and have deadlines
    "municipal": 24 * 3,
    "registry": 24 * 7,  # 30k appraisals; weekly is generous
    "documents": 24,     # incremental: fetches a batch of unread PDFs
    "owners": 24,        # derived; cheap
    "index": 24,         # derived; cheap
    "grids": 24 * 3,     # spatial helper tables; only change when muni does
    # Daily, and daily is the point: one observation per listing per day is
    # what turns a board into a price history. More often would add cost and
    # anti-bot exposure without adding a data point the maths can use, since
    # every derived figure is measured in days.
    "yad2": 24,
    "facebook": 24,
    "onmap": 24,
    # The policy rate moves eight times a year on announced dates and the
    # mortgage statistics are monthly, but the flow is small and a stale rate
    # mis-prices every payment on the screen at once.
    "finance": 24,
    # Stops and schools change on the timescale of infrastructure projects.
    # Weekly is already far more often than the registers are revised.
    "amenities": 24 * 7,
    # Daily, for the same reason the sale harvest is: the rent board is the
    # denominator of every yield on screen, and it turns over faster than the
    # sale board does. 160 pages a run, which is a small ask of the source.
    "rentals": 24,
}

DEFAULT_SLEEP_MINUTES = 60

TABLES = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    error       TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sched_job ON scheduler_runs (job, started_at);
"""


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


# ---------------------------------------------------------------------- jobs

def _job_sync(conn, should_stop=None) -> dict:
    import sync
    return sync.sync_plans(mode="incremental", should_stop=should_stop)


def _job_renewal(conn, should_stop=None) -> dict:
    import urban_renewal_client as ur
    return {"complexes": ur.import_all(conn, should_stop=should_stop)}


def _job_market(conn, should_stop=None) -> dict:
    import market_client as mk
    return {"rows": mk.import_all(conn, should_stop=should_stop)}


def _job_rentals(conn, should_stop=None) -> dict:
    import onmap_rent
    return onmap_rent.harvest()


def _job_finance(conn, should_stop=None) -> dict:
    import finance_client as fc
    return {"observations": fc.import_rates(conn, should_stop=should_stop)}


def _job_amenities(conn, should_stop=None) -> dict:
    import amenity_client as am
    rows = am.import_all(conn, should_stop=should_stop)
    # The point indexes are built from the table at first use and would
    # otherwise serve the previous import for the life of the process.
    am.clear_cache()
    return {"rows": rows}


def _job_registry(conn, should_stop=None) -> dict:
    import appraisal_client as ac
    return {"rows": ac.import_all(conn, should_stop=should_stop)}


def _job_tenders(conn, should_stop=None) -> dict:
    import tenders_client as tc
    total = tc.import_tenders(conn, should_stop=should_stop)
    lots = tc.import_lots(conn, limit=150, should_stop=should_stop)
    return {"tenders": total, "lots": lots}


def _job_municipal(conn, should_stop=None) -> dict:
    import municipal_client as mn
    return {"rows": mn.import_all(conn, should_stop=should_stop)}


def _job_documents(conn, should_stop=None) -> dict:
    """
    Incremental by construction: `fetch_batch` selects only appraisals with no
    stored document, so each nightly run chips away at the backlog instead of
    re-downloading 30k PDFs.

    The batch is sized to the measured throughput (~9s per document, fetched
    and parsed concurrently), so a cycle spends roughly an hour here and the
    ~30k backlog clears in months rather than years. These documents carry the
    ruled ₪ amounts, which is the most valuable content in the system.
    """
    import document_client as dc
    return dc.fetch_batch(conn, limit=400, should_stop=should_stop)


def _job_owners(conn, should_stop=None) -> dict:
    import owners_client as oc
    return oc.rebuild(conn, should_stop=should_stop)


def _job_index(conn, should_stop=None) -> dict:
    import entity_index as ei
    return ei.rebuild(conn, should_stop=should_stop)


def _job_grids(conn, should_stop=None) -> dict:
    """
    Rebuild the spatial grid helpers.

    Without these, the dormant-land and dangerous-x-permit joins fall back to
    four-sided bbox range predicates, which cannot use an index past its first
    column: measured at 89.8s for one join, and the parcel-vs-building version
    never finished at all.
    """
    import dormant
    out = {"buildings": dormant.build_grid(conn),
           "permits": dormant.build_permit_grid(conn)}
    # Re-run the cross-reference now that everything is loaded; read-only.
    out["crossref"] = {"links": dormant.crossref_new(conn, limit=5)["total_links"]}
    # recompute=True: this is the one place the expensive 45k-parcel scan runs.
    out["dormant"] = dormant.refresh_summary(conn)
    return out


def _job_alerts(conn, should_stop=None) -> dict:
    """
    Baseline first, then watch, then deliver.

    `seed_baseline` is safe to re-run - it only inserts rows that are missing -
    and running it here means a subscription created since the last cycle is
    baselined before its first watch. Without that, a new subscriber's first
    digest contains the parcel's entire history.
    """
    import alerts
    seeded = alerts.seed_baseline(conn)
    watched = alerts.watch_all(conn)
    delivered = alerts.deliver_pending(conn)
    return {"seeded": seeded.get("baseline_rows_added"),
            "raised": watched.get("raised"),
            "by_watcher": watched.get("by_watcher"),
            "delivered": delivered.get("delivered"),
            "pending": delivered.get("pending"),
            "failures": delivered.get("failures")}


def _job_yad2(conn, should_stop=None) -> dict:
    """One full pass over the local Yad2 connector, into PlanWatch's own store.

    This job is what makes the lead engine worth anything. Days-on-market and
    price movement are not properties of a listing you can read off the board -
    they exist only if somebody wrote down what the board said yesterday. Each
    run appends to `yad2_price_history`, so the observation window deepens by a
    day every day and "this seller has cut twice in three weeks" becomes a fact
    we hold rather than a claim we cannot support.

    Runs synchronously here (not via `start()`) because the scheduler already
    owns a worker thread and serialises its jobs; two harvests at once would
    only fight for the connector's browser renders.
    """
    import yad2_feed
    result = yad2_feed.harvest(target=18000, workers=yad2_feed.DEFAULT_WORKERS)
    return {k: result.get(k) for k in
            ("status", "pages_fetched", "pages_failed", "rows_seen",
             "rows_new", "rows_stored", "error")}


def _job_facebook(conn, should_stop=None) -> dict:
    """Harvest Facebook Marketplace listings into PlanWatch's own store.

    Runs synchronously here (not via `start()`) because the scheduler already
    owns a worker thread and serialises its jobs.
    """
    from jobs.daily_ingest import run_daily_ingest
    result = run_daily_ingest(sources={"facebook": {"target": 500, "enabled": True}})
    return result["results"][0] if result["results"] else {"status": "skipped"}


def _job_onmap(conn, should_stop=None) -> dict:
    """Harvest ONMAP listings into PlanWatch's own store."""
    from jobs.daily_ingest import run_daily_ingest
    result = run_daily_ingest(sources={"onmap": {"target": 5000, "enabled": True}})
    return result["results"][0] if result["results"] else {"status": "skipped"}


JOBS = {
    "sync": _job_sync, "renewal": _job_renewal, "market": _job_market,
    "registry": _job_registry, "tenders": _job_tenders,
    "municipal": _job_municipal, "documents": _job_documents,
    "owners": _job_owners, "index": _job_index, "grids": _job_grids,
    "alerts": _job_alerts, "yad2": _job_yad2, "facebook": _job_facebook,
    "onmap": _job_onmap, "finance": _job_finance, "amenities": _job_amenities,
    "rentals": _job_rentals,
}


# ----------------------------------------------------------------- execution

def last_success(conn, job) -> str | None:
    ensure_schema(conn)
    row = conn.execute(
        """SELECT finished_at FROM scheduler_runs
           WHERE job = ? AND ok = 1 AND skipped = 0
           ORDER BY id DESC LIMIT 1""", (job,)).fetchone()
    return row[0] if row else None


def _due(conn, job, force=False) -> tuple[bool, str | None]:
    if force:
        return True, None
    last = last_success(conn, job)
    if not last:
        return True, None
    hours = INTERVAL_HOURS.get(job, 24)
    from datetime import datetime, timedelta, timezone
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True, None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    nxt = when + timedelta(hours=hours)
    if datetime.now(timezone.utc) < nxt:
        return False, f"last success {last[:16]}, next due {nxt.isoformat()[:16]}"
    return True, None


def run_job(conn, job, force=False, should_stop=None) -> dict:
    """Run one job, recording start/finish and any error."""
    import db as _db
    import json as _json

    ensure_schema(conn)
    if job not in JOBS:
        raise ValueError(f"unknown job {job!r}; expected one of {list(JOBS)}")

    due, why = _due(conn, job, force)
    if not due:
        conn.execute(
            """INSERT INTO scheduler_runs (job, started_at, finished_at, ok,
                                           skipped, detail)
               VALUES (?,?,?,1,1,?)""",
            (job, _db.now_iso(), _db.now_iso(), why))
        conn.commit()
        return {"job": job, "skipped": True, "reason": why}

    started = _db.now_iso()
    cur = conn.execute(
        "INSERT INTO scheduler_runs (job, started_at) VALUES (?,?)",
        (job, started))
    run_id = cur.lastrowid
    conn.commit()

    try:
        detail = JOBS[job](conn, should_stop=should_stop)
        # A cooperative cancellation is not a successful run.  In particular,
        # sync.sync_plans() deliberately returns {"cancelled": True} after it
        # has recorded its own failed sync_run, so treating that dictionary as
        # success here made the scheduler dashboard claim a completed cycle.
        # Keep the two audit trails consistent and make the next cycle retry
        # the job instead of applying its normal success interval.
        if isinstance(detail, dict) and detail.get("cancelled"):
            error = "cancelled by user"
            conn.execute(
                """UPDATE scheduler_runs SET finished_at=?, ok=0, error=?
                   WHERE id=?""", (_db.now_iso(), error, run_id))
            conn.commit()
            return {"job": job, "ok": False, "cancelled": True,
                    "error": error, "detail": detail}
        conn.execute(
            """UPDATE scheduler_runs SET finished_at=?, ok=1, detail=?
               WHERE id=?""",
            (_db.now_iso(), _json.dumps(detail, ensure_ascii=False)[:2000],
             run_id))
        conn.commit()
        return {"job": job, "ok": True, "detail": detail}
    except Exception as exc:
        # Recorded, not raised: one broken feed must not stop the cycle.
        error = f"{type(exc).__name__}: {exc}"[:500]
        conn.execute(
            """UPDATE scheduler_runs SET finished_at=?, ok=0, error=?
               WHERE id=?""", (_db.now_iso(), error, run_id))
        conn.commit()
        return {"job": job, "ok": False, "error": error,
                "traceback": traceback.format_exc(limit=3)}


def run_cycle(conn, only=None, force=False, should_stop=None) -> dict:
    """
    Run every job once, in dependency order.

    A failing job is recorded and the cycle continues - the alternative is that
    one upstream outage blocks alerts for the day.
    """
    jobs = [j for j in ORDER if not only or j in only]
    out = {"jobs": [], "ok": 0, "failed": 0, "skipped": 0}
    for job in jobs:
        if should_stop and should_stop():
            out["stopped"] = True
            break
        result = run_job(conn, job, force=force, should_stop=should_stop)
        out["jobs"].append(result)
        if result.get("skipped"):
            out["skipped"] += 1
        elif result.get("ok"):
            out["ok"] += 1
        else:
            out["failed"] += 1
    return out


def status(conn, limit=20) -> dict:
    ensure_schema(conn)
    jobs = []
    for job in ORDER:
        row = conn.execute(
            """SELECT started_at, finished_at, ok, skipped, detail, error
               FROM scheduler_runs WHERE job=? ORDER BY id DESC LIMIT 1""",
            (job,)).fetchone()
        due, why = _due(conn, job)
        jobs.append({
            "job": job, "interval_hours": INTERVAL_HOURS.get(job, 24),
            "last_success": last_success(conn, job),
            "due_now": due, "why_not": why,
            "last": dict(row) if row else None,
        })
    recent = [dict(r) for r in conn.execute(
        """SELECT id, job, started_at, finished_at, ok, skipped, error
           FROM scheduler_runs ORDER BY id DESC LIMIT ?""", (limit,))]
    return {"jobs": jobs, "recent": recent, "order": list(ORDER)}


def daemon(conn_factory, sleep_minutes=DEFAULT_SLEEP_MINUTES, should_stop=None):
    """
    Loop forever, running a cycle then sleeping.

    A fresh connection per cycle: a long-lived SQLite handle across hours of
    idling is asking for lock trouble, and reconnecting costs nothing.
    """
    while True:
        if should_stop and should_stop():
            return
        conn = conn_factory()
        try:
            result = run_cycle(conn, should_stop=should_stop)
            print(f"[cycle] ok={result['ok']} failed={result['failed']} "
                  f"skipped={result['skipped']}")
            for job in result["jobs"]:
                if job.get("error"):
                    print(f"   !! {job['job']}: {job['error'][:120]}")
        finally:
            conn.close()
        for _ in range(int(sleep_minutes * 60)):
            if should_stop and should_stop():
                return
            time.sleep(1)


def install_hint() -> dict:
    """How to register this with the OS scheduler."""
    import sys
    from pathlib import Path
    python = sys.executable
    script = Path(__file__).resolve()
    return {
        "windows": (
            'schtasks /Create /TN "PlanWatch daily" /SC HOURLY /MO 6 '
            f'/TR "\\"{python}\\" \\"{script}\\" run" /F'),
        "windows_note": "רץ כל 6 שעות; כל job מדלג לבד אם לא הגיע זמנו.",
        "cron": f"0 */6 * * * {python} {script} run >> planwatch-cron.log 2>&1",
        "daemon": f"{python} {script} daemon",
        "verify": f"{python} {script} status",
    }


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    force = "--force" in args
    only = None
    if "--only" in args:
        only = set(args[args.index("--only") + 1].split(","))

    if cmd == "install":
        print(json.dumps(install_hint(), ensure_ascii=False, indent=1))
    elif cmd == "daemon":
        minutes = DEFAULT_SLEEP_MINUTES
        if "--minutes" in args:
            minutes = float(args[args.index("--minutes") + 1])
        print(f"PlanWatch scheduler daemon; cycle every {minutes} min. Ctrl+C to stop.")
        try:
            daemon(db.get_conn, sleep_minutes=minutes)
        except KeyboardInterrupt:
            print("\nstopped.")
    elif cmd == "run":
        conn = db.get_conn()
        result = run_cycle(conn, only=only, force=force)
        print(json.dumps(result, ensure_ascii=False, indent=1)[:3000])
        conn.close()
    else:
        conn = db.get_conn()
        st = status(conn)
        print(f"{'job':12} {'every':>7} {'due':>5}  last success")
        for j in st["jobs"]:
            print(f"  {j['job']:10} {j['interval_hours']:>5}h "
                  f"{'YES' if j['due_now'] else ' no':>5}  "
                  f"{(j['last_success'] or '-')[:19]}")
        conn.close()
