"""Production hardening utilities for NADLANFIX.

Provides:
- Rate limiting
- Request signing
- Database connection pooling
- Data retention policies
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


class RateLimiter:
    """Simple in-memory rate limiter."""

    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        """Check if a request is allowed.

        Args:
            key: Rate limit key (e.g., IP address, user ID)

        Returns:
            True if request is allowed
        """
        now = time.time()
        if key not in self._requests:
            self._requests[key] = []

        self._requests[key] = [t for t in self._requests[key] if now - t < self.window_seconds]

        if len(self._requests[key]) >= self.max_requests:
            return False

        self._requests[key].append(now)
        return True

    def get_remaining(self, key: str) -> int:
        """Get remaining requests for a key."""
        now = time.time()
        if key not in self._requests:
            return self.max_requests

        self._requests[key] = [t for t in self._requests[key] if now - t < self.window_seconds]
        return max(0, self.max_requests - len(self._requests[key]))


class RequestSigner:
    """HMAC request signing for API authentication."""

    def __init__(self, secret: str):
        self.secret = secret.encode() if isinstance(secret, str) else secret

    def sign(self, method: str, path: str, timestamp: str, body: str = "") -> str:
        """Sign a request.

        Args:
            method: HTTP method
            path: Request path
            timestamp: ISO timestamp
            body: Request body

        Returns:
            HMAC signature hex string
        """
        message = f"{method}\n{path}\n{timestamp}\n{body}"
        return hmac.new(self.secret, message.encode(), hashlib.sha256).hexdigest()

    def verify(self, method: str, path: str, timestamp: str, signature: str, body: str = "") -> bool:
        """Verify a request signature.

        Args:
            method: HTTP method
            path: Request path
            timestamp: ISO timestamp
            signature: HMAC signature to verify
            body: Request body

        Returns:
            True if signature is valid
        """
        expected = self.sign(method, path, timestamp, body)
        return hmac.compare_digest(expected, signature)


class ConnectionPool:
    """Simple SQLite connection pool."""

    def __init__(self, db_path: Path | str, max_connections: int = 10):
        self.db_path = Path(db_path)
        self.max_connections = max_connections
        self._pool: list[sqlite3.Connection] = []
        self._in_use: int = 0

    def get_connection(self) -> sqlite3.Connection:
        """Get a connection from the pool."""
        if self._pool:
            conn = self._pool.pop()
            try:
                conn.execute("SELECT 1")
                return conn
            except Exception:
                pass

        if self._in_use >= self.max_connections:
            raise RuntimeError(f"Connection pool exhausted ({self.max_connections} max)")

        self._in_use += 1
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def release_connection(self, conn: sqlite3.Connection) -> None:
        """Release a connection back to the pool."""
        try:
            conn.rollback()
            if len(self._pool) < self.max_connections:
                self._pool.append(conn)
            else:
                conn.close()
            self._in_use = max(0, self._in_use - 1)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            self._in_use = max(0, self._in_use - 1)

    def close_all(self) -> None:
        """Close all connections in the pool."""
        for conn in self._pool:
            try:
                conn.close()
            except Exception:
                pass
        self._pool.clear()
        self._in_use = 0


class RetentionPolicy:
    """Data retention policy enforcement."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)

    def enforce_retention(self, table: str, date_column: str, retention_days: int) -> int:
        """Delete rows older than retention period.

        Args:
            table: Table name
            date_column: Date column name
            retention_days: Number of days to retain

        Returns:
            Number of rows deleted
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {date_column} < ?",
                (cutoff.isoformat(timespec="seconds"),)
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def get_table_size(self, table: str) -> int:
        """Get row count for a table."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def get_oldest_record(self, table: str, date_column: str) -> str | None:
        """Get the oldest record date for a table."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                f"SELECT MIN({date_column}) as oldest FROM {table}"
            ).fetchone()
            return row["oldest"] if row else None
        finally:
            conn.close()
