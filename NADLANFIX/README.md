# NADLANFIX - פלטפורמת נדל"ן חכמה

מערכת אינטגרטיבית לניהול וניתוח נתוני נדל"ן בישראל, המשלבת מקורות מרובים (יד 2, Facebook Marketplace, על המפה) בממשק אחד אחיד.

## 🎯 סקירה כללית

NADLANFIX היא פלטפורמת נדל"ן הבנויה על בסיס Python ו-Node.js, המאפשרת:

- **איסוף נתונים** מיד 2, Facebook Marketplace, והפורטל "על המפה"
- **נורמליזציה** של נתונים מ-מקורות שונים לפורמט אחיד
- **ניתוח שוק** עם מדדי מחיר, יום-בשוק, וזיהוי הזדמנויות
- **ממשק ניהול** (Dashboard) לצפייה וניהול הנתונים
- **API מובנה** לאינטגרציה עם מערכות חיצוניות

## 🏗️ ארכיטקטורה

```
NADLANFIX/
├── sources/                    # Source adapters (unified interface)
│   ├── base/                   # Base adapter, models, exceptions
│   ├── yad2/                   # Yad2 source adapter
│   ├── facebook/               # Facebook Marketplace source adapter
│   ├── onmap/                  # ONMAP source adapter
│   └── shared/                 # Shared utilities
├── jobs/                       # Scheduled jobs
│   ├── scheduler.py            # Unified scheduler with retry/health
│   ├── daily_ingest.py         # Daily orchestration entry point
│   ├── health.py               # Health monitoring and alerts
│   └── validation.py           # Data validation layer
├── pipeline/                   # Processing pipeline (future)
│   ├── fetch/                  # Data fetching
│   ├── normalize/              # Normalization
│   ├── dedupe/                 # Deduplication
│   ├── score/                  # Scoring
│   ├── enrich/                 # Enrichment
│   └── persist/                # Persistence
├── storage/                    # Storage layer (future)
│   ├── sqlite_local/           # Local SQLite store
│   ├── firestore_sync/         # Firestore sync
│   └── snapshots/              # Data snapshots
├── dashboard.py                # Main API server
├── db.py                       # Database schema
├── facebook_feed.py            # Facebook Marketplace harvester
├── yad2_feed.py                # Yad2 harvester
├── onmap_feed.py               # ONMAP harvester
├── webapp/                     # Public web application
│   ├── index.html
│   ├── app.js
│   └── css/
├── public-site/                # User-facing website
│   ├── index.html              # Login
│   ├── search.html             # Property search
│   ├── categories.html         # Category selection
│   ├── transactions.html       # All transactions
│   ├── opportunities.html      # Top 15 opportunities
│   ├── map.html                # Interactive map
│   ├── planning.html           # Planning & GIS
│   ├── css/style.css
│   └── js/
└── tests/                      # Test suite
```

## זרימת נתונים

```
Source (Yad2/Facebook/ONMAP)
    ↓ fetch()
Raw listings
    ↓ normalize()
Canonical listings
    ↓ validate()
Validated listings
    ↓ persist()
SQLite (local store)
    ↓ export()
Firestore (operational DB)
    ↓ API
Clients (webapp, public-site)
```

## התחלה מהירה

### דרישות

- Python 3.10+
- Node.js 18+ (for backend)
- Playwright browsers
- SQLite 3

### התקנה

```bash
# Clone repository
git clone <repo-url>
cd NADLANFIX

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Initialize database
python -c "import db; db.ensure_schema(db.get_conn())"

# Run scheduler
python jobs/daily_ingest.py run --all

# Start dashboard
python dashboard.py

# Start backend (in another terminal)
cd backend
npm install
npm run start
```

### משתני סביבה

```bash
# Database
PLANWATCH_DB=./data/planwatch.sqlite3

# Facebook Marketplace
PLANWATCH_FB_STORAGE_STATE=./data/facebook_storage_state.json
PLANWATCH_FB_QUERY=מכירות בתים
PLANWATCH_FB_MIN_PRICE=400000
PLANWATCH_FB_TARGET=500

# Telegram/WhatsApp bots (optional)
PLANWATCH_TELEGRAM_TOKEN=your_token
WHATSAPP_AUTH_DIR=./data/whatsapp-auth
```

## שימוש

### scheduler.py

```bash
# Run all sources
python jobs/daily_ingest.py run --all

# Run specific sources
python jobs/daily_ingest.py run --sources yad2,facebook

# Check status
python jobs/daily_ingest.py status

# Check health
python jobs/daily_ingest.py health
```

### dashboard.py

```bash
# Start dashboard server
python dashboard.py

# API endpoints
GET  /api/health                    # Health check
GET  /api/combined-sale-listings    # All listings (deduped)
GET  /api/deals                     # Top opportunities
GET  /api/facebook/harvest          # Facebook harvest status
POST /api/facebook/harvest          # Start Facebook harvest
GET  /api/localities                # City list
```

### Source Adapters

```python
from sources.base.adapter import BaseSourceAdapter
from sources.yad2.adapter import Yad2SourceAdapter
from sources.facebook.adapter import FacebookSourceAdapter

# Use adapter
adapter = FacebookSourceAdapter(db_path="./data/planwatch.sqlite3")
conn = adapter.get_conn()
result = adapter.run(conn, target=500)
print(result.status, result.rows_new)
```

## מקורות נתונים

| מקור | סטטוס | תיאור |
|------|-------|-------|
| יד 2 | ✅ פעיל | מודעות מכירה והשכרה |
| Facebook Marketplace | ✅ פעיל | מודעות נדל"ן מ-Facebook |
| על המפה | ✅ פעיל | מודעות מכירה והשכרה |
| הלמ"ס | ✅ פעיל | מדדי שוק |
| בנק ישראל | ✅ פעיל | ריבית ומשכנתאות |
| תכנון עירוני | ✅ פעיל | תכניות בנייה |

## פיתוח

### הוספת מקור חדש

1. צור adapter ב-`sources/<source_name>/adapter.py`
2. ירש מ-`BaseSourceAdapter`
3. implements: `source_name`, `display_name`, `ensure_schema`, `fetch`, `normalize`, `count`
4. הוסף ל-`jobs/daily_ingest.py`

### מבנה Adapter

```python
class MySourceAdapter(BaseSourceAdapter):
    @property
    def source_name(self) -> str:
        return "my_source"

    def ensure_schema(self, conn):
        # Create tables
        pass

    def fetch(self, conn, target, **kwargs):
        # Fetch raw data
        pass

    def normalize(self, raw):
        # Convert to canonical format
        pass

    def count(self, conn):
        # Return counts
        pass
```

## בדיקות

```bash
# Run tests
pytest tests/

# Run specific test
pytest tests/test_facebook_feed.py
```

## פריסה

ראה `DEPLOY.md` לפרטי פריסה ב-Render ו-Docker.

## רישיון

פרויקט פנימי - כל הזכויות שמורות.
