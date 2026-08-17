"""Local-only search for the separately stored authorised people dataset.

This is intentionally a CLI, not a dashboard endpoint: the main dashboard has
no login system and must never become a public personal-data search service.
Routine results contain only the minimum identifying context and a masked
phone.  Reading the full imported payload requires an operator token.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
from pathlib import Path
from ingestion.feeds.legacy_people_importer import _hash_identifier, decrypt_identifier


DEFAULT_DB = Path("data/private_people.sqlite3")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    # The authorised importer may hold a short DELETE-journal write lock while
    # it encrypts IDs in batches.  Wait briefly rather than failing a local
    # operator search with "database is locked" immediately.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _normal(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def search(db_path: Path, query: str, *, city: str = "", limit: int = 25) -> list[dict]:
    """Return minimal, masked result cards for a name/address search."""
    query = _normal(query)
    city = _normal(city)
    if len(query) < 2:
        raise ValueError("query must contain at least two characters")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    terms = query.split()
    clauses, params = [], []
    for term in terms:
        pattern = f"%{term}%"
        clauses.append("(family_name LIKE ? OR given_name LIKE ? OR street LIKE ?)")
        params.extend((pattern, pattern, pattern))
    if city:
        clauses.append("city LIKE ?")
        params.append(f"%{city}%")
    conn = _connect_readonly(Path(db_path))
    try:
        rows = conn.execute(
            f"""SELECT source_record, family_name, given_name, city, street,
                       house_number, phone_mask, updated_at_source
                FROM people WHERE {' AND '.join(clauses)}
                ORDER BY family_name, given_name LIMIT ?""", params + [limit]
        ).fetchall()
        keys = ("source_record", "family_name", "given_name", "city", "street",
                "house_number", "phone_mask", "updated_at_source")
        return [dict(zip(keys, row)) for row in rows]
    finally:
        conn.close()


def search_identifier(db_path: Path, identifier: str) -> list[dict]:
    """Exact, local lookup by ID without returning or logging the ID itself."""
    identifier = "".join((identifier or "").split())
    if not identifier.isdigit() or not 5 <= len(identifier) <= 12:
        raise ValueError("identifier must contain 5..12 digits")
    conn = _connect_readonly(Path(db_path))
    try:
        row = conn.execute(
            "SELECT value_encrypted FROM private_secrets WHERE name='id-hash-salt'"
        ).fetchone()
        salt = decrypt_identifier(row[0]) if row else None
        if not salt:
            raise RuntimeError("private ID lookup is not available for this database")
        found = conn.execute(
            """SELECT source_record, family_name, given_name, city, street,
                      house_number, phone_mask, updated_at_source
                 FROM people WHERE id_hash=?""",
            (_hash_identifier(identifier, salt),)).fetchall()
        keys = ("source_record", "family_name", "given_name", "city", "street",
                "house_number", "phone_mask", "updated_at_source")
        return [dict(zip(keys, result)) for result in found]
    finally:
        conn.close()


def details(db_path: Path, source_record: int, reveal_token: str) -> dict:
    """Read a full stored record only when the configured operator token matches."""
    expected = os.environ.get("PLANWATCH_PRIVATE_REVEAL_TOKEN")
    if not expected or not hmac.compare_digest(reveal_token, expected):
        raise PermissionError("valid private reveal token required")
    conn = _connect_readonly(Path(db_path))
    try:
        row = conn.execute("SELECT payload_json, id_encrypted FROM people WHERE source_record=?",
                           (source_record,)).fetchone()
        if row is None:
            raise LookupError("record not found")
        payload = json.loads(row[0])
        identifier = decrypt_identifier(row[1])
        if identifier:
            payload["תז"] = identifier
        return payload
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local search of authorised private data.")
    parser.add_argument("query", nargs="?", help="name, street, or other indexed term")
    parser.add_argument("--city", default="")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--record", type=int, help="source record for a detail reveal")
    parser.add_argument("--id", dest="identifier", default="",
                        help="exact ID lookup (prefer the authenticated POST API to avoid shell history)")
    parser.add_argument("--reveal-token", default="")
    args = parser.parse_args()
    if args.record is not None:
        result = details(args.db, args.record, args.reveal_token)
    elif args.identifier:
        result = search_identifier(args.db, args.identifier)
    elif args.query:
        result = search(args.db, args.query, city=args.city, limit=args.limit)
    else:
        parser.error("provide a query or --record")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
