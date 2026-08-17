"""Inspect madlan scrapers - navigate with os.listdir to avoid encoding issues."""
import os, sys, json, glob

c = glob.glob(r'F:\AI-STUDIO-BUILDER-APP\PROJECT-CITY\*-API')[0]
c2a = os.path.join(c, 'curl2api')
scrapers_dir = os.path.join(c2a, 'scrapers')

out = open('inspect_output.txt', 'w', encoding='utf-8')

def log(*args):
    print(*args, file=out)
    out.flush()

log(f"Scrapers dir: {scrapers_dir}")
files = sorted(f for f in os.listdir(scrapers_dir) if f.endswith('.json'))
log(f"Scrapers ({len(files)}):")
for f in files:
    log(f"  {f}")

log("\n=== madlan_listings.json ===")
ml_path = os.path.join(scrapers_dir, 'madlan_listings.json')
if os.path.exists(ml_path):
    with open(ml_path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
        log(json.dumps(data, ensure_ascii=False, indent=2))

log("\n=== madlan_with_cookies.json ===")
mc_path = os.path.join(scrapers_dir, 'madlan_with_cookies.json')
if os.path.exists(mc_path):
    with open(mc_path, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
        log(json.dumps(data, ensure_ascii=False, indent=2))

out.close()
print("done")
