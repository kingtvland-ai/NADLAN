"""
PlanWatch - alert delivery + signal watchers (התראות ומסירה)
=============================================================
Closes the last gap recorded in PROJECT-MAP.md: notifications were written to
`notifications` with `delivered=0` and nothing ever sent them.

Two halves:

**1. Watchers.** `sync.py` only ever raised alerts on plan status changes -
that was the only signal the system had when it was written. Since then five
more arrived, and each is worth a subscriber's attention:

| watcher | signal | why a broker cares |
|---|---|---|
| `dangerous` | a מבנה מסוכן order near a watched parcel | strongest renewal trigger there is |
| `permits` | a new building permit, especially תמ"א 38 | someone is already moving |
| `renewal` | a declared renewal complex covering the parcel | the area is now a target |
| `tenders` | an OPEN RAMI tender on/near the parcel | land actually for sale, with a deadline |
| `appraisals` | a new decisive appraisal on the parcel | a levy dispute = value was created |

**2. Delivery.** `deliver_pending()` renders each notification and hands it to a
channel. Channels are pluggable; `console` and `file` need no configuration, and
`email` activates when SMTP env vars are present.

Why delivery is deliberately conservative
-----------------------------------------
* **Idempotent.** A notification is marked `delivered=1` only after the channel
  reports success. A crash mid-send re-sends rather than silently dropping - for
  an alerting system, a duplicate is a nuisance and a miss is a failure.
* **Batched per subscriber.** Twelve separate emails about one parcel is how
  people mute an alert channel. One digest per subscriber per run.
* **Bootstrap-aware.** `watch_all()` on a fresh subscription would fire an alert
  for every historical fact about the parcel. `seed_baseline()` records what was
  already true at subscribe time so only genuinely NEW things alert. This is the
  same trap already fixed once in `sync.py`.

SMTP configuration
------------------
    PLANWATCH_SMTP_HOST     PLANWATCH_SMTP_PORT   (default 587)
    PLANWATCH_SMTP_USER     PLANWATCH_SMTP_PASS
    PLANWATCH_SMTP_FROM     PLANWATCH_SMTP_TLS    (default 1)

Absent any of these, the email channel reports itself unconfigured rather than
failing at send time.
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

TABLES = """
/* What was already true when a subscription was created, so the first watch
   run does not alert on the parcel's entire history. */
CREATE TABLE IF NOT EXISTS alert_baseline (
    subscription_id INTEGER NOT NULL,
    watcher     TEXT NOT NULL,
    signal_key  TEXT NOT NULL,     -- stable id of the thing seen
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (subscription_id, watcher, signal_key)
);

/* One row per delivery attempt - the audit trail for "did the client get it". */
CREATE TABLE IF NOT EXISTS alert_deliveries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT,
    channel     TEXT NOT NULL,
    target      TEXT,              -- address / path actually used
    count       INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT,
    sent_at     TEXT NOT NULL
);

/* Per-subscriber delivery preferences. */
CREATE TABLE IF NOT EXISTS alert_channels (
    client_id   TEXT PRIMARY KEY,
    channel     TEXT NOT NULL DEFAULT 'console',   -- console | file | email
    target      TEXT,                              -- email address / file path
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_baseline_sub ON alert_baseline (subscription_id);
CREATE INDEX IF NOT EXISTS idx_deliv_at     ON alert_deliveries (sent_at);
"""

#: Watchers, in the order they run. Keys are stored in `alert_baseline.watcher`
#: and in `notifications.kind`, so renaming one orphans its baseline.
WATCHERS = ("dangerous", "permits", "renewal", "tenders", "appraisals")

DEFAULT_RADIUS_M = 300.0


def ensure_schema(conn) -> None:
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    conn.commit()


def _has(conn, table) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone())


def _subs(conn, subscription_id=None) -> list:
    where = "active = 1"
    params: list = []
    if subscription_id:
        where += " AND id = ?"
        params.append(subscription_id)
    return conn.execute(
        f"SELECT * FROM subscriptions WHERE {where} ORDER BY id", params
    ).fetchall()


# ------------------------------------------------------------------ signals

def _dangerous_signals(conn, sub) -> list[tuple[str, str, dict]]:
    """Dangerous-building orders near the watched point."""
    if not (_has(conn, "muni_dangerous") and sub["lat"] and sub["lon"]):
        return []
    import municipal_client as mn
    radius = sub["radius_m"] or DEFAULT_RADIUS_M
    out = []
    for row in mn.dangerous_near(conn, sub["lat"], sub["lon"],
                                 radius_m=radius, limit=50):
        key = f"{row['city']}/{row['object_id']}"
        severity = mn.danger_severity(row["order_kind"])
        address = " ".join(str(x) for x in (row.get("street"),
                                            row.get("house_num")) if x)
        out.append((key,
                    f"מבנה מסוכן {address} — {row['order_kind']} "
                    f"({round(row['distance_m'])} מ׳)",
                    {"severity": severity, "distance_m": row["distance_m"],
                     "order_kind": row["order_kind"], "address": address}))
    return out


def _permit_signals(conn, sub) -> list[tuple[str, str, dict]]:
    """New building permits near the watched point."""
    if not (_has(conn, "muni_permits") and sub["lat"] and sub["lon"]):
        return []
    import municipal_client as mn
    radius = sub["radius_m"] or DEFAULT_RADIUS_M
    out = []
    for row in mn.permits_near(conn, sub["lat"], sub["lon"],
                               radius_m=radius, limit=50):
        key = f"{row['city']}/{row['object_id']}"
        tag = 'תמ"א 38 · ' if row.get("is_tama38") else ""
        units = f"{row['housing_units']} יח\"ד · " if row.get("housing_units") else ""
        out.append((key,
                    f"{tag}בקשת היתר {row.get('addresses') or ''} — "
                    f"{units}{row.get('stage') or ''}",
                    {"is_tama38": row.get("is_tama38"),
                     "units": row.get("housing_units"),
                     "stage": row.get("stage"),
                     "addresses": row.get("addresses")}))
    return out


def _renewal_signals(conn, sub) -> list[tuple[str, str, dict]]:
    """Declared urban-renewal complexes covering or near the parcel."""
    if not (_has(conn, "urban_renewal") and sub["lat"] and sub["lon"]):
        return []
    import urban_renewal_client as ur
    radius = sub["radius_m"] or 1500.0
    out = []
    try:
        near = ur.near_point(conn, sub["lat"], sub["lon"], radius_m=radius,
                             limit=20)
    except Exception:
        return []
    for row in near:
        key = str(row.get("mitham_id") or row.get("object_id"))
        out.append((key,
                    f"מתחם התחדשות {row.get('name') or key} — "
                    f"{row.get('status') or ''} · "
                    f"{row.get('units_existing') or '?'} דירות קיימות",
                    {"mitham_id": row.get("mitham_id"),
                     "status": row.get("status"),
                     "units_existing": row.get("units_existing")}))
    return out


def _tender_signals(conn, sub) -> list[tuple[str, str, dict]]:
    """
    RAMI tenders on the watched parcel. Only OPEN ones alert - a closed tender
    is history, and an alert a subscriber cannot act on trains them to ignore
    the channel.
    """
    if not (_has(conn, "tender_lots") and sub["gush"]):
        return []
    import tenders_client as tc
    out = []
    for row in tc.lots_for_parcel(conn, sub["gush"], sub["helka"], limit=30):
        if row.get("status") and "פתוח" not in str(row["status"]) \
                and "נפתח" not in str(row["status"]):
            continue
        key = f"{row['michraz_id']}/{row['tik_id']}"
        price = (f" · שומה {int(row['appraised_price']):,} ₪"
                 if row.get("appraised_price") else "")
        out.append((key,
                    f"מכרז {row.get('name')} על החלקה — {row.get('status')}"
                    f"{price} · נסגר {row.get('closes_at') or '?'}",
                    {"michraz_id": row["michraz_id"],
                     "closes_at": row.get("closes_at"),
                     "appraised_price": row.get("appraised_price")}))
    return out


def _appraisal_signals(conn, sub) -> list[tuple[str, str, dict]]:
    """Decisive appraisals on the watched parcel."""
    if not (_has(conn, "appraisal_parcels") and sub["gush"]):
        return []
    import appraisal_client as ac
    out = []
    for row in ac.appraisals_for_parcel(conn, sub["gush"], sub["helka"],
                                        limit=30):
        key = str(row["id"])
        out.append((key,
                    f"שמאות מכריעה {row.get('decision_date')} — "
                    f"{row.get('appraisal_type')} · {row.get('committee')}",
                    {"appraisal_id": row["id"],
                     "decision_date": row.get("decision_date"),
                     "committee": row.get("committee")}))
    return out


_SIGNAL_FNS = {
    "dangerous": _dangerous_signals,
    "permits": _permit_signals,
    "renewal": _renewal_signals,
    "tenders": _tender_signals,
    "appraisals": _appraisal_signals,
}


# ------------------------------------------------------------------ watching

def seed_baseline(conn, subscription_id=None, watchers=WATCHERS) -> dict:
    """
    Record what is ALREADY true, without alerting.

    Run this when a subscription is created. Without it the first `watch_all()`
    fires one alert per historical fact - a parcel in Tel Aviv would produce
    dozens at once, which is how an alert channel gets muted on day one. The
    same mistake was already made and fixed in `sync.py`; this is the fix
    applied up front.
    """
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    added = 0
    for sub in _subs(conn, subscription_id):
        for watcher in watchers:
            for key, _msg, _meta in _SIGNAL_FNS[watcher](conn, sub):
                cur = conn.execute(
                    """INSERT INTO alert_baseline
                         (subscription_id, watcher, signal_key, seen_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT DO NOTHING""",
                    (sub["id"], watcher, key, now))
                added += cur.rowcount
    conn.commit()
    return {"baseline_rows_added": added}


def watch_all(conn, subscription_id=None, watchers=WATCHERS,
              dry_run=False) -> dict:
    """
    Check every watcher for every active subscription and raise notifications
    for signals not already in the baseline.

    A raised signal is added to the baseline in the same transaction, so it
    cannot alert twice.
    """
    import db as _db

    ensure_schema(conn)
    now = _db.now_iso()
    raised: list[dict] = []
    per_watcher: dict = {w: 0 for w in watchers}

    for sub in _subs(conn, subscription_id):
        known = {(r["watcher"], r["signal_key"]) for r in conn.execute(
            "SELECT watcher, signal_key FROM alert_baseline WHERE subscription_id=?",
            (sub["id"],))}
        for watcher in watchers:
            for key, message, meta in _SIGNAL_FNS[watcher](conn, sub):
                if (watcher, key) in known:
                    continue
                label = sub["label"] or (
                    f"גוש {sub['gush']} חלקה {sub['helka']}"
                    if sub["gush"] else f"{sub['lat']},{sub['lon']}")
                text = f"{label}: {message}"
                raised.append({"subscription_id": sub["id"],
                               "client_id": sub["client_id"],
                               "watcher": watcher, "key": key,
                               "message": text, "meta": meta})
                per_watcher[watcher] += 1
                if not dry_run:
                    # object_id is NULL: none of these signals is plan-scoped.
                    # It used to be NOT NULL with an FK to plans, and a 0
                    # sentinel failed the constraint - see the migration in
                    # db.py.
                    conn.execute(
                        """INSERT INTO notifications
                             (subscription_id, object_id, kind, message,
                              created_at, delivered)
                           VALUES (?,NULL,?,?,?,0)""",
                        (sub["id"], watcher, text, now))
                    conn.execute(
                        """INSERT INTO alert_baseline
                             (subscription_id, watcher, signal_key, seen_at)
                           VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
                        (sub["id"], watcher, key, now))
    if not dry_run:
        conn.commit()
    return {"raised": len(raised), "by_watcher": per_watcher,
            "dry_run": dry_run, "items": raised[:50]}


# ------------------------------------------------------------------ channels

def smtp_config() -> dict:
    host = (os.environ.get("PLANWATCH_SMTP_HOST") or "").strip()
    return {
        "configured": bool(host),
        "host": host or None,
        "port": int(os.environ.get("PLANWATCH_SMTP_PORT") or 587),
        "user": (os.environ.get("PLANWATCH_SMTP_USER") or "").strip() or None,
        "sender": (os.environ.get("PLANWATCH_SMTP_FROM") or "").strip() or None,
        "tls": (os.environ.get("PLANWATCH_SMTP_TLS") or "1") != "0",
        "env": ["PLANWATCH_SMTP_HOST", "PLANWATCH_SMTP_PORT",
                "PLANWATCH_SMTP_USER", "PLANWATCH_SMTP_PASS",
                "PLANWATCH_SMTP_FROM", "PLANWATCH_SMTP_TLS"],
    }


def _render(client_id, rows) -> tuple[str, str]:
    """Digest subject + body for one subscriber."""
    kinds = {}
    for row in rows:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    summary = " · ".join(f"{v} {k}" for k, v in kinds.items())
    subject = f"PlanWatch: {len(rows)} עדכונים ({summary})"
    lines = [f"שלום {client_id},", "",
             f"{len(rows)} עדכונים חדשים על הנכסים שאתה עוקב אחריהם:", ""]
    for row in rows:
        when = str(row["created_at"])[:16].replace("T", " ")
        lines.append(f"[{when}] {row['message']}")
        if row["pl_url"]:
            lines.append(f"    {row['pl_url']}")
    lines += ["", "—", "PlanWatch. המידע ממקורות ציבוריים רשמיים;",
              "לבעלות רשומה יש להזמין נסח טאבו רשמי."]
    return subject, "\n".join(lines)


def _send_console(target, subject, body) -> None:
    print("=" * 70)
    print(subject)
    print("-" * 70)
    print(body)


def _send_file(target, subject, body) -> None:
    path = target or "alerts.log"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n{'='*70}\n{subject}\n{'-'*70}\n{body}\n")


def _send_email(target, subject, body) -> None:
    cfg = smtp_config()
    if not cfg["configured"]:
        raise RuntimeError(
            "SMTP not configured. Set PLANWATCH_SMTP_HOST (and USER/PASS/FROM) "
            "to enable email delivery.")
    if not target:
        raise RuntimeError("no email address for this client")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"] or cfg["user"] or "planwatch@localhost"
    msg["To"] = target
    msg.set_content(body)
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
        if cfg["tls"]:
            smtp.starttls()
        password = os.environ.get("PLANWATCH_SMTP_PASS")
        if cfg["user"] and password:
            smtp.login(cfg["user"], password)
        smtp.send_message(msg)


CHANNELS = {"console": _send_console, "file": _send_file, "email": _send_email}


def set_channel(conn, client_id, channel="console", target=None) -> dict:
    import db as _db

    ensure_schema(conn)
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; "
                         f"expected one of {sorted(CHANNELS)}")
    conn.execute(
        """INSERT INTO alert_channels (client_id, channel, target, active, created_at)
           VALUES (?,?,?,1,?)
           ON CONFLICT (client_id) DO UPDATE SET
             channel=excluded.channel, target=excluded.target, active=1""",
        (client_id, channel, target, _db.now_iso()))
    conn.commit()
    return {"client_id": client_id, "channel": channel, "target": target}


def channels(conn) -> list[dict]:
    ensure_schema(conn)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM alert_channels ORDER BY client_id")]


def deliver_pending(conn, client_id=None, dry_run=False, limit=500) -> dict:
    """
    Deliver undelivered notifications, one digest per subscriber.

    Marks `delivered=1` **only after** the channel returns without raising. A
    failed send leaves the rows pending so the next run retries: for alerting,
    a duplicate is a nuisance and a miss is a failure.
    """
    import db as _db

    ensure_schema(conn)
    where, params = ["n.delivered = 0"], []
    if client_id:
        where.append("s.client_id = ?")
        params.append(client_id)
    rows = conn.execute(
        f"""SELECT n.id, n.kind, n.message, n.created_at, s.client_id,
                   p.pl_url
            FROM notifications n
            JOIN subscriptions s ON s.id = n.subscription_id
            LEFT JOIN plans p ON p.object_id = n.object_id
            WHERE {' AND '.join(where)}
            ORDER BY s.client_id, n.created_at LIMIT ?""",
        params + [limit]).fetchall()
    if not rows:
        # Same keys as the normal path. Returning a narrower dict here made
        # callers KeyError on `failures` whenever there was nothing to send.
        return {"delivered": 0, "batches": 0, "pending": 0, "failures": [],
                "dry_run": dry_run}

    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row["client_id"], []).append(row)

    prefs = {c["client_id"]: c for c in channels(conn)}
    out = {"delivered": 0, "batches": 0, "failures": [], "dry_run": dry_run}
    for client, batch in grouped.items():
        pref = prefs.get(client) or {"channel": "console", "target": None}
        channel = pref.get("channel") or "console"
        target = pref.get("target")
        subject, body = _render(client, batch)
        if dry_run:
            out["batches"] += 1
            out.setdefault("preview", []).append(
                {"client_id": client, "channel": channel, "count": len(batch),
                 "subject": subject, "body": body[:600]})
            continue
        ok, error = True, None
        try:
            CHANNELS[channel](target, subject, body)
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:300]
            out["failures"].append({"client_id": client, "error": error})
        conn.execute(
            """INSERT INTO alert_deliveries
                 (client_id, channel, target, count, ok, error, sent_at)
               VALUES (?,?,?,?,?,?,?)""",
            (client, channel, target, len(batch), 1 if ok else 0, error,
             _db.now_iso()))
        if ok:
            marks = ",".join("?" for _ in batch)
            conn.execute(
                f"UPDATE notifications SET delivered=1 WHERE id IN ({marks})",
                [r["id"] for r in batch])
            out["delivered"] += len(batch)
        out["batches"] += 1
    conn.commit()
    out["pending"] = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE delivered=0").fetchone()[0]
    return out


def run_once(conn, deliver=True, dry_run=False) -> dict:
    """Watch every signal, then deliver. This is the scheduled entry point."""
    watched = watch_all(conn, dry_run=dry_run)
    out = {"watch": watched}
    if deliver:
        out["deliver"] = deliver_pending(conn, dry_run=dry_run)
    return out


def status(conn) -> dict:
    ensure_schema(conn)
    pending = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE delivered=0").fetchone()[0]
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, COUNT(*) FROM notifications GROUP BY 1")}
    recent = [dict(r) for r in conn.execute(
        "SELECT * FROM alert_deliveries ORDER BY id DESC LIMIT 10")]
    return {
        "pending": pending,
        "total_notifications": conn.execute(
            "SELECT COUNT(*) FROM notifications").fetchone()[0],
        "by_kind": by_kind,
        "watchers": list(WATCHERS),
        "baseline_rows": conn.execute(
            "SELECT COUNT(*) FROM alert_baseline").fetchone()[0],
        "channels": channels(conn),
        "smtp": smtp_config(),
        "recent_deliveries": recent,
    }


if __name__ == "__main__":
    import json
    import sys

    import db
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    conn = db.get_conn()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "seed":
        print(json.dumps(seed_baseline(conn), ensure_ascii=False))
    elif cmd == "watch":
        print(json.dumps(watch_all(conn, dry_run="--dry" in sys.argv),
                         ensure_ascii=False, indent=1)[:1500])
    elif cmd == "deliver":
        print(json.dumps(deliver_pending(conn, dry_run="--dry" in sys.argv),
                         ensure_ascii=False, indent=1)[:2000])
    elif cmd == "run":
        print(json.dumps(run_once(conn, dry_run="--dry" in sys.argv),
                         ensure_ascii=False, indent=1)[:2000])
    else:
        print(json.dumps(status(conn), ensure_ascii=False, indent=1)[:1800])
    conn.close()
