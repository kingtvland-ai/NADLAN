#!/usr/bin/env python3
"""Quick audit of NADLANFIX codebase state."""

import sys
sys.path.insert(0, '.')

# Test 1: Core imports
print("=" * 60)
print("TEST 1: Core imports")
print("=" * 60)

modules = [
    "db",
    "accounts", 
    "dashboard",
    "jobs.scheduler",
    "jobs.daily_ingest",
    "app.crm.crm_backend",
    "storage.firestore_sync",
    "sources.base.adapter",
]

for module_name in modules:
    try:
        __import__(module_name)
        print(f"✓ {module_name}")
    except ImportError as e:
        print(f"✗ {module_name}: {e}")
    except Exception as e:
        print(f"! {module_name}: {type(e).__name__}: {str(e)[:60]}")

# Test 2: CRM Backend instantiation
print("\n" + "=" * 60)
print("TEST 2: CRM Backend instantiation")
print("=" * 60)

try:
    from pathlib import Path
    from app.crm.crm_backend import CRMBackend
    db_path = Path("data/test_crm.sqlite3")
    crm = CRMBackend(db_path)
    print("✓ CRMBackend.__init__() succeeded - tables created")
except Exception as e:
    print(f"✗ CRMBackend: {type(e).__name__}: {str(e)[:80]}")

# Test 3: Check actual tables exist
print("\n" + "=" * 60)
print("TEST 3: Check CRM tables exist")
print("=" * 60)

try:
    import sqlite3
    conn = sqlite3.connect("data/test_crm.sqlite3")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name LIKE 'crm_%'
    """)
    tables = cursor.fetchall()
    if tables:
        print(f"✓ Found {len(tables)} CRM tables:")
        for (table_name,) in tables:
            print(f"  - {table_name}")
    else:
        print("✗ No CRM tables found")
    conn.close()
except Exception as e:
    print(f"✗ Table check: {type(e).__name__}: {str(e)[:80]}")

# Test 4: Check if code has external dependencies
print("\n" + "=" * 60)
print("TEST 4: Check requirements.txt")
print("=" * 60)

try:
    with open("requirements.txt", "r") as f:
        reqs = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        print(f"Total dependencies: {len(reqs)}")
        # Check for suspicious ones
        suspicious = [r for r in reqs if any(x in r.lower() for x in ["facebook", "selenium", "playwright"])]
        if suspicious:
            print(f"External scrapers found: {suspicious}")
        else:
            print("✓ No obvious external scraper dependencies")
except Exception as e:
    print(f"! {type(e).__name__}: {str(e)[:80]}")

print("\n" + "=" * 60)
print("AUDIT COMPLETE")
print("=" * 60)
