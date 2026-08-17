"""
PlanWatch - end-user accounts
==============================
Separate from `PLANWATCH_BASIC_AUTH`, which gates the internal admin
dashboard and is known only to the operator. This module backs a second,
narrower system: real username/password accounts the operator hands out to
end users of the public webapp/bots, authenticated with a bearer token
instead of a shared password.

Passwords are hashed with `hashlib.scrypt` (stdlib, no dependency) and a
per-user random salt. Sessions are opaque random tokens (`secrets.token_urlsafe`)
stored in `sessions`, checked by `resolve_session()` on every request that
carries `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta

import db

SESSION_DAYS = 30

#: The four search modes the webapp exposes (webapp/index.html's #modes).
#: A mode absent from a user's stored `permissions` dict is enabled - see
#: the users.permissions column comment in db.py - so every user created
#: before this feature existed keeps full access with no migration needed.
MODES = ("listings", "smart", "renewal", "market")


def _permissions_out(raw: str | None) -> dict:
    stored = {}
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
    return {mode: stored.get(mode, True) for mode in MODES}


def hash_password(password: str) -> tuple[str, str]:
    """Returns (salt_hex, hash_hex)."""
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt.encode("utf-8"),
                            n=2**14, r=8, p=1, dklen=32)
    return salt, digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt_hex.encode("utf-8"),
                            n=2**14, r=8, p=1, dklen=32)
    return secrets.compare_digest(digest.hex(), hash_hex)


def create_user(conn, username: str, password: str, permissions: dict | None = None) -> int:
    salt, pw_hash = hash_password(password)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, password_salt, is_active, "
        "created_at, permissions) VALUES (?, ?, ?, 1, ?, ?)",
        (username, pw_hash, salt, db.now_iso(), json.dumps(permissions or {})),
    )
    conn.commit()
    return cur.lastrowid


def list_users(conn) -> list[dict]:
    # last_login is the most recent session ever issued, not the current one
    # in use - a session outliving SESSION_DAYS still marks when the user
    # last actually logged in, which is what the admin panel wants to show.
    rows = conn.execute(
        """SELECT u.id, u.username, u.is_active, u.created_at, u.permissions,
                  MAX(s.created_at) AS last_login
           FROM users u LEFT JOIN sessions s ON s.user_id = u.id
           GROUP BY u.id ORDER BY u.username"""
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["permissions"] = _permissions_out(item.pop("permissions"))
        out.append(item)
    return out


def set_active(conn, user_id: int, active: bool) -> None:
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
    conn.commit()


def set_permissions(conn, user_id: int, permissions: dict) -> None:
    # Only the four known modes are ever persisted - an unrecognised key
    # from a stale client can't silently accumulate in the column.
    clean = {mode: bool(permissions.get(mode, True)) for mode in MODES}
    conn.execute("UPDATE users SET permissions = ? WHERE id = ?",
                (json.dumps(clean), user_id))
    conn.commit()


def reset_password(conn, user_id: int, password: str) -> None:
    salt, pw_hash = hash_password(password)
    conn.execute(
        "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
        (pw_hash, salt, user_id),
    )
    conn.commit()


def authenticate(conn, username: str, password: str) -> int | None:
    row = conn.execute(
        "SELECT id, password_hash, password_salt, is_active FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if not row or not row["is_active"]:
        return None
    if not verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return row["id"]


def create_session(conn, user_id: int, days: int = SESSION_DAYS) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, db.now_iso(), db.iso_in(timedelta(days=days).total_seconds())),
    )
    conn.commit()
    return token


def resolve_session(conn, token: str) -> int | None:
    row = conn.execute(
        "SELECT user_id, expires_at FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < db.now_iso():
        return None
    return row["user_id"]


def delete_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def get_user(conn, user_id: int) -> dict | None:
    row = conn.execute(
        "SELECT username, permissions FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        return None
    return {"username": row["username"], "permissions": _permissions_out(row["permissions"])}


#: Non-secret operator-editable values - safe to hand back on the PUBLIC
#: /api/public/bot-links route, see the `settings` table comment in db.py.
SETTINGS_KEYS = ("whatsapp_bot_number", "telegram_bot_username")

#: The Telegram bot token IS a real secret (whoever has it can operate the
#: bot as the operator), so it is kept out of SETTINGS_KEYS/get_settings -
#: it must never reach the public /api/public/bot-links route. It still gets
#: a DB-backed home rather than env-var-only, because bots/telegram/bot.js
#: is a long-running process the operator can't easily redeploy just to
#: rotate a token; dashboard.py's /api/admin/settings (Basic-Auth only, same
#: gate as the rest of the admin dashboard) is the sole way to read or write
#: it. PLANWATCH_TELEGRAM_TOKEN still works and takes priority if set - see
#: bots/telegram/bot.js.
SECRET_SETTINGS_KEYS = ("telegram_bot_token",)
ADMIN_SETTINGS_KEYS = SETTINGS_KEYS + SECRET_SETTINGS_KEYS


def get_settings(conn) -> dict:
    """Public-safe values only - what /api/public/bot-links returns."""
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    return {key: stored.get(key, "") for key in SETTINGS_KEYS}


def get_admin_settings(conn) -> dict:
    """Everything the "הגדרות מנהל" tab may show, including the Telegram
    bot token. Only ever call this behind the operator's own Basic Auth."""
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {r["key"]: r["value"] for r in rows}
    return {key: stored.get(key, "") for key in ADMIN_SETTINGS_KEYS}


def set_settings(conn, values: dict) -> None:
    now = db.now_iso()
    for key in ADMIN_SETTINGS_KEYS:
        if key not in values:
            continue
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(values[key] or "").strip(), now),
        )
    conn.commit()
