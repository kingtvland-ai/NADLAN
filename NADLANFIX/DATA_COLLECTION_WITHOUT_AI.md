# איסוף נתונים ללא AI - NADLANFIX

**עדכון אחרון:** 2026-08-17  
**מטרה:** שיטות ופרוטוקולים לאיסוף נתונים מיד 2 ופרוביידרים אחרים ללא תלות ב-AI

---

## 📋 תוכן העניינים

1. [סקירה](#סקירה)
2. [אדריכלות איסוף נתונים](#אדריכלות-איסוף-נתונים)
3. [יד 2 - Protocol](#יד-2---protocol)
4. [Facebook Marketplace - Protocol](#facebook-marketplace---protocol)
5. [על המפה - Protocol](#על-המפה---protocol)
6. [פרוביידרים נוספים](#פרוביידרים-נוספים)
7. [טיפול בשגיאות](#טיפול-בשגיאות)
8. [תחזוקה](#תחזוקה)

---

## סקירה

NADLANFIX collects real estate listing data from multiple sources using programmatic web scraping and API access, without relying on AI/LLM for data extraction. All data collection is deterministic, rule-based, and fully reproducible.

### Data Collection Architecture

```
Source APIs/Websites
    ↓
Adapter Layer (sources/)
├── fetch()        # Raw data retrieval
├── normalize()    # Canonical format
├── validate()     # Quality checks
└── persist()      # Write to DB
    ↓
SQLite Database
    ↓
Dashboard API
    ↓
Applications & Reports
```

### Key Principles

1. **No AI/LLM involved** - All extraction is rule-based and deterministic
2. **Reproducible** - Same input always produces same output
3. **Auditable** - Every extraction step is logged and traceable
4. **Fault tolerant** - Failures don't corrupt existing data
5. **Rate-limited** - Respects source provider rate limits
6. **Cached** - Minimizes API calls and redundant fetches

---

## אדריכלות איסוף נתונים

### Component Overview

```python
# sources/base/adapter.py
class BaseSourceAdapter:
    """Interface all sources must implement"""
    
    def fetch(conn, target):
        """Get raw data from source API/website"""
        pass
    
    def normalize(raw):
        """Convert to canonical format"""
        pass
    
    def validate(listing):
        """Check data quality"""
        pass
    
    def persist(conn, listing):
        """Write to database"""
        pass
```

### Data Flow Per Source

```
Raw Listing
    ↓
1. Fetch Phase
   - HTTP request / Playwright navigation
   - Parse JSON/HTML response
   - Handle pagination
   
    ↓
2. Normalize Phase
   - Map fields to canonical schema
   - Convert units (sq meters, NIS, etc.)
   - Extract address components
   - Standardize text (Hebrew/English)
   
    ↓
3. Validate Phase
   - Check required fields present
   - Validate numeric ranges (price, area)
   - Remove duplicates
   - Check for offensive/invalid content
   
    ↓
4. Persist Phase
   - Check if listing exists (by fingerprint)
   - Insert new or update existing
   - Record timestamp
   - Log to audit trail
```

### Canonical Schema

All listings normalized to this format:

```python
class CanonicalListing:
    listing_id: str              # Unique ID for this listing
    source: str                  # yad2, facebook, onmap
    external_id: str             # ID from original source
    
    # Basic Info
    title: str                   # Property name/title
    description: str             # Full text description
    normalized_title: str        # Title with spaces normalized
    
    # Location
    city: str                    # City name (Hebrew)
    neighborhood: str            # Neighborhood
    street: str                  # Street address
    address_text: str            # Full text address
    lat: float                   # Latitude (WGS84)
    lon: float                   # Longitude (WGS84)
    gush: str                    # Parcel block (גוש)
    helka: str                   # Parcel lot (חלקה)
    
    # Property Details
    property_type: str           # house, apartment, etc.
    rooms: float                 # Number of rooms
    area_sqm: float              # Area in m²
    floor: str                   # Floor number
    bathrooms: int               # Number of bathrooms
    parking: bool                # Has parking
    condition: str               # new, renovated, needs_work
    
    # Pricing
    price: float                 # Price in NIS
    price_text: str              # Original price text
    currency: str                # NIS, USD, etc.
    previous_price: float        # Earlier price (if tracked)
    
    # Media
    image_url: str               # Main image URL
    images: list[str]            # All image URLs
    
    # Seller Info
    seller_name: str             # Contact person
    agency: str                  # Real estate agency
    phone: str                   # Phone number
    phone_source: str            # Where phone came from
    
    # Tracking
    first_seen_at: str           # ISO timestamp
    last_seen_at: str            # ISO timestamp
    captured_at: str             # When data captured
    delisted_at: str             # When removed (if known)
    seen_count: int              # How many times observed
    
    # Quality Metrics
    deal_score: float            # Calculated opportunity score
    score_confidence: float      # Confidence 0-1
    score_reasons: list[str]     # Why this score
```

---

## יד 2 - Protocol

### Overview

Yad2 (יד 2) is Israel's largest real estate portal. NADLANFIX uses two approaches:

1. **Public Search Feed** - HTTP API, no authentication needed
2. **Premium Listings** - Requires browser automation to bypass bot protection

### Approach 1: Public Search Feed (Recommended)

**File:** `ingestion/feeds/yad2_feed.py`

```python
# URL: https://www.yad2.co.il/realestate/api/listings/search?...

# Parameters
{
    "type": 2,                    # 2 = sale, 1 = rent
    "area": "תל אביב",            # City name
    "page": 1,
    "pageSize": 100,
    "sortBy": "lastUpdate",
    "sortDirection": "desc",
    "minPrice": None,
    "maxPrice": None,
    "minArea": None,
    "maxArea": None,
    "propertyType": "all",
    "withPhotos": True
}

# Response
{
    "results": [
        {
            "token": "yad2_123456",    # Unique ID
            "title": "דירה 3 חדרים, תל אביב",
            "price": 1800000,
            "priceText": "₪ 1.8M",
            "area": 85,
            "rooms": 3,
            "floor": "2",
            "city": "תל אביב",
            "address": "רחוב הבנים 10",
            "imageUrl": "...",
            "description": "...",
            "url": "https://www.yad2.co.il/..."
        }
    ],
    "totalCount": 4523
}
```

**Fetching Strategy:**

```python
def harvest_yad2_public_feed():
    """
    1. Start with empty results list
    2. For each city in CITIES:
       a. For each property type in TYPES:
          i. Set page = 1
          ii. Request paginated API results
          iii. Parse JSON response (no HTML parsing needed)
          iv. Extract fields to canonical format
          v. Check for duplicates (by token)
          vi. Insert/update in database
          vii. Increment page
          viii. Repeat until no more results
    3. Record run status and timestamp
    4. Return summary (rows_seen, rows_new, rows_updated)
    """
    pass
```

**Handling Pagination:**

- Yad2 API returns max 100 items per page
- Total count provided upfront
- Keep fetching until `page * pageSize >= totalCount`
- Cache results to avoid re-fetching same page

**Deduplication:**

```python
# Yad2 tokens are stable identifiers
# Check before insert:
existing = db.execute(
    "SELECT token FROM yad2_listings WHERE token = ?",
    (yad2_token,)
).fetchone()

if existing:
    # Update: newer price, floor info, photos
    db.execute("UPDATE yad2_listings SET ... WHERE token = ?")
else:
    # Insert: new listing
    db.execute("INSERT INTO yad2_listings VALUES ...")
```

**Field Mapping:**

```python
canonical = {
    "listing_id": f"yad2:{token}",
    "source": "yad2",
    "external_id": token,
    "title": response["title"],
    "description": response.get("description", ""),
    "price": response["price"],
    "rooms": response["rooms"],
    "area_sqm": response["area"],
    "floor": str(response["floor"]),
    "city": response["city"],
    "address_text": response["address"],
    "image_url": response["imageUrl"],
    # ... map all fields
}
```

### Approach 2: Premium Listings (Browser Automation)

**File:** `backend/src/feed.js` (Node.js + Playwright)

**Why Browser?** Yad2 has Radware bot protection. Real authenticated Chromium profile can access full listing details.

```javascript
// backend/src/feed.js
const { chromium } = require('playwright');

async function harvestYad2PremiumListings() {
    // 1. Launch browser with persistent profile
    const context = await browser.createBrowserContext({
        storageState: './data/yad2-profile.json'  // Persistent session
    });
    
    // 2. Navigate to Yad2
    const page = await context.newPage();
    await page.goto('https://www.yad2.co.il/realestate/');
    
    // 3. Fetch listings via XHR
    const listings = await page.evaluate(() => {
        // Return array of listing elements
        return Array.from(document.querySelectorAll('[data-listing-id]'))
            .map(el => extractListingFromDOM(el));
    });
    
    // 4. For each listing, click for details
    for (const listing of listings) {
        await page.click(`[data-listing-id="${listing.id}"]`);
        await page.waitForNavigation();
        
        const details = await page.evaluate(() => {
            // Extract detailed info from detail page
            return {
                phone: document.querySelector('[data-phone]')?.textContent,
                details: document.querySelector('.details')?.textContent,
                images: Array.from(document.querySelectorAll('img[src*=listing]'))
                    .map(i => i.src)
            };
        });
        
        // 5. Normalize and persist
        const canonical = normalizeYad2(listing, details);
        persistListing(canonical);
    }
    
    // 6. Save profile for next run
    await context.storageState({ path: './data/yad2-profile.json' });
}
```

**Persistence Strategy:**

- Don't use browser for initial scrape (too slow)
- Use browser only to:
  1. Extract phone numbers (hidden from direct API)
  2. Get full descriptions
  3. Download images
  4. Verify listings still active

---

## Facebook Marketplace - Protocol

### Overview

Facebook Marketplace has millions of property listings. NADLANFIX accesses via:

1. **Facebook Graph API** (if developer account available)
2. **Web Scraping** with Playwright (public listings)

### Approach: Web Scraping

**File:** `ingestion/feeds/facebook_feed.py`

**URL Pattern:** `https://www.facebook.com/marketplace/category/housing`

```python
def harvest_facebook_marketplace(
    query="מכירות בתים",           # Search query
    min_price=400000,
    max_price=5000000,
    target=500                      # Number of listings to fetch
):
    """
    1. Navigate to Facebook Marketplace
    2. Enter search query
    3. Apply filters (price range, location)
    4. Scroll/paginate to load listings
    5. Extract listing data from DOM
    6. Handle infinite scroll
    7. Persist unique listings
    8. Stop when target reached or no more results
    """
    pass
```

**Parsing Strategy:**

```javascript
// Extract listing info from Facebook DOM
const listings = Array.from(
    document.querySelectorAll('[role="article"]')
).map(article => {
    const link = article.querySelector('a[href*="/marketplace/item"]');
    const price = article.querySelector('[data-price]')?.textContent;
    const title = article.querySelector('h3')?.textContent;
    const image = article.querySelector('img')?.src;
    
    return {
        id: extractIdFromLink(link.href),
        url: link.href,
        title: title,
        price: parsePrice(price),
        image: image,
        seller: extractSellerInfo(article),
        description: article.querySelector('[data-description]')?.textContent
    };
});
```

**Deduplication:**

```python
# Facebook listing URLs are stable
# Extract listing_id from URL: m.facebook.com/marketplace/item/{id}
# Check before insert:
existing = db.execute(
    "SELECT id FROM facebook_listings WHERE id = ?",
    (listing_id,)
).fetchone()
```

**Field Mapping:**

```python
canonical = {
    "listing_id": f"facebook:{listing_id}",
    "source": "facebook",
    "external_id": listing_id,
    "title": title,
    "description": description,
    "price": price,
    "city": extracted_from_description_or_manual,
    "image_url": primary_image,
    "images": all_images,
    "url": facebook_marketplace_url,
    "seller_name": seller_name,
    "phone": seller_phone_if_visible,
    # ... other fields
}
```

**Handling Authentication:**

```python
# Save browser state after login
playwright_context.storageState = './data/facebook_storage_state.json'

# Reuse on next run:
context = browser.createBrowserContext(
    storageState='./data/facebook_storage_state.json'
)

# If login required, exit gracefully with warning
# User must login manually first time or provide credentials
```

**Rate Limiting:**

- Wait 2-5 seconds between page loads
- Don't scroll faster than browser can render
- Respect Facebook's rate limit headers
- If blocked (403/429), pause for 1 hour

---

## על המפה - Protocol

### Overview

"על המפה" (onmap.co.il) is Israel's second-largest property portal after Yad2.

**File:** `ingestion/feeds/onmap_feed.py`

### API Documentation

```
Base URL: https://onmap.co.il/api/

Search Endpoint:
GET /api/realestate/search?
  type=sale                          # sale, rent
  region=תל אביב                     # Region/city
  page=1
  pageSize=50
  sortBy=date_modified
  sortDirection=desc
  filter[price_from]=0
  filter[price_to]=10000000
  filter[area_from]=0
  filter[area_to]=500

Response:
{
    "results": [
        {
            "id": "onmap_12345",
            "title": "דירה 3.5 חדרים",
            "price": 2500000,
            "area": 90,
            "rooms": 3.5,
            "city": "תל אביב",
            "street": "רחוב העצמאות 10",
            "url": "https://onmap.co.il/property/...",
            "images": ["url1", "url2"],
            "seller": {
                "name": "...",
                "phone": "...",
                "agency": "..."
            },
            "metadata": {
                "listedDate": "2026-08-15",
                "modifiedDate": "2026-08-17"
            }
        }
    ],
    "totalCount": 2341,
    "pageCount": 47
}
```

**Fetching Strategy:**

```python
def harvest_onmap(
    regions=['תל אביב', 'ירושלים', 'חיפה'],
    listing_type='sale',
    target=5000
):
    """
    1. For each region in regions:
       a. Set page = 1, pageSize = 50
       b. Request listings
       c. Parse JSON
       d. Normalize each listing
       e. Check for duplicates
       f. Persist to database
       g. Increment page
       h. Continue until pageCount reached or target met
    """
    pass
```

**API Response Parsing:**

```python
import requests

def fetch_onmap_page(city, page, pageSize=50):
    """Fetch single page of results"""
    url = 'https://onmap.co.il/api/realestate/search'
    params = {
        'type': 'sale',
        'region': city,
        'page': page,
        'pageSize': pageSize,
        'sortBy': 'date_modified'
    }
    
    response = requests.get(url, params=params, timeout=10)
    return response.json()

def process_onmap_response(response):
    """Convert raw API response to canonical format"""
    listings = []
    
    for item in response['results']:
        canonical = {
            "listing_id": f"onmap:{item['id']}",
            "source": "onmap",
            "external_id": item['id'],
            "title": item['title'],
            "price": item['price'],
            "rooms": item['rooms'],
            "area_sqm": item['area'],
            "city": item['city'],
            "street": item['street'],
            "address_text": f"{item['street']}, {item['city']}",
            "image_url": item['images'][0] if item['images'] else None,
            "images": item['images'],
            "url": item['url'],
            "seller_name": item.get('seller', {}).get('name'),
            "agency": item.get('seller', {}).get('agency'),
            "phone": item.get('seller', {}).get('phone'),
            # ... map all fields
        }
        listings.append(canonical)
    
    return listings
```

**Deduplication:**

```python
# Check if listing exists by external_id
existing = db.execute(
    "SELECT id FROM onmap_listings WHERE id = ?",
    (onmap_id,)
).fetchone()

if existing and listing_updated():
    # Update: price change, new images, etc.
    update_onmap_listing(listing)
else:
    # Insert: new listing
    insert_onmap_listing(listing)
```

**Handling Updates:**

```python
# ONMAP includes modification timestamp
last_modified = datetime.fromisoformat(item['metadata']['modifiedDate'])

# Track when we last saw it
stored_modified = db.execute(
    "SELECT modified_at FROM onmap_listings WHERE id = ?",
    (item['id'],)
).fetchone()

if stored_modified < last_modified:
    # Listing was updated since last fetch
    update_fields = [
        'price', 'rooms', 'area_sqm', 'images',
        'description', 'modified_at'
    ]
    update_listing_fields(item['id'], item, update_fields)
```

---

## פרוביידרים נוספים

### Adding a New Source

To add a new real estate provider:

1. **Create Adapter**

```python
# sources/<provider_name>/adapter.py
from sources.base.adapter import BaseSourceAdapter

class MyProviderAdapter(BaseSourceAdapter):
    @property
    def source_name(self):
        return "myprovider"
    
    @property
    def display_name(self):
        return "My Provider"
    
    def ensure_schema(self, conn):
        # Create provider-specific tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS myprovider_listings (
                id TEXT PRIMARY KEY,
                title TEXT,
                price REAL,
                ... other fields
            )
        """)
    
    def fetch(self, conn, target):
        # Implement fetching logic
        # Return list of raw listing dicts
        results = []
        for listing in get_listings():
            results.append(listing)
        return results
    
    def normalize(self, raw):
        # Convert raw to canonical format
        return {
            "listing_id": f"myprovider:{raw['id']}",
            "source": "myprovider",
            "external_id": raw['id'],
            "title": raw['title'],
            # ... map all fields
        }
    
    def count(self, conn):
        # Return stats
        return {
            "total": conn.execute(
                "SELECT COUNT(*) FROM myprovider_listings"
            ).fetchone()[0]
        }
```

2. **Register in Daily Ingest**

```python
# jobs/daily_ingest.py
from sources.myprovider.adapter import MyProviderAdapter

DEFAULT_SOURCES = {
    "yad2": {...},
    "facebook": {...},
    "onmap": {...},
    "myprovider": {
        "target": 1000,
        "enabled": True
    }
}
```

3. **Test**

```bash
python -m pytest tests/smoke_tests.py::test_source_adapters
```

---

## טיפול בשגיאות

### Common Errors & Solutions

**Error: "Connection timeout"**
```python
# Increase timeout and retry
MAX_RETRIES = 3
TIMEOUT = 30  # seconds

import tenacity
@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
    stop=tenacity.stop_after_attempt(3)
)
def fetch_with_retry():
    return requests.get(url, timeout=TIMEOUT)
```

**Error: "403 Forbidden" (rate limited)**
```python
import time
import random

# Exponential backoff
for attempt in range(MAX_RETRIES):
    try:
        response = requests.get(url)
        if response.status_code == 403:
            wait_time = 2 ** attempt + random.uniform(0, 1)
            logger.warning(f"Rate limited, waiting {wait_time}s")
            time.sleep(wait_time)
        else:
            return response
    except Exception as e:
        logger.error(f"Attempt {attempt} failed: {e}")
```

**Error: "Invalid JSON response"**
```python
# Check Content-Type, handle HTML errors
try:
    data = response.json()
except json.JSONDecodeError:
    if 'text/html' in response.headers.get('content-type', ''):
        logger.error(f"Got HTML instead of JSON: {response.text[:200]}")
    else:
        logger.error(f"Invalid JSON: {response.text[:200]}")
    raise
```

**Error: "Duplicate listings"**
```python
# Use fingerprinting for deduplication
import hashlib

def create_fingerprint(listing):
    """Create deterministic fingerprint"""
    text = f"{listing['title']}|{listing['city']}|{listing['price']}|{listing['area']}"
    return hashlib.md5(text.encode()).hexdigest()

# Check before insert
fingerprint = create_fingerprint(listing)
existing = db.execute(
    "SELECT id FROM normalized_listings WHERE fingerprint = ?",
    (fingerprint,)
).fetchone()
```

### Logging & Debugging

```python
import logging

logger = logging.getLogger(__name__)

# Log every fetch
logger.info(f"Starting {source_name} harvest, target={target}")
logger.debug(f"Fetching page {page}, pageSize={page_size}")
logger.info(f"Fetched {len(results)} listings from {source_name}")
logger.warning(f"Rate limited, retrying in {wait_time}s")
logger.error(f"Failed to persist listing {listing_id}: {error}")

# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

## תחזוקה

### Regular Maintenance Tasks

**Daily:**
- Monitor scheduler runs: Check `scheduler_runs` table
- Verify data freshness: Check `last_synced_at` timestamp
- Monitor for errors: Check logs for exceptions

**Weekly:**
- Review deduplication stats
- Check for new providers or API changes
- Update source credentials if needed

**Monthly:**
- Analyze data quality metrics
- Check for stale listings (not seen in 30 days)
- Review source health statistics

### Database Maintenance

```python
# Cleanup old data
def cleanup_old_listings(days=90):
    """Remove listings older than N days"""
    cutoff = datetime.now() - timedelta(days=days)
    
    conn = sqlite3.connect(PLANWATCH_DB)
    conn.execute("""
        DELETE FROM normalized_listings
        WHERE last_seen_at < ? AND delisted_at IS NOT NULL
    """, (cutoff.isoformat(),))
    conn.commit()
    logger.info("Cleanup complete")

# Defragment database
def defragment_database():
    """Optimize database performance"""
    conn = sqlite3.connect(PLANWATCH_DB)
    conn.execute("PRAGMA optimize")
    conn.execute("VACUUM")
    conn.commit()
    logger.info("Defragmentation complete")
```

### Monitoring Checklist

```python
def health_check():
    """Daily health check"""
    conn = sqlite3.connect(PLANWATCH_DB)
    
    checks = {
        "database_accessible": check_db_accessible(conn),
        "scheduler_running": check_scheduler_healthy(conn),
        "data_fresh": check_data_freshness(conn),
        "no_error_spike": check_error_rate(conn),
        "disk_space_ok": check_disk_space(),
    }
    
    if not all(checks.values()):
        send_alert(f"Health check failed: {checks}")
    
    return checks

def check_data_freshness(conn):
    """Last successful ingest was recent"""
    last_run = conn.execute("""
        SELECT MAX(finished_at) FROM scheduler_runs
        WHERE status = 'success'
    """).fetchone()[0]
    
    if last_run:
        age = (datetime.now() - datetime.fromisoformat(last_run)).days
        return age <= 1  # Success in last 24 hours
    
    return False
```

---

## Summary: No AI Required

The entire data collection pipeline is:

✅ **Rule-based** - Same input = Same output  
✅ **Auditable** - Every step is logged  
✅ **Reproducible** - Can trace any listing back to source  
✅ **Deterministic** - No randomness or inference  
✅ **Maintainable** - Clear code, easy to debug  
✅ **Scalable** - Works from 100 to 1M listings  

All field extraction, normalization, and validation is done through:
- HTTP API parsing (JSON)
- HTML DOM parsing (CSS selectors, XPath)
- Regex patterns for text extraction
- Rule-based validation logic

**No machine learning, neural networks, or AI involved.**
