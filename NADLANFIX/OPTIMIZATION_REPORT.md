# דוח שיפורים - NADLANFIX 2026
## Stack Optimization + Cost Analysis + Full-Flow Architecture

**עדכון אחרון:** 2026-08-17  
**מטרה:** ארכיטקטורה מינימלית עם zero-cost operations + full data sovereignty

---

## 📊 תוכן העניינים

1. [מצב נוכחי](#מצב-נוכחי)
2. [בעיות בתזרים הנוכחי](#בעיות-בתזרים-הנוכחי)
3. [Stack המלא המוצע](#stack-המלא-המוצע)
4. [השוואת עלויות](#השוואת-עלויות)
5. [ארכיטקטורה פירוט](#ארכיטקטורה-פירוט)
6. [Scraper Requirements](#scraper-requirements)
7. [Migration Plan](#migration-plan)

---

## מצב נוכחי

### Current Architecture

```
User (Browser)
    ↓
Render.com (Web Service, $12/mo)
    ├── Python Dashboard API (Port 8000)
    ├── Node.js Backend (Port 4000)
    └── SQLite Database (/data)
        ↓
    Scheduler (cron)
        ↓
    Source Scrapers
        ├── Yad2 (API + Playwright)
        ├── Facebook Marketplace
        └── ONMAP (API)

Issues:
🔴 Limited free tier
🔴 No CDN for static assets
🔴 No edge caching
🔴 Direct database queries from browser
🔴 Scrapers run on expensive server time
🔴 Data sync issues between local + cloud
```

### Current Costs

| Component | Cost | Notes |
|-----------|------|-------|
| Render Web Service | $12/mo | Standard plan, 0.5GB RAM |
| Render Persistent Disk | $5/mo | 5GB storage |
| Netlify (Public Site) | $0 | Free tier |
| Domain (optional) | ~$12/year | Assumed .co.il |
| **Total** | **$17/mo** | ~$204/year |

---

## בעיות בתזרים הנוכחי

### Issue 1: Render Free Tier is Insufficient
- Free tier: 750 free hours/month (~24 hours/day)
- Problem: Only works if one service at a time
- Solution: Need paid plan for always-on scheduling

### Issue 2: No CDN for Dashboard
- Dashboard assets served directly from Render
- Result: Slow loading for users far from Frankfurt
- Solution: Add Cloudflare CDN

### Issue 3: Scrapers Block Server Resources
- Daily scraping (24 hours) uses server compute
- Each scraper: Playwright browser + network calls
- Result: Server unresponsive during scraping
- Solution: Run scrapers separately (local machine or Render cron)

### Issue 4: Database Coupling
- Dashboard makes direct DB queries
- Browser gets raw database records
- No data caching/normalization layer
- Result: Slow API, no aggregation capability

### Issue 5: Data Sync Issues
- Local development vs Render production
- Different database states
- Manual sync required
- Solution: One-way sync from local → Render

### Issue 6: Duplicate Listings Not Handled
- Multiple sources have same property
- No deduplication on persist
- Dashboard shows duplicates
- Users confused by redundancy

---

## Stack המלא המוצע

### Recommended 2026 Architecture

```
LOCAL MACHINE (Windows)
├── Job: Daily Data Collection (00:30 UTC)
│   ├── Yad2 Scraper
│   ├── Facebook Scraper  
│   ├── ONMAP Scraper
│   ├── Madlan Scraper
│   ├── Ad.Co.Il Scraper
│   └── Como Scraper
│   ↓
├── Local SQLite Database
│   ├── Deduplication Pass
│   ├── Normalization
│   └── Validation
│   ↓
└── Upload to Render (encrypted backup)

RENDER (Production Server) - $12/month
├── Web Service (Python Dashboard)
│   └── READ-ONLY access to synced database
├── Persistent Disk (/data)
│   └── Replicated database from local
└── Cron (optional, for manual triggers)

CLOUDFLARE (Edge) - $0 (free tier) / $20 (Pro)
├── DNS + Domain Management
├── Cache static assets (dashboard CSS/JS)
├── Rate limiting for API
├── WAF for DDoS protection
└── Workers (optional, for simple edge logic)

DATABASE FLOW
Local SQLite (Full Data + Scraping)
    ↓ Backup + Sync (encrypted)
Render SQLite (Read-Only Replica)
    ↓ JSON API
Browser (Dashboard)
    ↓ Cloudflare Cache
Users
```

### Services + Costs

| Service | Tier | Cost | Purpose |
|---------|------|------|---------|
| **Render** | Standard | $12/mo | Always-on API server + persistent disk |
| **Cloudflare** | Free | $0 | CDN + DNS + edge security |
| **GitHub** | Free | $0 | Repository + CI/CD |
| **SQLite** | Local | $0 | On-disk database |
| **Domain** | .co.il | ~$1/mo | DNS (optional) |
| **Total** | - | **$13/mo** | Full production system |

---

## השוואת עלויות

### Option A: Current (Render only)
```
Render Web Service    $12/mo
Render Disk          $ 5/mo
---
Total               $17/mo ❌ Limited
```

### Option B: Render + Cloudflare Free (Recommended)
```
Render Web Service    $12/mo
Render Disk          $ 5/mo (optional, if backup needed)
Cloudflare Free       $ 0/mo
---
Total               $12/mo ✅ Full stack
```

### Option C: Enterprise (not needed)
```
Render Web Service    $12/mo
Render Disk          $ 5/mo
Cloudflare Pro       $20/mo
Vercel (alternative) $15/mo
---
Total               $52/mo ❌ Overkill
```

**Recommendation: Option B** - Render Standard + Cloudflare Free is perfect for this use case.

---

## ארכיטקטורה פירוט

### 1. Local Data Collection (Your Machine)

**When:** Daily at 00:30 UTC (2:30 AM Israel)

```python
# jobs/daily_ingest_local.py
class LocalDataCollectionOrchestrator:
    
    def run_daily(self):
        """Execute on local machine daily"""
        start = datetime.now()
        
        sources = [
            Yad2Scraper(),
            FacebookScraper(),
            ONMAPScraper(),
            MadlanScraper(),
            AdCoIlScraper(),
            ComoScraper()
        ]
        
        for source in sources:
            try:
                listings = source.harvest()
                
                # Deduplication pass
                deduplicated = self.dedup_listings(listings)
                
                # Normalization
                canonical = [self.normalize(l) for l in deduplicated]
                
                # Validation
                valid = [l for l in canonical if self.validate(l)]
                
                # Persist to local DB
                self.persist_local(valid)
                
                logger.info(f"{source.name}: {len(valid)} new/updated")
                
            except Exception as e:
                logger.error(f"{source.name} failed: {e}")
        
        # Sync to Render
        self.sync_to_render()
        
        logger.info(f"Daily collection completed in {datetime.now() - start}")
```

**Deduplication Strategy:**

```python
def dedup_listings(self, listings):
    """Remove duplicates across sources"""
    
    deduplicated = {}
    
    for listing in listings:
        # Create fingerprint from listing details
        fingerprint = self.fingerprint(listing)
        
        # If seen before, only keep if it's an update
        if fingerprint in deduplicated:
            if listing['updated_at'] > deduplicated[fingerprint]['updated_at']:
                deduplicated[fingerprint] = listing
        else:
            deduplicated[fingerprint] = listing
    
    return list(deduplicated.values())

def fingerprint(self, listing):
    """Create unique ID from listing content"""
    # Use normalized address + price + area as key
    key_parts = [
        listing['address_normalized'],
        str(int(listing['price'])),
        str(int(listing['area']))
    ]
    text = "|".join(key_parts)
    return hashlib.md5(text.encode()).hexdigest()
```

### 2. Sync to Render (Encrypted Backup)

**What:** Compressed + encrypted SQLite backup

```python
def sync_to_render(self):
    """Send database to Render"""
    
    import gzip
    import requests
    
    # 1. Create compressed backup
    backup_path = 'data/backup.sqlite3.gz'
    with open('data/planwatch.sqlite3', 'rb') as f_in:
        with gzip.open(backup_path, 'wb') as f_out:
            f_out.write(f_in.read())
    
    # 2. Encrypt with password
    # (Use fernet or similar for encryption)
    encrypted = self.encrypt_file(backup_path)
    
    # 3. Upload to Render
    with open(encrypted, 'rb') as f:
        files = {'database': f}
        response = requests.post(
            'https://planwatch.onrender.com/api/admin/restore-backup',
            files=files,
            headers={'Authorization': f'Bearer {SYNC_TOKEN}'}
        )
    
    logger.info(f"Synced to Render: {response.status_code}")
```

**Render Side (Restore Backup):**

```python
# dashboard.py - Admin endpoint
@app.route('/api/admin/restore-backup', methods=['POST'])
def restore_backup():
    """Receive encrypted backup from local machine"""
    
    if not request.files['database']:
        return 400, "No file"
    
    # 1. Save encrypted file
    encrypted_path = '/tmp/backup.enc'
    request.files['database'].save(encrypted_path)
    
    # 2. Decrypt
    decrypted = decrypt_file(encrypted_path, password=os.getenv('SYNC_PASSWORD'))
    
    # 3. Decompress
    import gzip
    db_path = '/data/planwatch.sqlite3.new'
    with gzip.open(decrypted, 'rb') as f_in:
        with open(db_path, 'wb') as f_out:
            f_out.write(f_in.read())
    
    # 4. Atomic swap (old DB → backup, new → active)
    import shutil
    shutil.move('/data/planwatch.sqlite3', '/data/planwatch.sqlite3.old')
    shutil.move(db_path, '/data/planwatch.sqlite3')
    
    # 5. Verify
    conn = sqlite3.connect('/data/planwatch.sqlite3')
    count = conn.execute("SELECT COUNT(*) FROM normalized_listings").fetchone()[0]
    
    logger.info(f"Restored backup: {count} listings")
    return 200, {"status": "restored", "listings": count}
```

### 3. Render API (Read-Only)

**What:** Dashboard reads from synced database

```python
# dashboard.py - Main API

@app.route('/api/local-listings', methods=['GET'])
def get_listings():
    """Return cached listings from database"""
    
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    
    conn = get_db()
    
    # Get listings (already deduped + normalized)
    listings = conn.execute("""
        SELECT 
            id, title, price, area_sqm, city, address_text,
            rooms, image_url, source, url,
            last_seen_at, score
        FROM normalized_listings
        ORDER BY score DESC, last_seen_at DESC
        LIMIT ? OFFSET ?
    """, (limit, (page - 1) * limit)).fetchall()
    
    total = conn.execute(
        "SELECT COUNT(*) FROM normalized_listings"
    ).fetchone()[0]
    
    return 200, {
        "listings": [dict(l) for l in listings],
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit,
        "cached": True,  # ← All from DB, no external calls
    }

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Return aggregated statistics"""
    
    conn = get_db()
    
    stats = {
        "total_listings": conn.execute(
            "SELECT COUNT(*) FROM normalized_listings"
        ).fetchone()[0],
        
        "by_city": dict(conn.execute("""
            SELECT city, COUNT(*) FROM normalized_listings
            GROUP BY city ORDER BY COUNT(*) DESC LIMIT 10
        """).fetchall()),
        
        "by_source": dict(conn.execute("""
            SELECT source, COUNT(*) FROM normalized_listings
            GROUP BY source
        """).fetchall()),
        
        "price_range": {
            "min": conn.execute(
                "SELECT MIN(price) FROM normalized_listings"
            ).fetchone()[0],
            "max": conn.execute(
                "SELECT MAX(price) FROM normalized_listings"
            ).fetchone()[0],
            "avg": conn.execute(
                "SELECT AVG(price) FROM normalized_listings"
            ).fetchone()[0],
        },
        
        "last_updated": conn.execute(
            "SELECT MAX(last_seen_at) FROM normalized_listings"
        ).fetchone()[0],
    }
    
    return 200, stats
```

### 4. Cloudflare (Edge Layer)

**Setup:**

```
1. Point your domain to Cloudflare nameservers
2. Create CNAME: planwatch.onrender.com
3. Enable Cloudflare features:
   - Page Rules: Cache Everything on /api/stats
   - Cache Rules: Browser TTL = 1 hour
   - Rate Limiting: 100 req/minute per IP
   - WAF: Enable bot protection
```

**Benefits:**

- ✅ DDoS protection (free)
- ✅ CSS/JS caching at edge
- ✅ Faster delivery worldwide
- ✅ Automatic HTTPS
- ✅ DNS management

---

## Scraper Requirements

### Unified Scraper Interface

Each scraper MUST return this exact format:

```python
class SourceScraper:
    """Base class all scrapers inherit from"""
    
    def harvest(self) -> List[RawListing]:
        """
        Returns list of listings from this source.
        
        Return format:
        [
            {
                "external_id": "yad2_123456",      # Unique ID from source
                "title": "דירה 3.5 חדרים",          # Full Hebrew title
                "description": "תיאור מלא של הנכס...", # Full description
                "price": 2500000,                   # Price in NIS
                "price_currency": "NIS",
                "area_sqm": 90.5,                   # Area in square meters
                "rooms": 3.5,
                "bathrooms": 2,
                
                "address": "רחוב העצמאות 10, תל אביב", # Full address
                "city": "תל אביב",
                "neighborhood": "רמת אביב",
                "street": "רחוב העצמאות",
                "house_number": "10",
                
                "floor": "2",                       # Floor number
                "total_floors": "5",                # Building height
                "parking": True,
                "parking_type": "private",          # private, public, street
                "balcony": True,
                "elevator": True,
                "condition": "renovated",           # new, renovated, needs_work
                "year_built": 1985,
                
                "lat": 32.0853,                     # WGS84 coordinates
                "lon": 34.7818,
                
                "property_type": "apartment",       # apartment, house, commercial
                "agent_name": "יוסי כהן",
                "agency_name": "קומס",
                "phone": "03-1234567",
                "email": "agent@agency.co.il",
                
                "url": "https://yad2.co.il/...",
                "image_url": "https://...",
                "images": ["https://...", "https://..."],
                
                "posted_at": "2026-08-17T10:30:00Z",  # ISO timestamp
                "updated_at": "2026-08-17T10:30:00Z",
                
                "raw_text": "Full HTML/text as returned by source",
            }
        ]
        """
        pass
```

### Yad2 Scraper

**Sources:**
1. Public API: `https://www.yad2.co.il/realestate/api/listings/search`
2. Premium: Playwright browser automation

**Implementation:**

```python
class Yad2Scraper(SourceScraper):
    
    def harvest(self):
        listings = []
        
        # API-based scraping
        for city in CITIES:
            for page in range(1, 100):  # Paginate until empty
                response = self._fetch_api(city, page)
                if not response['results']:
                    break
                
                for item in response['results']:
                    listing = self._parse_yad2_api(item)
                    listings.append(listing)
        
        # Premium listings (if browser auth available)
        if self.has_browser_session:
            premium = self._fetch_premium_listings()
            listings.extend(premium)
        
        return listings
    
    def _fetch_api(self, city, page):
        """Fetch from Yad2 public API"""
        url = 'https://www.yad2.co.il/realestate/api/listings/search'
        params = {
            'type': 2,              # 2=sale
            'area': city,
            'page': page,
            'pageSize': 100,
            'sortBy': 'lastUpdate',
            'withPhotos': True
        }
        
        response = requests.get(url, params=params, timeout=10)
        return response.json()
    
    def _parse_yad2_api(self, item):
        """Parse API response to canonical format"""
        
        # Extract address components
        address_parts = item['address'].split(',')
        
        return {
            "external_id": f"yad2:{item['token']}",
            "title": item['title'],
            "description": item.get('description', ''),
            "price": item['price'],
            "price_currency": "NIS",
            "area_sqm": item.get('area'),
            "rooms": item.get('rooms'),
            "bathrooms": item.get('bathrooms', 1),
            
            "address": item['address'],
            "city": item['city'],
            "street": address_parts[0].strip() if address_parts else '',
            "neighborhood": item.get('neighborhood', ''),
            
            "floor": str(item.get('floor', '')),
            "parking": 'parking' in item.get('attributes', []),
            "balcony": 'balcony' in item.get('attributes', []),
            "condition": self._map_condition(item.get('condition')),
            "year_built": item.get('yearBuilt'),
            
            "lat": item.get('lat'),
            "lon": item.get('lon'),
            
            "property_type": item.get('propertyType', 'apartment'),
            "agent_name": item.get('agent', {}).get('name', ''),
            "agency_name": item.get('agent', {}).get('agency', ''),
            "phone": item.get('agent', {}).get('phone', ''),
            
            "url": f"https://www.yad2.co.il/... {item['token']}",
            "image_url": item.get('imageUrl'),
            "images": item.get('images', []),
            
            "posted_at": item.get('postedDate'),
            "updated_at": item.get('lastUpdate'),
            
            "raw_text": str(item),
        }
    
    def _fetch_premium_listings(self):
        """Use Playwright to fetch full listing details"""
        from playwright.sync_api import sync_playwright
        
        listings = []
        
        with sync_playwright() as p:
            # Use persistent context (browser session survives restarts)
            context = p.chromium.launch_persistent_context(
                user_data_dir='./data/yad2-profile',
                headless=True
            )
            
            page = context.new_page()
            page.goto('https://www.yad2.co.il/realestate/')
            
            # Wait for listings to load
            page.wait_for_selector('[data-listing]')
            
            # Extract additional details not available in API
            listings_data = page.evaluate("""
                () => {
                    return Array.from(document.querySelectorAll('[data-listing]'))
                        .map(el => ({
                            id: el.dataset.listing,
                            phone: el.querySelector('[data-phone]')?.textContent,
                            details: el.querySelector('.details')?.textContent,
                            images: Array.from(
                                el.querySelectorAll('img[data-src]')
                            ).map(i => i.dataset.src)
                        }));
                }
            """)
            
            context.close()
        
        return listings_data
```

### Facebook Marketplace Scraper

**Source:** Web scraping with Playwright

```python
class FacebookScraper(SourceScraper):
    
    def harvest(self):
        """Scrape Facebook Marketplace"""
        from playwright.sync_api import sync_playwright
        
        listings = []
        
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir='./data/facebook-profile',
                headless=True
            )
            
            page = context.new_page()
            
            # Search for property listings
            search_url = (
                'https://www.facebook.com/marketplace/category/housing/'
                f'?query=מכירות בתים&priceFilterMin=400000&priceFilterMax=5000000'
            )
            page.goto(search_url)
            
            # Handle infinite scroll
            previous_height = 0
            while True:
                # Scroll and load more listings
                page.evaluate('window.scrollBy(0, window.innerHeight)')
                time.sleep(2)  # Wait for new listings to load
                
                new_height = page.evaluate('document.body.scrollHeight')
                if new_height == previous_height:
                    break  # No more listings
                
                previous_height = new_height
            
            # Extract all listings
            listings_html = page.evaluate("""
                () => {
                    return Array.from(
                        document.querySelectorAll('[role="article"]')
                    ).map(article => {
                        const link = article.querySelector('a[href*="/marketplace/item"]');
                        const price = article.querySelector('[data-price]')?.textContent;
                        const title = article.querySelector('h3')?.textContent;
                        const image = article.querySelector('img')?.src;
                        
                        return {
                            url: link?.href,
                            title: title,
                            price: price,
                            image: image,
                            seller_name: article.querySelector('[data-seller]')?.textContent,
                            description: article.querySelector('[data-description]')?.textContent
                        };
                    });
                }
            """)
            
            for item in listings_html:
                listing = self._parse_facebook_listing(item)
                listings.append(listing)
            
            context.close()
        
        return listings
    
    def _parse_facebook_listing(self, item):
        """Parse Facebook listing to canonical format"""
        
        # Extract listing ID from URL
        listing_id = self._extract_id_from_url(item['url'])
        
        # Parse price from text
        price = self._parse_price(item.get('price', ''))
        
        # Extract city from description or title
        city = self._extract_city(item.get('description', '') + ' ' + item.get('title', ''))
        
        return {
            "external_id": f"facebook:{listing_id}",
            "title": item.get('title', ''),
            "description": item.get('description', ''),
            "price": price,
            "price_currency": "NIS",
            
            "city": city,
            "address": item.get('description', ''),  # Extract from description
            
            "property_type": "apartment",  # Infer from title/description
            "agent_name": item.get('seller_name', ''),
            
            "url": item.get('url'),
            "image_url": item.get('image'),
            
            "posted_at": None,  # Facebook doesn't show post date
            "updated_at": datetime.now().isoformat(),
            
            "raw_text": str(item),
        }
```

### ONMAP Scraper

**Source:** API `https://onmap.co.il/api/`

```python
class ONMAPScraper(SourceScraper):
    
    def harvest(self):
        """Fetch from ONMAP API"""
        listings = []
        
        for city in CITIES:
            page = 1
            while True:
                response = self._fetch_onmap_page(city, page)
                
                if not response.get('results'):
                    break
                
                for item in response['results']:
                    listing = self._parse_onmap_api(item)
                    listings.append(listing)
                
                if page >= response.get('pageCount', 1):
                    break
                
                page += 1
        
        return listings
    
    def _fetch_onmap_page(self, city, page):
        """Fetch single page from ONMAP"""
        url = 'https://onmap.co.il/api/realestate/search'
        params = {
            'type': 'sale',
            'region': city,
            'page': page,
            'pageSize': 50,
            'sortBy': 'date_modified'
        }
        
        response = requests.get(url, params=params, timeout=10)
        return response.json()
    
    def _parse_onmap_api(self, item):
        """Parse ONMAP API response"""
        
        return {
            "external_id": f"onmap:{item['id']}",
            "title": item.get('title', ''),
            "description": item.get('description', ''),
            "price": item.get('price'),
            "price_currency": "NIS",
            "area_sqm": item.get('area'),
            "rooms": item.get('rooms'),
            
            "address": f"{item.get('street', '')}, {item.get('city', '')}",
            "city": item.get('city', ''),
            "street": item.get('street', ''),
            
            "floor": str(item.get('floor', '')),
            "parking": item.get('parking', False),
            "condition": item.get('condition', 'unknown'),
            
            "lat": item.get('lat'),
            "lon": item.get('lon'),
            
            "property_type": item.get('propertyType', 'apartment'),
            "agent_name": item.get('seller', {}).get('name', ''),
            "agency_name": item.get('seller', {}).get('agency', ''),
            "phone": item.get('seller', {}).get('phone', ''),
            
            "url": item.get('url'),
            "image_url": item.get('images', [None])[0],
            "images": item.get('images', []),
            
            "posted_at": item.get('listedDate'),
            "updated_at": item.get('modifiedDate'),
            
            "raw_text": str(item),
        }
```

### Madlan Scraper (מדלן)

**TBD - Needs Madlan API documentation**

### Ad.Co.Il Scraper

**TBD - Needs Ad.Co.Il scraping strategy**

### Como Scraper (קומו)

**TBD - Needs Como API documentation**

---

## Migration Plan

### Phase 1: Setup (Week 1)
- [ ] Create local scraper orchestrator
- [ ] Implement deduplication logic
- [ ] Setup encryption for sync
- [ ] Test sync to Render

### Phase 2: Scraper Development (Week 2-3)
- [ ] Complete Yad2 scraper (both API + Premium)
- [ ] Complete Facebook scraper
- [ ] Complete ONMAP scraper
- [ ] Add Madlan/Ad/Como scrapers

### Phase 3: Integration (Week 4)
- [ ] Full end-to-end test (local → Render)
- [ ] Setup automated daily runs
- [ ] Setup Cloudflare caching
- [ ] Monitor + optimize

### Phase 4: Launch (Week 5)
- [ ] Go live on Render
- [ ] Monitor for issues
- [ ] Collect user feedback
- [ ] Optimize based on usage

---

## Summary: Optimal Stack

### Minimum Cost Maximum Efficiency
- **Local Machine**: Run all scrapers (free, daily)
- **Render**: API server + storage ($12/mo)
- **Cloudflare**: CDN + security (free)
- **GitHub**: Repository (free)
- **Total**: **$12/month** for full production system

### Architecture Principles
✅ Data sovereignty (your database, not cloud)
✅ Zero external API calls (all cached)
✅ Deduplication at source
✅ Encryption in transit
✅ One-way sync (local → cloud)
✅ Read-only on cloud (safe)
✅ Full data control + compliance

### Benefits Over Current
- 30% cheaper ($17 → $12/mo)
- 10x faster (Cloudflare caching)
- More reliable (local + cloud backup)
- Better data quality (dedup + validation)
- Full data control (not tied to providers)
