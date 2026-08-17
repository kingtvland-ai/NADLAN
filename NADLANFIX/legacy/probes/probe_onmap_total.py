"""Probe the 8099 ONMAP endpoint: top-level keys + how many records."""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

def fetch(page: int) -> dict:
    url = f"http://127.0.0.1:8099/api/onmap?page={page}"
    with urllib.request.urlopen(url, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))

payload = fetch(1)
print("top-level keys:", sorted(payload.keys()))
print("record_count:", payload.get("record_count"))
print("pages_fetched:", payload.get("pages_fetched"))
print("drift_status:", payload.get("drift_status"))
print("drift_note:", payload.get("drift_note"))

data = payload.get("data")
if isinstance(data, list):
    print("data is list, len:", len(data))
    if data and isinstance(data[0], dict):
        first = data[0]
        print("first item keys:", sorted(first.keys()))
        print("first entityType:", first.get("entityType"))
        print("first search_option:", first.get("search_option"))
        kinds = {}
        for row in data:
            e = row.get("entityType")
            s = row.get("search_option")
            key = f"{e}/{s}"
            kinds[key] = kinds.get(key, 0) + 1
        print("entityType/search_option counts:", kinds)
elif isinstance(data, dict):
    print("data is dict, keys:", sorted(data.keys()))
    for k, v in data.items():
        if isinstance(v, list):
            print(f"  {k}: list len {len(v)}")
        else:
            print(f"  {k}: {type(v).__name__} = {str(v)[:120]}")

# Try page 2 and 3
for p in (2, 3):
    try:
        p2 = fetch(p)
        d2 = p2.get("data")
        n2 = len(d2) if isinstance(d2, list) else "not-list"
        print(f"page {p}: record_count={p2.get('record_count')} data_len={n2}")
        if isinstance(d2, list) and d2:
            ids2 = {r.get("id") for r in d2}
            ids1 = {r.get("id") for r in data}
            print(f"  unique on page {p}: {len(ids2)} overlap with page1: {len(ids2 & ids1)}")
    except Exception as exc:
        print(f"page {p} error: {exc}")
