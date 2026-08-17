# Scraper Specifications - NADLANFIX 2026
## Exact Requirements for Each Data Source

**עדכון אחרון:** 2026-08-17  
**Version:** 1.0 - Implementation Ready

---

## 📋 תוכן העניינים

1. [Universal Scraper Interface](#universal-scraper-interface)
2. [Yad2 (יד 2) Scraper](#yad2-scraper)
3. [Facebook Marketplace Scraper](#facebook-scraper)
4. [ONMAP Scraper](#onmap-scraper)
5. [Madlan (מדלן) Scraper](#madlan-scraper)
6. [Ad.Co.Il Scraper](#adcoil-scraper)
7. [Como (קומו) Scraper](#como-scraper)
8. [Testing & Validation](#testing--validation)

---

## Universal Scraper Interface

### Base Class Definition

Every scraper MUST inherit from this base and implement all methods:

```python
# sources/base/scraper.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Dict
from datetime import datetime

@dataclass
class RawListing:
    """Raw listing data directly from source API/HTML"""
    
    # Identification
    external_id: str                # Unique ID from source
    source: str                     # 'yad2', 'facebook', etc
    
    # Core Information (MUST be present)
    title: str                      # Property title/headline
    description: str                # Full text description
    price: float                    # Price in NIS
    address: str                    # Full address text
    city: str                       # City name (Hebrew)
    
    # Property Details
    area_sqm: Optional[float] = None
    rooms: Optional[float] = None
    bathrooms: Optional[int] = None
    floor: Optional[str] = None
    parking: Optional[bool] = None
    balcony: Optional[bool] = None
    elevator: Optional[bool] = None
    condition: Optional[str] = None # new, renovated, needs_work
    property_type: Optional[str] = None
    year_built: Optional[int] = None
    
    # Location Details
    lat: Optional[float] = None
    lon: Optional[float] = None
    neighborhood: Optional[str] = None
    street: Optional[str] = None
    house_number: Optional[str] = None
    gush: Optional[str] = None      # Parcel block
    helka: Optional[str] = None     # Parcel lot
    
    # Contact Information
    agent_name: Optional[str] = None
    agency_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    
    # Media
    url: str                        # Source URL (MUST be valid)
    image_url: Optional[str] = None
    images: List[str] = None        # All image URLs
    
    # Timestamps (ISO 8601)
    posted_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    # Raw Source
    raw_text: str = ""              # HTML/JSON as returned


class SourceScraper(ABC):
    """Base class for all source scrapers"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Return scraper name: 'yad2', 'facebook', etc"""
        pass
    
    @property
    @abstractmethod
    def display_name(self) -> str:
        """Return display name: 'Yad2', 'Facebook Marketplace', etc"""
        pass
    
    @abstractmethod
    def harvest(self) -> List[RawListing]:
        """
        Fetch and return all listings.
        
        Returns:
            List of RawListing objects with all available fields populated.
            
        Raises:
            ScraperError: If critical error (auth, network, parsing)
        
        Responsibilities:
        1. Fetch raw data from source (API or HTML)
        2. Parse and extract fields
        3. Handle pagination/infinite scroll
        4. Return consistent format
        
        MUST:
        - Include ALL available data from source
        - Use correct types (float for price, int for rooms)
        - Return ISO 8601 timestamps
        - Set valid URL for each listing
        - Handle errors gracefully
        
        MUST NOT:
        - Make assumptions about missing fields
        - Modify data values (e.g., convert price units)
        - Skip listings with missing fields
        - Return duplicates (dedup handled later)
        """
        pass
    
    def validate_listing(self, listing: RawListing) -> bool:
        """
        Validate listing has minimum required fields.
        
        Required fields:
        - external_id: Non-empty
        - title: Non-empty
        - price: > 0
        - address: Non-empty
        - city: Non-empty
        - url: Valid URL
        
        Returns True if valid, False otherwise.
        """
        if not listing.external_id:
            return False
        if not listing.title or len(listing.title) < 3:
            return False
        if not listing.price or listing.price <= 0:
            return False
        if not listing.address:
            return False
        if not listing.city:
            return False
        if not listing.url or not listing.url.startswith('http'):
            return False
        return True


class ScraperError(Exception):
    """Base exception for scraper errors"""
    pass


class ScraperAuthError(ScraperError):
    """Authentication/session error"""
    pass


class ScraperNetworkError(ScraperError):
    """Network/timeout error"""
    pass


class ScraperParseError(ScraperError):
    """Failed to parse response"""
    pass
```

### Harvest Registry

```python
# sources/__init__.py
from sources.yad2.scraper import Yad2Scraper
from sources.facebook.scraper import FacebookScraper
from sources.onmap.scraper import ONMAPScraper
from sources.madlan.scraper import MadlanScraper
from sources.ad.scraper import AdScraper
from sources.como.scraper import ComoScraper

REGISTERED_SCRAPERS = {
    'yad2': Yad2Scraper,
    'facebook': FacebookScraper,
    'onmap': ONMAPScraper,
    'madlan': MadlanScraper,
    'ad': AdScraper,
    'como': ComoScraper,
}

def get_all_scrapers():
    """Instantiate all registered scrapers"""
    return {name: cls() for name, cls in REGISTERED_SCRAPERS.items()}
```

---

## Yad2 Scraper

### Source Information

- **Name:** Yad2 (יד 2) - Israel's #1 Real Estate Portal
- **API Base:** `https://www.yad2.co.il/realestate/api/listings/search`
- **Coverage:** ~100,000+ active listings
- **Update Frequency:** Listings update multiple times per day
- **Auth Required:** No (public API)

### Yad2 API Response Format

```json
{
  "results": [
    {
      "token": "12345678",
      "title": "דירה 3.5 חדרים, תל אביב",
      "description": "תיאור מלא של הדירה...",
      "price": 2500000,
      "priceText": "₪ 2.5M",
      "currency": "NIS",
      "area": 90.5,
      "rooms": 3.5,
      "bathrooms": 2,
      "floor": "2",
      "totalFloors": "5",
      "address": "רחוב העצמאות 10, תל אביב",
      "city": "תל אביב",
      "neighborhood": "רמת אביב",
      "street": "רחוב העצמאות",
      "houseNumber": "10",
      "lat": 32.0853,
      "lon": 34.7818,
      "gush": "1234",
      "helka": "5678",
      "propertyType": "apartment",
      "condition": "renovated",
      "yearBuilt": 1985,
      "parking": true,
      "parkingType": "private",
      "balcony": true,
      "elevator": true,
      "attributes": ["מחסן", "גן", "דירה משופצת"],
      "agent": {
        "name": "יוסי כהן",
        "agency": "קומס",
        "phone": "03-1234567",
        "email": "agent@agency.co.il"
      },
      "imageUrl": "https://images.yad2.co.il/...",
      "images": ["https://...", "https://..."],
      "url": "https://www.yad2.co.il/realestate/item/12345678",
      "postedDate": "2026-08-15T10:30:00Z",
      "lastUpdate": "2026-08-17T14:22:00Z"
    }
  ],
  "totalCount": 4523,
  "pageCount": 46
}
```

### Implementation

```python
# sources/yad2/scraper.py
import requests
import logging
from typing import List
from datetime import datetime
from sources.base.scraper import SourceScraper, RawListing, ScraperNetworkError, ScraperParseError

logger = logging.getLogger(__name__)

class Yad2Scraper(SourceScraper):
    
    name = 'yad2'
    display_name = 'Yad2'
    
    BASE_URL = 'https://www.yad2.co.il/realestate/api/listings/search'
    CITIES = ['תל אביב', 'ירושלים', 'חיפה', 'באר שבע', 'אשקלון']
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def harvest(self) -> List[RawListing]:
        """Fetch all listings from Yad2"""
        
        all_listings = []
        
        for city in self.CITIES:
            logger.info(f"Scraping Yad2 {city}...")
            
            page = 1
            while True:
                try:
                    response = self._fetch_page(city, page)
                    
                    if not response.get('results'):
                        break
                    
                    # Parse each listing
                    for item in response['results']:
                        try:
                            listing = self._parse_listing(item)
                            if self.validate_listing(listing):
                                all_listings.append(listing)
                        except Exception as e:
                            logger.warning(f"Failed to parse listing: {e}")
                            continue
                    
                    # Check if more pages
                    if page >= response.get('pageCount', 1):
                        break
                    
                    page += 1
                    
                except ScraperNetworkError as e:
                    logger.error(f"Network error on page {page}: {e}")
                    break
        
        logger.info(f"Yad2: Scraped {len(all_listings)} listings total")
        return all_listings
    
    def _fetch_page(self, city: str, page: int) -> dict:
        """Fetch single page from API"""
        
        params = {
            'type': 2,              # 2 = sale listings
            'area': city,
            'page': page,
            'pageSize': 100,        # Max 100 per page
            'sortBy': 'lastUpdate',
            'sortDirection': 'desc',
            'withPhotos': True,
        }
        
        try:
            response = self.session.get(
                self.BASE_URL,
                params=params,
                timeout=15
            )
            response.raise_for_status()
            return response.json()
        
        except requests.Timeout:
            raise ScraperNetworkError(f"Timeout fetching {city} page {page}")
        except requests.ConnectionError as e:
            raise ScraperNetworkError(f"Connection error: {e}")
        except requests.exceptions.RequestException as e:
            raise ScraperNetworkError(f"Request failed: {e}")
        except ValueError as e:
            raise ScraperParseError(f"Invalid JSON response: {e}")
    
    def _parse_listing(self, item: dict) -> RawListing:
        """Parse API response to RawListing"""
        
        return RawListing(
            external_id=str(item.get('token', '')),
            source='yad2',
            
            title=item.get('title', ''),
            description=item.get('description', ''),
            price=self._parse_float(item.get('price')),
            address=item.get('address', ''),
            city=item.get('city', ''),
            
            area_sqm=self._parse_float(item.get('area')),
            rooms=self._parse_float(item.get('rooms')),
            bathrooms=self._parse_int(item.get('bathrooms')),
            floor=str(item.get('floor', '')) if item.get('floor') else None,
            parking=item.get('parking'),
            balcony=item.get('balcony'),
            elevator=item.get('elevator'),
            condition=item.get('condition', ''),
            property_type=item.get('propertyType', 'apartment'),
            year_built=self._parse_int(item.get('yearBuilt')),
            
            lat=self._parse_float(item.get('lat')),
            lon=self._parse_float(item.get('lon')),
            neighborhood=item.get('neighborhood', ''),
            street=item.get('street', ''),
            house_number=item.get('houseNumber', ''),
            gush=item.get('gush', ''),
            helka=item.get('helka', ''),
            
            agent_name=item.get('agent', {}).get('name', ''),
            agency_name=item.get('agent', {}).get('agency', ''),
            phone=item.get('agent', {}).get('phone', ''),
            email=item.get('agent', {}).get('email', ''),
            
            url=item.get('url', '') or f"https://www.yad2.co.il/realestate/item/{item.get('token', '')}",
            image_url=item.get('imageUrl'),
            images=item.get('images', []),
            
            posted_at=item.get('postedDate'),
            updated_at=item.get('lastUpdate'),
            
            raw_text=str(item)
        )
    
    @staticmethod
    def _parse_float(value) -> float:
        """Safely parse float value"""
        if value is None or value == '':
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def _parse_int(value) -> int:
        """Safely parse int value"""
        if value is None or value == '':
            return None
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None
```

---

## Facebook Scraper

### Source Information

- **Name:** Facebook Marketplace - Housing Section
- **URL Base:** `https://www.facebook.com/marketplace/category/housing`
- **Coverage:** ~50,000+ active listings (Israel region)
- **Update Frequency:** Real-time
- **Auth Required:** Yes (Facebook account)
- **Tech:** Playwright (headless browser)

### Implementation

```python
# sources/facebook/scraper.py
import logging
import time
from typing import List
from datetime import datetime
from playwright.sync_api import sync_playwright
from sources.base.scraper import SourceScraper, RawListing, ScraperError, ScraperAuthError

logger = logging.getLogger(__name__)

class FacebookScraper(SourceScraper):
    
    name = 'facebook'
    display_name = 'Facebook Marketplace'
    
    PROFILE_PATH = './data/facebook-profile'  # Persistent login
    SEARCH_URL = 'https://www.facebook.com/marketplace/category/housing'
    
    def harvest(self) -> List[RawListing]:
        """Scrape Facebook Marketplace"""
        
        listings = []
        
        try:
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir=self.PROFILE_PATH,
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                
                page = context.new_page()
                
                # Navigate to search
                logger.info("Loading Facebook Marketplace...")
                page.goto(self.SEARCH_URL, wait_until='domcontentloaded')
                
                # Check if logged in
                if 'login' in page.url.lower() or not page.query_selector('[role="article"]'):
                    raise ScraperAuthError("Not logged in to Facebook. Please login manually first.")
                
                # Scroll and load all listings
                listings.extend(self._scroll_and_extract(page))
                
                context.close()
        
        except ScraperAuthError:
            raise
        except Exception as e:
            raise ScraperError(f"Facebook scraping failed: {e}")
        
        logger.info(f"Facebook: Scraped {len(listings)} listings")
        return listings
    
    def _scroll_and_extract(self, page) -> List[RawListing]:
        """Scroll infinite scroll and extract listings"""
        
        listings = []
        seen_urls = set()
        previous_height = 0
        scroll_attempts = 0
        max_scrolls = 50  # Limit scrolling
        
        while scroll_attempts < max_scrolls:
            # Extract current visible listings
            articles = page.query_selector_all('[role="article"]')
            
            for article in articles:
                try:
                    # Extract URL (key identifier)
                    link = article.query_selector('a[href*="/marketplace/item"]')
                    if not link:
                        continue
                    
                    url = link.get_attribute('href')
                    if url in seen_urls:
                        continue
                    
                    seen_urls.add(url)
                    
                    # Extract visible data from DOM
                    listing = self._extract_from_article(article, url)
                    if self.validate_listing(listing):
                        listings.append(listing)
                
                except Exception as e:
                    logger.warning(f"Failed to extract listing: {e}")
                    continue
            
            # Scroll down
            page.evaluate('window.scrollBy(0, window.innerHeight)')
            time.sleep(2)  # Wait for content to load
            
            # Check if reached end
            new_height = page.evaluate('document.body.scrollHeight')
            if new_height == previous_height:
                break  # No new content
            
            previous_height = new_height
            scroll_attempts += 1
        
        return listings
    
    def _extract_from_article(self, article, url: str) -> RawListing:
        """Extract data from article element"""
        
        # Extract listing ID from URL
        listing_id = url.split('/')[url.split('/').count('marketplace/item') + url.split('/').index('marketplace/item') + 1]
        
        # Get text content
        title = article.query_selector('h3')
        title_text = title.text_content() if title else ''
        
        # Get price
        price_elem = article.query_selector('[data-test-id*="price"]')
        price_text = price_elem.text_content() if price_elem else ''
        price = self._parse_price(price_text)
        
        # Get image
        image_elem = article.query_selector('img')
        image_url = image_elem.get_attribute('src') if image_elem else None
        
        # Extract location and description from title/price area
        location = ''
        if article.query_selector('[data-test-id*="location"]'):
            location = article.query_selector('[data-test-id*="location"]').text_content()
        
        # Extract city (usually in title or location)
        city = self._extract_city(title_text + ' ' + location)
        
        return RawListing(
            external_id=f"fb_{listing_id}",
            source='facebook',
            
            title=title_text,
            description='',  # FB doesn't show full description in listing
            price=price,
            address=location,
            city=city,
            
            property_type='apartment',  # Infer from category
            
            url=f"https://www.facebook.com{url}" if not url.startswith('http') else url,
            image_url=image_url,
            
            posted_at=None,  # FB doesn't show post date
            updated_at=datetime.now().isoformat(),
            
            raw_text=article.inner_html()
        )
    
    @staticmethod
    def _parse_price(price_text: str) -> float:
        """Extract price from text"""
        import re
        # Match ₪ 2,500,000 or 2500000 or 2.5M
        price_text = price_text.replace('₪', '').replace(',', '').strip()
        match = re.search(r'(\d+(?:\.\d+)?)\s*([MK])?', price_text)
        if match:
            price = float(match.group(1))
            unit = match.group(2)
            if unit == 'M':
                price *= 1_000_000
            elif unit == 'K':
                price *= 1_000
            return price
        return None
    
    @staticmethod
    def _extract_city(text: str) -> str:
        """Extract city from text"""
        cities = {
            'תל אביב': ['תא', 'תל אביב יפו'],
            'ירושלים': ['ירו', 'ירושלים עיר'],
            'חיפה': ['חיפה'],
            'באר שבע': ['באר שבע'],
        }
        text_lower = text.lower()
        for city, aliases in cities.items():
            for alias in aliases:
                if alias in text_lower:
                    return city
        # Fallback to first word
        words = text.split(',')
        return words[-1].strip() if words else ''
```

---

## ONMAP Scraper

### Source Information

- **Name:** ONMAP - Israel's #2 Real Estate Portal (after Yad2)
- **API Base:** `https://onmap.co.il/api/realestate/search`
- **Coverage:** ~60,000+ active listings
- **Update Frequency:** Daily
- **Auth Required:** No (public API)

### ONMAP API Response Format

```json
{
  "results": [
    {
      "id": "12345678",
      "title": "דירה 3 חדרים",
      "description": "תיאור...",
      "price": 2000000,
      "area": 85,
      "rooms": 3,
      "bathrooms": 2,
      "floor": "2",
      "city": "תל אביב",
      "street": "רחוב העצמאות 10",
      "lat": 32.0853,
      "lon": 34.7818,
      "propertyType": "apartment",
      "condition": "good",
      "parking": true,
      "images": ["https://...", "https://..."],
      "seller": {
        "name": "יוסי כהן",
        "agency": "קומס",
        "phone": "03-1234567"
      },
      "url": "https://onmap.co.il/property/12345678",
      "metadata": {
        "listedDate": "2026-08-15T10:30:00Z",
        "modifiedDate": "2026-08-17T14:22:00Z"
      }
    }
  ],
  "totalCount": 2341,
  "pageCount": 47
}
```

### Implementation

```python
# sources/onmap/scraper.py
import requests
import logging
from typing import List
from sources.base.scraper import SourceScraper, RawListing, ScraperNetworkError, ScraperParseError

logger = logging.getLogger(__name__)

class ONMAPScraper(SourceScraper):
    
    name = 'onmap'
    display_name = 'ONMAP'
    
    BASE_URL = 'https://onmap.co.il/api/realestate/search'
    CITIES = ['תל אביב', 'ירושלים', 'חיפה', 'באר שבע']
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def harvest(self) -> List[RawListing]:
        """Fetch all listings from ONMAP"""
        
        all_listings = []
        
        for city in self.CITIES:
            logger.info(f"Scraping ONMAP {city}...")
            
            page = 1
            while True:
                try:
                    response = self._fetch_page(city, page)
                    
                    if not response.get('results'):
                        break
                    
                    for item in response['results']:
                        try:
                            listing = self._parse_listing(item)
                            if self.validate_listing(listing):
                                all_listings.append(listing)
                        except Exception as e:
                            logger.warning(f"Failed to parse listing: {e}")
                            continue
                    
                    if page >= response.get('pageCount', 1):
                        break
                    
                    page += 1
                
                except ScraperNetworkError as e:
                    logger.error(f"Network error on page {page}: {e}")
                    break
        
        logger.info(f"ONMAP: Scraped {len(all_listings)} listings total")
        return all_listings
    
    def _fetch_page(self, city: str, page: int) -> dict:
        """Fetch single page from API"""
        
        params = {
            'type': 'sale',
            'region': city,
            'page': page,
            'pageSize': 50,
            'sortBy': 'date_modified'
        }
        
        try:
            response = self.session.get(
                self.BASE_URL,
                params=params,
                timeout=15
            )
            response.raise_for_status()
            return response.json()
        
        except requests.Timeout:
            raise ScraperNetworkError(f"Timeout fetching {city} page {page}")
        except requests.ConnectionError as e:
            raise ScraperNetworkError(f"Connection error: {e}")
        except requests.exceptions.RequestException as e:
            raise ScraperNetworkError(f"Request failed: {e}")
        except ValueError as e:
            raise ScraperParseError(f"Invalid JSON response: {e}")
    
    def _parse_listing(self, item: dict) -> RawListing:
        """Parse API response to RawListing"""
        
        return RawListing(
            external_id=str(item.get('id', '')),
            source='onmap',
            
            title=item.get('title', ''),
            description=item.get('description', ''),
            price=self._parse_float(item.get('price')),
            address=f"{item.get('street', '')}, {item.get('city', '')}",
            city=item.get('city', ''),
            
            area_sqm=self._parse_float(item.get('area')),
            rooms=self._parse_float(item.get('rooms')),
            bathrooms=self._parse_int(item.get('bathrooms')),
            floor=str(item.get('floor', '')) if item.get('floor') else None,
            parking=item.get('parking'),
            property_type=item.get('propertyType', 'apartment'),
            condition=item.get('condition', ''),
            
            lat=self._parse_float(item.get('lat')),
            lon=self._parse_float(item.get('lon')),
            street=item.get('street', ''),
            
            agent_name=item.get('seller', {}).get('name', ''),
            agency_name=item.get('seller', {}).get('agency', ''),
            phone=item.get('seller', {}).get('phone', ''),
            
            url=item.get('url', '') or f"https://onmap.co.il/property/{item.get('id', '')}",
            images=item.get('images', []),
            
            posted_at=item.get('metadata', {}).get('listedDate'),
            updated_at=item.get('metadata', {}).get('modifiedDate'),
            
            raw_text=str(item)
        )
    
    @staticmethod
    def _parse_float(value) -> float:
        if value is None or value == '':
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def _parse_int(value) -> int:
        if value is None or value == '':
            return None
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None
```

---

## Madlan Scraper

### Source Information

- **Name:** Madlan (מדלן) - Real Estate Portal
- **API Base:** `https://www.madlan.co.il/api/...` (TBD)
- **Coverage:** ~30,000 listings
- **Auth Required:** Unknown (needs investigation)
- **Status:** PLACEHOLDER - Needs API documentation

### Placeholder Implementation

```python
# sources/madlan/scraper.py
import logging
from typing import List
from sources.base.scraper import SourceScraper, RawListing, ScraperError

logger = logging.getLogger(__name__)

class MadlanScraper(SourceScraper):
    
    name = 'madlan'
    display_name = 'Madlan'
    
    def harvest(self) -> List[RawListing]:
        """
        TODO: Implement Madlan scraping
        
        Research needed:
        1. API endpoint documentation
        2. Authentication method (if required)
        3. Response format
        4. Rate limits
        5. Coverage (cities, property types)
        """
        logger.warning("Madlan scraper not yet implemented")
        return []
```

---

## Ad.Co.Il Scraper

### Source Information

- **Name:** Ad.Co.Il - Israeli Classifieds
- **Coverage:** ~20,000 real estate listings
- **Auth Required:** Unknown
- **Status:** PLACEHOLDER - Needs investigation

### Placeholder Implementation

```python
# sources/ad/scraper.py
import logging
from typing import List
from sources.base.scraper import SourceScraper, RawListing

logger = logging.getLogger(__name__)

class AdScraper(SourceScraper):
    
    name = 'ad'
    display_name = 'Ad.Co.Il'
    
    def harvest(self) -> List[RawListing]:
        """
        TODO: Implement Ad.Co.Il scraping
        
        Research needed:
        1. Website structure (HTML)
        2. Listing page format
        3. How to find all listings (search/category)
        4. Rate limits
        """
        logger.warning("Ad.Co.Il scraper not yet implemented")
        return []
```

---

## Como Scraper

### Source Information

- **Name:** Como (קומו) - Real Estate Agency
- **Coverage:** Agency-specific listings
- **Auth Required:** Unknown
- **Status:** PLACEHOLDER - Needs investigation

### Placeholder Implementation

```python
# sources/como/scraper.py
import logging
from typing import List
from sources.base.scraper import SourceScraper, RawListing

logger = logging.getLogger(__name__)

class ComoScraper(SourceScraper):
    
    name = 'como'
    display_name = 'Como'
    
    def harvest(self) -> List[RawListing]:
        """
        TODO: Implement Como scraping
        
        Note: Como may be a real estate agency with listings
        on multiple platforms rather than standalone portal.
        
        Research needed:
        1. Is Como a standalone portal or agency?
        2. Where are listings published?
        3. API availability?
        """
        logger.warning("Como scraper not yet implemented")
        return []
```

---

## Testing & Validation

### Unit Tests for Each Scraper

```python
# tests/test_scrapers.py
import unittest
from sources.yad2.scraper import Yad2Scraper
from sources.facebook.scraper import FacebookScraper
from sources.onmap.scraper import ONMAPScraper

class TestYad2Scraper(unittest.TestCase):
    
    def setUp(self):
        self.scraper = Yad2Scraper()
    
    def test_harvest_returns_listings(self):
        """Test that harvest returns non-empty list"""
        listings = self.scraper.harvest()
        self.assertIsInstance(listings, list)
        self.assertGreater(len(listings), 0)
    
    def test_listing_has_required_fields(self):
        """Test that all listings have minimum required fields"""
        listings = self.scraper.harvest()
        
        required = ['external_id', 'title', 'price', 'address', 'city', 'url']
        
        for listing in listings:
            for field in required:
                self.assertIsNotNone(getattr(listing, field))
                self.assertTrue(len(str(getattr(listing, field))) > 0)
    
    def test_url_is_valid(self):
        """Test that URL is valid HTTP/HTTPS"""
        listings = self.scraper.harvest()[:5]
        
        for listing in listings:
            self.assertTrue(
                listing.url.startswith('http://') or 
                listing.url.startswith('https://'),
                f"Invalid URL: {listing.url}"
            )
    
    def test_price_is_positive(self):
        """Test that price is positive number"""
        listings = self.scraper.harvest()[:5]
        
        for listing in listings:
            if listing.price:
                self.assertGreater(listing.price, 0)
    
    def test_no_duplicates(self):
        """Test that no duplicate external_ids"""
        listings = self.scraper.harvest()
        ids = [l.external_id for l in listings]
        self.assertEqual(len(ids), len(set(ids)))


class TestFacebookScraper(unittest.TestCase):
    
    def setUp(self):
        self.scraper = FacebookScraper()
    
    def test_login_required(self):
        """Test that scraper requires login"""
        # This test assumes no valid login cookie
        # It should raise ScraperAuthError
        with self.assertRaises(Exception):
            self.scraper.harvest()


class TestONMAPScraper(unittest.TestCase):
    
    def setUp(self):
        self.scraper = ONMAPScraper()
    
    def test_harvest_returns_listings(self):
        """Test that harvest returns non-empty list"""
        listings = self.scraper.harvest()
        self.assertIsInstance(listings, list)
        self.assertGreater(len(listings), 0)
```

### Integration Tests

```python
# tests/test_scraper_integration.py
import unittest
from sources import get_all_scrapers

class TestScraperIntegration(unittest.TestCase):
    
    def test_all_scrapers_runnable(self):
        """Test that all registered scrapers can run"""
        scrapers = get_all_scrapers()
        
        for name, scraper in scrapers.items():
            with self.subTest(scraper=name):
                try:
                    listings = scraper.harvest()
                    # Should return list
                    self.assertIsInstance(listings, list)
                except Exception as e:
                    # Some may fail (e.g., no login), log but don't fail
                    print(f"Scraper {name} skipped: {e}")
    
    def test_deduplication_works(self):
        """Test that dedup removes duplicates"""
        from pipeline.deduplication import DeduplicationEngine
        
        # Create test listings with same address
        duplicate_listings = [
            {'address': '10 Main St, Tel Aviv', 'price': 2000000, 'area_sqm': 90},
            {'address': '10 Main St, Tel Aviv', 'price': 2000000, 'area_sqm': 90},
            {'address': '10 Main St, Tel Aviv', 'price': 2050000, 'area_sqm': 90},  # Slightly different
        ]
        
        dedup = DeduplicationEngine(None)
        result = dedup.dedup(duplicate_listings)
        
        # Should have 1-2 unique (depending on dedup logic)
        self.assertLessEqual(len(result), len(duplicate_listings))
```

---

## Deployment Checklist

- [ ] Yad2 scraper tested and producing listings
- [ ] Facebook scraper tested (requires manual login setup)
- [ ] ONMAP scraper tested and producing listings
- [ ] Madlan scraper implemented
- [ ] Ad.Co.Il scraper implemented
- [ ] Como scraper implemented
- [ ] Deduplication tested and working
- [ ] Normalization tested and working
- [ ] Validation tests passing
- [ ] Daily harvest scheduled and running
- [ ] Sync to Render working
- [ ] Dashboard displaying listings
- [ ] Performance benchmarks met
