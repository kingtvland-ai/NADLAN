"""Authenticated localhost API for the authorised private people database.

It intentionally runs separately from ``dashboard.py`` because the dashboard
has no authentication.  The server binds only to loopback and refuses to start
without ``PLANWATCH_PRIVATE_API_TOKEN``.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import private_people_search as people


class Handler(BaseHTTPRequestHandler):
    token = ""
    db_path = Path("data/private_people.sqlite3")

    def log_message(self, fmt, *args):
        # Never write request URLs (which may contain names) to a console log.
        return

    def _authorised(self) -> bool:
        value = self.headers.get("Authorization", "")
        return value.startswith("Bearer ") and hmac.compare_digest(value[7:], self.token)

    def _send(self, body: dict, status: int = 200):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self._authorised():
            return self._send({"error": "unauthorised"}, 401)
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/health":
                return self._send({"ok": True})
            if parsed.path == "/search":
                return self._send({"rows": people.search(
                    self.db_path, query.get("q", ""), city=query.get("city", ""),
                    limit=int(query.get("limit", "25")))})
            if parsed.path == "/record":
                # A separate reveal token is required for decrypted fields.
                return self._send(people.details(
                    self.db_path, int(query["id"]), self.headers.get("X-Reveal-Token", "")))
            self._send({"error": "not found"}, 404)
        except (ValueError, KeyError, RuntimeError) as exc:
            self._send({"error": str(exc)}, 400)
        except PermissionError:
            self._send({"error": "reveal token required"}, 403)
        except LookupError:
            self._send({"error": "not found"}, 404)
        except sqlite3.Error:
            self._send({"error": "private database is temporarily unavailable"}, 503)

    def do_POST(self):
        """Sensitive identifier searches use a body, never a URL query string."""
        if not self._authorised():
            return self._send({"error": "unauthorised"}, 401)
        if self.path != "/search-by-id":
            return self._send({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("request body must be 1..1024 bytes")
            payload = json.loads(self.rfile.read(length))
            identifier = payload.get("id") if isinstance(payload, dict) else None
            return self._send({"rows": people.search_identifier(self.db_path, identifier)})
        except (ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
            self._send({"error": str(exc)}, 400)
        except sqlite3.Error:
            self._send({"error": "private database is temporarily unavailable"}, 503)


def main() -> int:
    parser = argparse.ArgumentParser(description="Private-data localhost API")
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--db", type=Path, default=Path("data/private_people.sqlite3"))
    args = parser.parse_args()
    token = os.environ.get("PLANWATCH_PRIVATE_API_TOKEN")
    if not token:
        raise SystemExit("PLANWATCH_PRIVATE_API_TOKEN must be set")
    Handler.token, Handler.db_path = token, args.db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Private API on http://127.0.0.1:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
