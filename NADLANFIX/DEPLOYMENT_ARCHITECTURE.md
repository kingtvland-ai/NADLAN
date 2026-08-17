# Deployment Architecture - NADLANFIX 2026
## Complete Data Flow + Implementation Details

**עדכון אחרון:** 2026-08-17  
**Version:** 1.0 - Production Ready

---

## 📊 System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      YOUR LOCAL MACHINE                         │
│                      (Windows, Daily 00:30)                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Yad2       │  │  Facebook    │  │   ONMAP      │          │
│  │  Scraper     │  │  Scraper     │  │  Scraper     │          │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
│         │                 │                 │                  │
│         └─────────────────┼─────────────────┘                  │
│                           ↓                                    │
│                   ┌───────────────┐                            │
│                   │  Dedup Pass   │ (Remove duplicates)        │
│                   └───────┬───────┘                            │
│                           ↓                                    │
│                   ┌───────────────┐                            │
│                   │  Normalize    │ (Canonical format)         │
│                   └───────┬───────┘                            │
│                           ↓                                    │
│                   ┌───────────────┐                            │
│                   │  Validate     │ (Quality checks)           │
│                   └───────┬───────┘                            │
│                           ↓                                    │
│                 ┌─────────────────────┐                       │
│                 │  Local SQLite DB    │                       │
│                 │  Full Data + Stats  │                       │
│                 └────────┬────────────┘                       │
│                          │                                    │
│                          └────→ Backup Files                  │
│                                 (Daily)                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
              │
              │ Encrypted Sync (gzip + fernet)
              │ HTTPS POST /api/admin/restore-backup
              ↓
┌─────────────────────────────────────────────────────────────────┐
│                      RENDER.COM                                  │
│                    (Production Server)                          │
│                      $12/month                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌───────────────────────────────────────────────┐            │
│  │  Python API Server (Port 8000)                │            │
│  ├───────────────────────────────────────────────┤            │
│  │ GET  /api/health                              │            │
│  │ GET  /api/local-listings       (cached)       │            │
│  │ GET  /api/stats                (aggregated)   │            │
│  │ POST /api/admin/restore-backup (receive sync) │            │
│  └────────────────┬────────────────────────────┘            │
│                   │                                          │
│  ┌────────────────↓─────────────────┐                       │
│  │  Synced SQLite Database          │                       │
│  │  /data/planwatch.sqlite3         │                       │
│  │  (Read-only for dashboard)       │                       │
│  └──────────────────────────────────┘                       │
│                                                                 │
│  ┌──────────────────────────────────┐                        │
│  │  Persistent Disk (5GB)           │                        │
│  │  - Current DB                    │                        │
│  │  - Backup history                │                        │
│  │  - Logs                          │                        │
│  └──────────────────────────────────┘                        │
│                                                                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           │ HTTP/HTTPS
                           ↓
┌─────────────────────────────────────────────────────────────────┐
│                      CLOUDFLARE                                  │
│                    (Edge Network)                               │
│                       FREE TIER                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ✓ DNS Management        ✓ DDoS Protection                     │
│  ✓ Page Caching          ✓ Rate Limiting                       │
│  ✓ HTTPS Everywhere      ✓ Bot Management                      │
│                                                                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           │ Cached Response
                           ↓
                    ┌───────────────┐
                    │  User Browser │
                    └───────────────┘
```

---

## 1️⃣ Local Data Collection Layer

### Startup on Your Machine

**When:** Every day at 00:30 UTC (2:30 AM Israel time)

**Setup on Windows:**

```powershell
# Create Windows Task Scheduler entry
# Task: "NADLANFIX Daily Harvest"
# Trigger: Daily at 00:30 UTC
# Action: python -m jobs.daily_harvest_all

# Or use a scheduled PowerShell script:
$trigger = New-ScheduledTaskTrigger -Daily -At 02:30
$action = New-ScheduledTaskAction -Execute "python" -Argument "-m jobs.daily_harvest_all"
$principal = New-ScheduledTaskPrincipal -RunLevel Highest
Register-ScheduledTask -TaskName "NADLANFIX Daily" -Trigger $trigger -Action $action
```

**Or on Linux/Mac:**

```bash
# Add to crontab
0 0 * * * cd /path/to/NADLANFIX && python -m jobs.daily_harvest_all
```

### Execution Flow

```python
# jobs/daily_harvest_all.py
import logging
import time
from datetime import datetime
from sources.yad2.scraper import Yad2Scraper
from sources.facebook.scraper import FacebookScraper
from sources.onmap.scraper import ONMAPScraper
from sources.madlan.scraper import MadlanScraper
from pipeline.deduplication import DeduplicationEngine
from pipeline.normalization import NormalizationEngine

logger = logging.getLogger(__name__)

def run_daily_harvest():
    """Execute all sources, dedup, normalize, persist"""
    
    start_time = datetime.now()
    logger.info(f"Starting daily harvest at {start_time}")
    
    # Step 1: Initialize database
    conn = sqlite3.connect('data/planwatch.sqlite3')
    ensure_schema(conn)
    
    # Step 2: Run all scrapers
    all_listings = []
    scrapers = [
        Yad2Scraper(),
        FacebookScraper(),
        ONMAPScraper(),
        MadlanScraper(),
    ]
    
    for scraper in scrapers:
        try:
            logger.info(f"Starting {scraper.name}...")
            listings = scraper.harvest()
            logger.info(f"{scraper.name}: {len(listings)} listings")
            all_listings.extend(listings)
        except Exception as e:
            logger.error(f"{scraper.name} failed: {e}", exc_info=True)
    
    logger.info(f"Total listings from all sources: {len(all_listings)}")
    
    # Step 3: Deduplication
    dedup_engine = DeduplicationEngine(conn)
    deduplicated = dedup_engine.dedup(all_listings)
    logger.info(f"After dedup: {len(deduplicated)} unique listings")
    
    # Step 4: Normalization
    norm_engine = NormalizationEngine()
    normalized = [norm_engine.normalize(l) for l in deduplicated]
    logger.info(f"Normalized to canonical format")
    
    # Step 5: Validation
    validated = []
    for listing in normalized:
        errors = validate_listing(listing)
        if not errors:
            validated.append(listing)
        else:
            logger.warning(f"Invalid listing: {listing['external_id']} - {errors}")
    
    logger.info(f"After validation: {len(validated)} valid listings")
    
    # Step 6: Persist to database
    inserted = 0
    updated = 0
    for listing in validated:
        result = persist_listing(conn, listing)
        if result == 'inserted':
            inserted += 1
        elif result == 'updated':
            updated += 1
    
    conn.commit()
    conn.close()
    
    logger.info(f"Database: {inserted} new, {updated} updated")
    
    # Step 7: Sync to Render
    try:
        sync_to_render()
        logger.info("Synced to Render successfully")
    except Exception as e:
        logger.error(f"Sync failed: {e}")
    
    elapsed = datetime.now() - start_time
    logger.info(f"Daily harvest completed in {elapsed}")
    
    return {
        "total_sources": len(all_listings),
        "after_dedup": len(deduplicated),
        "after_validation": len(validated),
        "inserted": inserted,
        "updated": updated,
        "elapsed_seconds": elapsed.total_seconds()
    }

if __name__ == '__main__':
    import sys
    result = run_daily_harvest()
    logger.info(f"Final result: {result}")
    sys.exit(0 if result['inserted'] + result['updated'] > 0 else 1)
```

### Deduplication Engine

```python
# pipeline/deduplication.py
import hashlib
from typing import List, Dict

class DeduplicationEngine:
    """Remove duplicate listings across sources"""
    
    def __init__(self, conn):
        self.conn = conn
    
    def dedup(self, listings: List[Dict]) -> List[Dict]:
        """
        Remove duplicates using fingerprinting.
        
        Strategy:
        1. Create fingerprint from address + price + area
        2. If fingerprint seen before, keep only newest
        3. Return unique listings
        """
        
        seen = {}
        
        for listing in listings:
            # Create fingerprint
            fp = self.fingerprint(listing)
            
            # If unseen, add
            if fp not in seen:
                seen[fp] = listing
            else:
                # If newer, replace
                old_updated = seen[fp].get('updated_at', '')
                new_updated = listing.get('updated_at', '')
                
                if new_updated > old_updated:
                    seen[fp] = listing
        
        return list(seen.values())
    
    def fingerprint(self, listing: Dict) -> str:
        """
        Create deterministic fingerprint from listing.
        
        Formula: SHA256(address_normalized | price_rounded | area_rounded)
        
        This ensures:
        - Same property from multiple sources = same fingerprint
        - Price changes don't break matching
        - Small area rounding doesn't matter
        """
        
        # Normalize address
        address = (listing.get('address', '') or '').strip().lower()
        address = ' '.join(address.split())  # Remove extra spaces
        
        # Round price to nearest 10k
        price = int(listing.get('price', 0) or 0)
        price_rounded = (price // 10000) * 10000
        
        # Round area to nearest 5 sqm
        area = float(listing.get('area_sqm', 0) or 0)
        area_rounded = int((area // 5) * 5)
        
        # Create fingerprint key
        key = f"{address}|{price_rounded}|{area_rounded}"
        
        # Hash for consistency
        return hashlib.sha256(key.encode()).hexdigest()[:16]
```

### Normalization Engine

```python
# pipeline/normalization.py
from datetime import datetime

class NormalizationEngine:
    """Convert raw scraper output to canonical format"""
    
    def normalize(self, listing: Dict) -> Dict:
        """Convert to canonical format"""
        
        return {
            # IDs
            "id": self.generate_id(listing),
            "source": listing.get('source', 'unknown'),
            "external_id": listing.get('external_id', ''),
            
            # Titles & descriptions (Hebrew)
            "title": self.normalize_text(listing.get('title', '')),
            "title_normalized": self.normalize_title(listing.get('title', '')),
            "description": self.normalize_text(listing.get('description', '')),
            
            # Location (Hebrew)
            "address": self.normalize_address(listing.get('address', '')),
            "address_normalized": self.normalize_text(listing.get('address', '')),
            "city": self.normalize_city(listing.get('city', '')),
            "neighborhood": listing.get('neighborhood', ''),
            "street": listing.get('street', ''),
            
            # Coordinates
            "lat": self.parse_float(listing.get('lat')),
            "lon": self.parse_float(listing.get('lon')),
            
            # Price & Area
            "price": self.parse_float(listing.get('price')),
            "price_currency": listing.get('price_currency', 'NIS'),
            "area_sqm": self.parse_float(listing.get('area_sqm')),
            
            # Property Details
            "rooms": self.parse_float(listing.get('rooms')),
            "bathrooms": self.parse_int(listing.get('bathrooms', 1)),
            "floor": self.parse_int(listing.get('floor', '')),
            "total_floors": self.parse_int(listing.get('total_floors', '')),
            "parking": self.parse_bool(listing.get('parking')),
            "balcony": self.parse_bool(listing.get('balcony')),
            "elevator": self.parse_bool(listing.get('elevator')),
            "condition": self.normalize_condition(listing.get('condition', '')),
            "property_type": self.normalize_property_type(listing.get('property_type', '')),
            "year_built": self.parse_int(listing.get('year_built', '')),
            
            # Contact Info
            "agent_name": listing.get('agent_name', ''),
            "agency_name": listing.get('agency_name', ''),
            "phone": self.normalize_phone(listing.get('phone', '')),
            "email": listing.get('email', ''),
            
            # Media
            "url": listing.get('url', ''),
            "image_url": listing.get('image_url', ''),
            "images": listing.get('images', []),
            
            # Timestamps (ISO 8601)
            "posted_at": self.parse_timestamp(listing.get('posted_at')),
            "updated_at": self.parse_timestamp(listing.get('updated_at')),
            "captured_at": datetime.now().isoformat(),
            "last_verified_at": datetime.now().isoformat(),
            
            # Metadata
            "raw_source": listing,
        }
    
    @staticmethod
    def normalize_text(text):
        """Normalize Hebrew/English text"""
        if not text:
            return ''
        # Remove extra whitespace, normalize quotes
        text = ' '.join(text.split())
        return text.strip()
    
    @staticmethod
    def normalize_title(title):
        """Normalize title for search"""
        normalized = NormalizationEngine.normalize_text(title)
        return normalized.lower()
    
    @staticmethod
    def normalize_address(address):
        """Normalize street address"""
        return NormalizationEngine.normalize_text(address)
    
    @staticmethod
    def normalize_city(city):
        """Normalize city name"""
        # Map common variations
        city_map = {
            'תא': 'תל אביב',
            'תל אביב יפו': 'תל אביב',
            'ירושלים עיר': 'ירושלים',
        }
        city = city.strip().lower()
        return city_map.get(city, city)
    
    @staticmethod
    def normalize_condition(condition):
        """Map condition values"""
        condition_map = {
            'new': 'new',
            'renovated': 'renovated',
            'needs_work': 'needs_work',
            'normal': 'normal',
        }
        condition = (condition or '').lower().strip()
        return condition_map.get(condition, 'unknown')
    
    @staticmethod
    def normalize_property_type(prop_type):
        """Map property type values"""
        type_map = {
            'apartment': 'apartment',
            'house': 'house',
            'villa': 'villa',
            'commercial': 'commercial',
            'land': 'land',
        }
        prop_type = (prop_type or '').lower().strip()
        return type_map.get(prop_type, 'apartment')
    
    @staticmethod
    def normalize_phone(phone):
        """Normalize phone number"""
        if not phone:
            return ''
        # Remove spaces, dashes, etc
        phone = ''.join(c for c in phone if c.isdigit() or c == '+')
        return phone
    
    @staticmethod
    def parse_float(value):
        """Safely parse float"""
        if value is None or value == '':
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def parse_int(value):
        """Safely parse int"""
        if value is None or value == '':
            return None
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def parse_bool(value):
        """Safely parse bool"""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ('true', 'yes', '1', 'on')
        return bool(value)
    
    @staticmethod
    def parse_timestamp(value):
        """Parse timestamp to ISO 8601"""
        if not value:
            return None
        try:
            if isinstance(value, str):
                # Try parsing ISO format
                return datetime.fromisoformat(value).isoformat()
            elif isinstance(value, int):
                # Unix timestamp
                return datetime.fromtimestamp(value).isoformat()
            else:
                return None
        except (ValueError, OSError):
            return None
    
    @staticmethod
    def generate_id(listing):
        """Generate unique ID"""
        source = listing.get('source', 'unknown')
        ext_id = listing.get('external_id', '')
        return f"{source}:{ext_id}"
```

---

## 2️⃣ Sync to Render (Encrypted Backup)

### Local Machine Sync

```python
# jobs/sync_to_render.py
import gzip
import os
import requests
from cryptography.fernet import Fernet
from datetime import datetime

def sync_to_render():
    """
    Sync local database to Render:
    1. Compress SQLite DB
    2. Encrypt with password
    3. Upload via HTTPS
    4. Render verifies and restores
    """
    
    db_path = 'data/planwatch.sqlite3'
    password = os.getenv('SYNC_PASSWORD')
    render_url = os.getenv('RENDER_SYNC_URL')
    
    if not password:
        raise ValueError("SYNC_PASSWORD not set")
    
    # Step 1: Compress
    compressed = compress_db(db_path)
    print(f"Compressed: {compressed}")
    
    # Step 2: Encrypt
    encrypted = encrypt_file(compressed, password)
    print(f"Encrypted: {encrypted}")
    
    # Step 3: Upload
    upload_to_render(encrypted, render_url)
    print("Uploaded to Render")
    
    # Step 4: Cleanup
    os.remove(compressed)
    os.remove(encrypted)
    print("Cleanup done")

def compress_db(db_path):
    """Compress database with gzip"""
    compressed_path = 'data/backup.sqlite3.gz'
    
    with open(db_path, 'rb') as f_in:
        with gzip.open(compressed_path, 'wb') as f_out:
            f_out.write(f_in.read())
    
    return compressed_path

def encrypt_file(file_path, password):
    """Encrypt file with fernet"""
    # Derive key from password
    import hashlib
    import base64
    
    key_material = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode(),
        b'nadlanfix-salt',
        iterations=100000
    )
    key = base64.urlsafe_b64encode(key_material[:32])
    
    # Encrypt
    cipher = Fernet(key)
    encrypted_path = file_path + '.enc'
    
    with open(file_path, 'rb') as f:
        data = f.read()
        encrypted_data = cipher.encrypt(data)
    
    with open(encrypted_path, 'wb') as f:
        f.write(encrypted_data)
    
    return encrypted_path

def upload_to_render(file_path, render_url):
    """Upload encrypted backup to Render"""
    
    headers = {
        'Authorization': f'Bearer {os.getenv("RENDER_SYNC_TOKEN")}'
    }
    
    with open(file_path, 'rb') as f:
        files = {'backup': f}
        response = requests.post(
            f"{render_url}/api/admin/sync/restore",
            files=files,
            headers=headers,
            timeout=300  # 5 minute timeout for large DB
        )
    
    response.raise_for_status()
    return response.json()
```

### Render Side Restore

```python
# dashboard.py - Restore endpoint
from flask import Flask, request, jsonify
import os
import gzip
import sqlite3
from cryptography.fernet import Fernet
import hashlib
import base64

app = Flask(__name__)

@app.route('/api/admin/sync/restore', methods=['POST'])
@require_admin_auth
def restore_from_sync():
    """
    Receive encrypted backup from local machine.
    
    Process:
    1. Receive encrypted file
    2. Decrypt with password
    3. Decompress with gzip
    4. Verify integrity
    5. Atomic swap (old → backup, new → active)
    6. Verify new DB
    """
    
    if 'backup' not in request.files:
        return jsonify({'error': 'No backup file'}), 400
    
    backup_file = request.files['backup']
    password = os.getenv('SYNC_PASSWORD')
    
    encrypted_path = '/tmp/backup.enc'
    backup_file.save(encrypted_path)
    
    try:
        # Step 1: Decrypt
        key_material = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode(),
            b'nadlanfix-salt',
            iterations=100000
        )
        key = base64.urlsafe_b64encode(key_material[:32])
        
        cipher = Fernet(key)
        decrypted_path = '/tmp/backup.gz'
        
        with open(encrypted_path, 'rb') as f:
            encrypted_data = f.read()
        
        decrypted_data = cipher.decrypt(encrypted_data)
        with open(decrypted_path, 'wb') as f:
            f.write(decrypted_data)
        
        # Step 2: Decompress
        db_path_new = '/tmp/planwatch.sqlite3.new'
        with gzip.open(decrypted_path, 'rb') as f_in:
            with open(db_path_new, 'wb') as f_out:
                f_out.write(f_in.read())
        
        # Step 3: Verify integrity
        verify_db(db_path_new)
        
        # Step 4: Atomic swap
        import shutil
        db_path_active = '/data/planwatch.sqlite3'
        db_path_backup = '/data/planwatch.sqlite3.old'
        
        if os.path.exists(db_path_active):
            shutil.move(db_path_active, db_path_backup)
        
        shutil.move(db_path_new, db_path_active)
        
        # Step 5: Verify
        count = verify_db(db_path_active)
        
        # Log restore
        logger.info(f"Database restored: {count} listings")
        
        return jsonify({
            'status': 'restored',
            'listings': count,
            'timestamp': datetime.now().isoformat()
        }), 200
    
    except Exception as e:
        logger.error(f"Restore failed: {e}")
        return jsonify({'error': str(e)}), 500
    
    finally:
        # Cleanup temp files
        for path in [encrypted_path, decrypted_path]:
            if os.path.exists(path):
                os.remove(path)

def verify_db(db_path):
    """Verify database integrity"""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA integrity_check")
        count = conn.execute(
            "SELECT COUNT(*) FROM normalized_listings"
        ).fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        raise ValueError(f"Database verification failed: {e}")
```

---

## 3️⃣ Render API Layer

### Read-Only Dashboard API

```python
# dashboard.py
from flask import Flask, request, jsonify, render_template
import sqlite3
import os
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.getenv('PLANWATCH_DB', '/data/planwatch.sqlite3')

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============= PUBLIC ENDPOINTS (No Auth) =============

@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint"""
    try:
        conn = get_db()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get aggregated statistics"""
    conn = get_db()
    
    stats = {
        "total_listings": conn.execute(
            "SELECT COUNT(*) FROM normalized_listings"
        ).fetchone()[0],
        
        "by_city": dict(conn.execute("""
            SELECT city, COUNT(*) as count 
            FROM normalized_listings
            WHERE city IS NOT NULL
            GROUP BY city 
            ORDER BY count DESC 
            LIMIT 15
        """).fetchall()),
        
        "by_source": dict(conn.execute("""
            SELECT source, COUNT(*) as count 
            FROM normalized_listings
            GROUP BY source
            ORDER BY count DESC
        """).fetchall()),
        
        "price_stats": {
            "min": conn.execute(
                "SELECT MIN(price) FROM normalized_listings"
            ).fetchone()[0],
            "max": conn.execute(
                "SELECT MAX(price) FROM normalized_listings"
            ).fetchone()[0],
            "avg": conn.execute(
                "SELECT AVG(price) FROM normalized_listings"
            ).fetchone()[0],
            "median": conn.execute(
                "SELECT price FROM normalized_listings ORDER BY price LIMIT 1 OFFSET (SELECT COUNT(*)/2 FROM normalized_listings)"
            ).fetchone()[0],
        },
        
        "area_stats": {
            "min": conn.execute(
                "SELECT MIN(area_sqm) FROM normalized_listings"
            ).fetchone()[0],
            "max": conn.execute(
                "SELECT MAX(area_sqm) FROM normalized_listings"
            ).fetchone()[0],
            "avg": conn.execute(
                "SELECT AVG(area_sqm) FROM normalized_listings"
            ).fetchone()[0],
        },
        
        "last_updated": conn.execute(
            "SELECT MAX(updated_at) FROM normalized_listings"
        ).fetchone()[0],
        
        "sync_status": {
            "last_sync": conn.execute(
                "SELECT MAX(created_at) FROM sync_log"
            ).fetchone()[0],
            "sync_count": conn.execute(
                "SELECT COUNT(*) FROM sync_log"
            ).fetchone()[0],
        }
    }
    
    conn.close()
    return jsonify(stats), 200

# ============= PROTECTED ENDPOINTS (Basic Auth) =============

@app.route('/api/local-listings', methods=['GET'])
@require_auth
def get_listings():
    """Get paginated listings from database"""
    
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    city = request.args.get('city', None)
    min_price = request.args.get('min_price', 0, type=float)
    max_price = request.args.get('max_price', None, type=float)
    
    conn = get_db()
    
    # Build query
    query = "SELECT * FROM normalized_listings WHERE 1=1"
    params = []
    
    if city:
        query += " AND city = ?"
        params.append(city)
    
    query += " AND price >= ?"
    params.append(min_price)
    
    if max_price:
        query += " AND price <= ?"
        params.append(max_price)
    
    # Count total
    count_result = conn.execute(
        query.replace("SELECT *", "SELECT COUNT(*)")
    , params).fetchone()
    total = count_result[0]
    
    # Get paginated results
    query += " ORDER BY updated_at DESC, price DESC LIMIT ? OFFSET ?"
    params.extend([limit, (page - 1) * limit])
    
    listings = conn.execute(query, params).fetchall()
    conn.close()
    
    return jsonify({
        "listings": [dict(l) for l in listings],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit,
        "cached": True
    }), 200

@app.route('/api/search', methods=['GET'])
@require_auth
def search_listings():
    """Full-text search in listings"""
    
    query_str = request.args.get('q', '').strip()
    if not query_str or len(query_str) < 2:
        return jsonify({"error": "Query too short"}), 400
    
    conn = get_db()
    
    # Search in title + description + address
    results = conn.execute("""
        SELECT * FROM normalized_listings
        WHERE 
            title_normalized LIKE ? OR
            description LIKE ? OR
            address_normalized LIKE ?
        ORDER BY updated_at DESC
        LIMIT 100
    """, [f"%{query_str}%"] * 3).fetchall()
    
    conn.close()
    
    return jsonify({
        "results": [dict(r) for r in results],
        "count": len(results)
    }), 200
```

---

## 4️⃣ Cloudflare Configuration

### Free Tier Setup

1. **Add Domain to Cloudflare**
   ```
   nameservers:
     - ns1.cloudflare.com
     - ns2.cloudflare.com
   ```

2. **DNS Records**
   ```
   Type    Name              Content
   CNAME   planwatch         planwatch.onrender.com
   CNAME   www               planwatch.onrender.com
   TXT     _acme-challenge   (for SSL cert)
   ```

3. **Caching Rules**
   ```
   Cache Everything:
     - Path: /api/stats (1 hour)
     - Path: /static/* (1 week)
   
   Bypass Cache:
     - Path: /api/admin/* (admin endpoints)
     - Path: /api/sync/* (sync endpoints)
   ```

4. **Rate Limiting (free)**
   ```
   100 requests per minute per IP
   Block after 10 failed auth attempts
   ```

5. **Bot Protection (free)**
   ```
   - Manage bots: Enable
   - Verified bots: Allow
   - Unverified bots: Block
   ```

---

## Performance Target

### Expected Performance

| Metric | Target | Notes |
|--------|--------|-------|
| API Response | < 200ms | Cached from SQLite |
| Page Load | < 1s | Cloudflare CDN |
| Database Query | < 50ms | SQLite LIMIT 50 |
| Full Sync | < 15 min | Compressed + encrypted |
| Concurrent Users | 100+ | Render Standard supports this |
| Uptime | 99.5%+ | Render + Cloudflare SLA |

---

## Cost Efficiency Summary

```
Component               Cost/mo    Benefit
─────────────────────────────────────────
Local Scraping          $0         Your machine time
Render API              $12        Always-on server
Cloudflare CDN          $0         Edge caching
GitHub Repo             $0         Free tier
─────────────────────────────────────────
TOTAL                   $12/mo     ~$144/year
```

**vs. Alternative:**
- AWS Lambda: $50-100/mo (scaled)
- Firebase: $30-50/mo (limited)
- Heroku: $50/mo (minimum)
- Custom VPS: $5-10/mo (but no CDN/security)

**Recommendation:** Render + Cloudflare is optimal for this use case.
