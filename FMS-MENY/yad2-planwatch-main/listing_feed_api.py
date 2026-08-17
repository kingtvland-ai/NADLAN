"""Signed inbound API for authorised CRM/MLS property feeds.

This is a lawful alternative to scraping marketplaces: a broker or licensed
feed partner pushes listings it is entitled to share.  The service defaults to
localhost and validates an HMAC-SHA256 signature before parsing JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import market_client


MAX_BODY_BYTES = 5 * 1024 * 1024


def signature_for(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body,
                                 hashlib.sha256).hexdigest()


def valid_signature(body: bytes, supplied: str | None, secret: str) -> bool:
    return bool(supplied) and hmac.compare_digest(supplied, signature_for(body, secret))


class Handler(BaseHTTPRequestHandler):
    secret = ""
    db_path = None

    def log_message(self, fmt, *args):
        # Do not log request paths or bodies: partner payloads may contain
        # addresses and contact information.
        return

    def _send(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send({"ok": True})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/v1/listings":
            return self._send({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send({"error": "invalid content length"}, 400)
        if not 0 < length <= MAX_BODY_BYTES:
            return self._send({"error": "payload must be 1..5MB"}, 413)
        body = self.rfile.read(length)
        if not valid_signature(body, self.headers.get("X-PlanWatch-Signature"),
                               self.secret):
            return self._send({"error": "invalid signature"}, 401)
        try:
            payload = json.loads(body)
            listings = payload["listings"]
            if not isinstance(listings, list):
                raise ValueError("listings must be an array")
            source = str(payload.get("source") or "partner-feed").strip()
            if not source:
                raise ValueError("source is required")
            conn = db.get_conn(self.db_path)
            try:
                imported = market_client.import_listings_records(conn, listings, source)
            finally:
                conn.close()
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return self._send({"error": str(exc)}, 400)
        self._send({"imported": imported, "source": source})


def main() -> int:
    parser = argparse.ArgumentParser(description="Signed authorised-listings feed API")
    parser.add_argument("--port", type=int, default=8014)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    secret = os.environ.get("PLANWATCH_LISTING_FEED_SECRET")
    if not secret:
        raise SystemExit("PLANWATCH_LISTING_FEED_SECRET must be set")
    Handler.secret, Handler.db_path = secret, args.db
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Listing feed API -> http://{args.host}:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
