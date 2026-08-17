#!/usr/bin/env python3
"""Fetch normalized Yad2 listings from the local backend and deliver them."""

from __future__ import annotations

import argparse, csv, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def request_json(url: str, timeout: int, *, data: bytes | None = None, headers=None):
    req = Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:500]}") from exc
    except URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def healthy(api_base: str) -> bool:
    try:
        status, payload = request_json(f"{api_base}/api/health", 3)
        return status == 200 and payload.get("ok") is True
    except (ConnectionError, RuntimeError, TimeoutError):
        return False


def find_root(explicit: str | None) -> Path:
    candidates = [Path(explicit).resolve()] if explicit else [Path.cwd(), *Path(__file__).resolve().parents]
    for root in candidates:
        if (root / "backend" / "src" / "server.js").is_file():
            return root
    raise RuntimeError("Could not locate backend/src/server.js; pass --project-root")


def start_backend(root: Path, api_base: str) -> None:
    env = os.environ.copy()
    port = api_base.rstrip("/").rsplit(":", 1)[-1]
    if port.isdigit(): env["PORT"] = port
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(["node", "src/server.js"], cwd=root / "backend", env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError("Backend exited during startup; run npm install in backend")
        if healthy(api_base): return
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for the Yad2 backend")


def write_output(path: Path, fmt: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    rows = payload.get("listings", [])
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields: writer.writeheader(); writer.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region-slug", required=True); p.add_argument("--deal-type", default="forsale")
    p.add_argument("--area", type=int); p.add_argument("--city", type=int); p.add_argument("--page", type=int, default=1)
    p.add_argument("--all", action="store_true", dest="all_pages"); p.add_argument("--output")
    p.add_argument("--format", choices=("json", "csv")); p.add_argument("--webhook"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--header", action="append", default=[]); p.add_argument("--header-env")
    p.add_argument("--api-base", default=os.getenv("YAD2_API_BASE", "http://127.0.0.1:4000"))
    p.add_argument("--project-root"); p.add_argument("--no-auto-start", action="store_true"); p.add_argument("--timeout", type=int, default=120)
    args = p.parse_args(); api_base = args.api_base.rstrip("/")
    if not healthy(api_base):
        if args.no_auto_start: raise RuntimeError(f"Backend is not reachable at {api_base}")
        start_backend(find_root(args.project_root), api_base)

    filters = {"dealType": args.deal_type, "regionSlug": args.region_slug, "page": args.page}
    if args.area is not None: filters["area"] = args.area
    if args.city is not None: filters["city"] = args.city
    if args.all_pages: filters["all"] = "true"
    _, source = request_json(f"{api_base}/api/listings?{urlencode(filters)}", args.timeout)
    payload = {"source": "yad2", "fetchedAt": datetime.now(timezone.utc).isoformat(), "filters": filters,
               "totalCount": source.get("totalCount", len(source.get("listings", []))), "listings": source.get("listings", [])}
    if args.output:
        output = Path(args.output).resolve(); fmt = args.format or ("csv" if output.suffix.lower() == ".csv" else "json")
        write_output(output, fmt, payload)
        print(json.dumps({"ok": True, "count": payload["totalCount"], "output": str(output)}, ensure_ascii=False))
    elif not args.webhook: print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.webhook:
        if args.dry_run:
            print(json.dumps({"ok": True, "dryRun": True, "count": payload["totalCount"], "webhook": args.webhook})); return 0
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        for raw in args.header:
            if ":" not in raw: raise ValueError("--header must use 'Name: value'")
            name, value = raw.split(":", 1); headers[name.strip()] = value.strip()
        token_name = args.header_env or "YAD2_WEBHOOK_TOKEN"; token = os.getenv(token_name)
        if args.header_env and not token: raise RuntimeError(f"Environment variable {token_name} is not set")
        if token: headers["Authorization"] = f"Bearer {token}"
        status, response = request_json(args.webhook, args.timeout, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
        print(json.dumps({"ok": True, "count": payload["totalCount"], "webhookStatus": status, "response": response}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (ConnectionError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr); raise SystemExit(1)
