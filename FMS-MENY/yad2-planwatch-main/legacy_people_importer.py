"""Streaming importer for the legacy fixed-record CVF people database.

The original application stores one CP1255 CSV-like row in each fixed-size
record.  ``3.MAP`` declares the record size (403 in the supplied export).
This importer never executes the accompanying legacy executable and reads the
source sequentially, so even multi-gigabyte exports do not need to fit RAM.

The target database is deliberately separate from PlanWatch's planning data.
It contains personal data and must stay on a protected local volume; it is not
served by the dashboard.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ENCODING = "cp1255"
DEFAULT_RECORD_SIZE = 403
DEFAULT_DB = Path("data/private_people.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_imported INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS people (
    source_record INTEGER PRIMARY KEY,
    id_hash TEXT,
    id_encrypted BLOB,
    family_name TEXT,
    given_name TEXT,
    city TEXT,
    street TEXT,
    house_number TEXT,
    phone_mask TEXT,
    updated_at_source TEXT,
    payload_json TEXT NOT NULL
);

"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_people_name ON people (family_name, given_name);
CREATE INDEX IF NOT EXISTS idx_people_city ON people (city);
CREATE INDEX IF NOT EXISTS idx_people_id_hash ON people (id_hash);
"""

SECRETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS private_secrets (
    name TEXT PRIMARY KEY,
    value_encrypted BLOB NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_size_from_map(map_path: Path) -> int:
    """Read and validate ``LineSize=N`` from the legacy MAP file."""
    for line in map_path.read_text(encoding="ascii", errors="strict").splitlines():
        key, sep, value = line.partition("=")
        if key.strip().lower() == "linesize" and sep:
            size = int(value.strip())
            if 32 <= size <= 65_536:
                return size
    raise ValueError(f"LineSize was not found in {map_path}")


def _parse_csv_record(raw: bytes) -> list[str]:
    """Decode one padded fixed record and parse its comma-separated cells."""
    text = raw.decode(ENCODING, errors="replace").rstrip("\0\r\n ")
    if not text:
        return []
    return next(csv.reader([text], strict=False))


def read_header(source: Path, record_size: int) -> list[str]:
    with source.open("rb") as stream:
        header = _parse_csv_record(stream.read(record_size))
    if not header or "תז" not in header or "משפחה" not in header:
        raise ValueError("CVF header is not recognised; refusing to import")
    return header


def iter_rows(source: Path, record_size: int):
    """Yield ``(record_number, row)`` from a CVF file after its header."""
    headers = read_header(source, record_size)
    with source.open("rb") as stream:
        stream.seek(record_size)
        number = 1
        while raw := stream.read(record_size):
            if len(raw) != record_size:
                raise ValueError(
                    f"truncated record {number}: {len(raw)} of {record_size} bytes")
            values = _parse_csv_record(raw)
            if values:
                # The source occasionally has trailing blank cells.  Preserve
                # the declared schema and pad missing cells deterministically.
                values = (values + [""] * len(headers))[:len(headers)]
                yield number, dict(zip(headers, values))
            number += 1


def _normal(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split())


def _hash_identifier(value: str, salt: str) -> str | None:
    value = "".join(value.split())
    if not value:
        return None
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def _mask_phone(value: str | None) -> str | None:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return None
    return "*" * max(0, len(digits) - 4) + digits[-4:]


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_protect(value: bytes) -> bytes:
    """Encrypt bytes for the current Windows user with DPAPI."""
    if os.name != "nt":
        raise RuntimeError("private identifier encryption requires Windows DPAPI")
    source = ctypes.create_string_buffer(value)
    in_blob = _DataBlob(len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), None, None, None, None,
                                    0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def decrypt_identifier(value: bytes | None) -> str | None:
    """Decrypt a DPAPI-protected identifier for an authorised detail reveal."""
    if not value:
        return None
    source = ctypes.create_string_buffer(value)
    in_blob = _DataBlob(len(value), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None,
                                      0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _private_salt(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value_encrypted FROM private_secrets WHERE name='id-hash-salt'").fetchone()
    if row:
        return decrypt_identifier(row[0]) or ""
    salt = os.urandom(32).hex()
    conn.execute("INSERT INTO private_secrets (name, value_encrypted) VALUES (?,?)",
                 ("id-hash-salt", _dpapi_protect(salt.encode("utf-8"))))
    return salt


def _row_values(record_number: int, row: dict[str, str], salt: str,
                retain_identifiers: bool) -> tuple:
    # Keep every source field in the private payload, while indexing only the
    # fields necessary for a narrow local search.  An ID is hashed for lookup;
    # it is removed from the payload so routine database reads cannot reveal it.
    payload = {key: value for key, value in row.items() if key != "תז"}
    return (
        record_number,
        _hash_identifier(row.get("תז", ""), salt),
        _dpapi_protect(row["תז"].encode("utf-8")) if retain_identifiers and row.get("תז") else None,
        _normal(row.get("משפחה")), _normal(row.get("פרטי")),
        _normal(row.get("ישוב")), _normal(row.get("רחוב")),
        _normal(row.get("מספר")), _mask_phone(row.get("טלפון")),
        _normal(row.get("עידכון אחרון")),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def import_cvf(source: Path, db_path: Path = DEFAULT_DB, *, map_path: Path | None = None,
               batch_size: int = 2_000, progress=None, dry_run: bool = False,
               retain_identifiers: bool = False, identifiers_only: bool = False) -> dict:
    """Import a CVF export into a separate local SQLite database."""
    if identifiers_only and not retain_identifiers:
        raise ValueError("identifiers_only requires retain_identifiers")
    source = Path(source)
    map_path = Path(map_path) if map_path else source.with_suffix(".MAP")
    record_size = record_size_from_map(map_path) if map_path.exists() else DEFAULT_RECORD_SIZE
    headers = read_header(source, record_size)
    if dry_run:
        sample = []
        for number, row in iter_rows(source, record_size):
            sample.append({"record": number, "fields": sorted(row)})
            if len(sample) == 3:
                break
        return {"dry_run": True, "record_size": record_size, "headers": headers,
                "sample_records": len(sample), "source_bytes": source.stat().st_size}

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # A source file this large is an append-heavy bulk load. Secondary indexes
    # multiply every inserted row's work, so rebuild them only after the final
    # commit.  Do not use WAL here: an 8M-row resume updates millions of pages
    # and leaves a second, multi-gigabyte WAL beside the database until the end.
    # DELETE journaling keeps only the current 2,000-row transaction journal;
    # each committed batch remains durable and the importer is the sole writer.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.executescript(SECRETS_SCHEMA)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    if "id_encrypted" not in columns:
        conn.execute("ALTER TABLE people ADD COLUMN id_encrypted BLOB")
    conn.executescript("""
        DROP INDEX IF EXISTS idx_people_name;
        DROP INDEX IF EXISTS idx_people_city;
        DROP INDEX IF EXISTS idx_people_id_hash;
    """)
    # The random salt is DPAPI-encrypted with the database, so identity lookup
    # remains exact without placing a reusable secret in source control or env.
    salt = _private_salt(conn)
    run_id = conn.execute(
        "INSERT INTO import_runs (source_path, started_at) VALUES (?,?)",
        (str(source.resolve()), now_iso()),
    ).lastrowid
    sql = """
        INSERT INTO people (source_record,id_hash,id_encrypted,family_name,given_name,
                            city,street,house_number,phone_mask,updated_at_source,payload_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source_record) DO UPDATE SET
          id_hash=excluded.id_hash,
          id_encrypted=COALESCE(excluded.id_encrypted, people.id_encrypted),
          family_name=excluded.family_name,
          given_name=excluded.given_name, city=excluded.city, street=excluded.street,
          house_number=excluded.house_number, phone_mask=excluded.phone_mask,
          updated_at_source=excluded.updated_at_source, payload_json=excluded.payload_json
    """
    identifiers_sql = """
        UPDATE people SET id_hash=?, id_encrypted=?
        WHERE source_record=? AND id_encrypted IS NULL
    """
    seen = imported = 0
    batch = []
    try:
        for number, row in iter_rows(source, record_size):
            seen += 1
            if identifiers_only:
                identifier = row.get("תז", "")
                if identifier:
                    batch.append((_hash_identifier(identifier, salt),
                                  _dpapi_protect(identifier.encode("utf-8")), number))
            else:
                batch.append(_row_values(number, row, salt, retain_identifiers))
            if len(batch) >= batch_size:
                before = conn.total_changes
                conn.executemany(identifiers_sql if identifiers_only else sql, batch)
                imported += conn.total_changes - before
                batch.clear()
                # Persist progress with every committed batch so a long import
                # is observable without reading personal rows.
                conn.execute("""UPDATE import_runs SET records_seen=?,
                             records_imported=? WHERE id=?""",
                             (seen, imported, run_id))
                conn.commit()
                if progress:
                    progress(seen, source.stat().st_size)
        if batch:
            before = conn.total_changes
            conn.executemany(identifiers_sql if identifiers_only else sql, batch)
            imported += conn.total_changes - before
        conn.execute("""UPDATE import_runs SET finished_at=?, records_seen=?,
                         records_imported=?, ok=1 WHERE id=?""",
                     (now_iso(), seen, imported, run_id))
        conn.commit()
        conn.executescript(INDEXES)
        conn.commit()
        return {"record_size": record_size, "headers": len(headers), "seen": seen,
                "imported": imported, "database": str(db_path),
                "identifiers_retained": retain_identifiers,
                "identifiers_only": identifiers_only}
    except Exception as exc:
        conn.execute("""UPDATE import_runs SET finished_at=?, records_seen=?,
                         records_imported=?, error=? WHERE id=?""",
                     (now_iso(), seen, imported, repr(exc), run_id))
        conn.commit()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a legacy CVF export safely.")
    parser.add_argument("source", type=Path, help="path to 3.CVF")
    parser.add_argument("--map", dest="map_path", type=Path)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retain-identifiers", action="store_true",
                        help="DPAPI-encrypt and retain ID numbers in the private DB")
    parser.add_argument("--identifiers-only", action="store_true",
                        help="only fill missing encrypted IDs; preserves existing people rows")
    args = parser.parse_args()

    def progress(rows, _bytes):
        print(f"Imported {rows:,} records", end="\r", flush=True)

    result = import_cvf(args.source, args.db, map_path=args.map_path,
                        progress=progress, dry_run=args.dry_run,
                        retain_identifiers=args.retain_identifiers,
                        identifiers_only=args.identifiers_only)
    print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
