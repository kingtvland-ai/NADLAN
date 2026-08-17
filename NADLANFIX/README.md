# NADLANFIX - פלטפורמת נדל"ן חכמה

מערכת אינטגרטיבית לניהול וניתוח נתוני נדל"ן בישראל, המשלבת מקורות מרובים (יד 2, Facebook Marketplace, על המפה) בממשק אחד אחיד.

## 🎯 סקירה כללית

NADLANFIX היא פלטפורמת נדל"ן הבנויה על בסיס Python ו-Node.js, המאפשרת:

- **איסוף נתונים** מיד 2, Facebook Marketplace, והפורטל "על המפה"
- **נורמליזציה** של נתונים מ-מקורות שונים לפורמט אחיד (`normalized_listings`)
- **ניתוח שוק** עם מדדי מחיר, יום-בשוק, וזיהוי הזדמנויות
- **ממשק ניהול** (Dashboard) לצפייה וניהול הנתונים
- **API מובנה** לאינטגרציה עם מערכות חיצוניות
- **Firestore sync** לפריסת Frontend על Netlify

## 🏗️ ארכיטקטורה (Topology חדשה)

```
┌─────────────────────────────────────────────────────────────────┐
│                    YOUR LOCAL MACHINE                           │
│                    (Windows / Mac / Linux)                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │   Yad2       │  │  Facebook    │  │   ONMAP      │          │
│  │  Scraper     │  │  Scraper     │  │  Scraper     │          │
│  │ (Node/Playwright) │ (Playwright) │  (Python/urllib)│        │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
│         │                 │                 │                  │
│         └─────────────────┼─────────────────┘                  │
│                           ↓                                    │
│                   ┌───────────────┐                            │
│                   │  Normalize    │ (Canonical format)         │
│                   └───────┬───────┘                            │
│                           ↓                                    │
│                 ┌─────────────────────┐                       │
│                 │  Local SQLite DB    │                       │
│                 │  (planwatch.sqlite3)│                       │
│                 └────────┬────────────┘                       │
│                          │                                    │
│                          ├──→ Firestore Sync (batch upsert)   │
│                          │                                    │
│  ┌──────────────┐  ┌──────┴───────┐  ┌──────────────┐        │
│  │ Python API   │  │  Frontend    │  │   Bots       │        │
│  │ (port 8000)  │  │ (public-site)│  │ (Telegram/   │        │
│  │              │  │ → Netlify    │  │  WhatsApp)   │        │
│  └──────────────┘  └──────────────┘  └──────────────┘        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
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
SQLite (local store: normalized_listings)
    ↓ FirestoreSync
Firestore (operational DB)
    ↓ API / Netlify Functions
Clients (public-site, bots)
```

## התחלה מהירה

### דרישות

- Python 3.10+
- Node.js 18+ (for Yad2/Facebook scrapers)
- Playwright browsers
- SQLite 3
- Firebase project (for Firestore sync)

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
python -c "import db; db.get_conn()"

# Run daily ingestion
python jobs/daily_ingest.py run --all

# Start dashboard API
python dashboard.py

# Start Node backend (in another terminal, for Yad2 feed)
cd backend
npm install
npm run start
```

### משתני סביבה

```bash
# Database
PLANWATCH_DB=./data/planwatch.sqlite3

# Firebase (for Firestore sync)
GOOGLE_APPLICATION_CREDENTIALS=./data/service-account.json
FIRESTORE_PROJECT_ID=your-project-id

# Facebook Marketplace
PLANWATCH_FACEBOOK_SESSION=./data/facebook_storage_state.json

# Telegram/WhatsApp bots (optional)
PLANWATCH_TELEGRAM_TOKEN=your_token
WHATSAPP_AUTH_DIR=./data/whatsapp-auth
```

## שימוש

### Daily Ingestion

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

### Dashboard API

```bash
# Start dashboard server
python dashboard.py

# API endpoints
GET  /api/health                    # Health check
GET  /api/parcel                    # Parcel lookup (gush/helka)
GET  /api/combined-sale-listings    # All listings from normalized_listings
GET  /api/deals                     # Top opportunities
POST /api/yad2/harvest              # Start Yad2 harvest
GET  /api/yad2/harvest/status       # Yad2 harvest status
```

### Facebook Session Management

```bash
# One-time: open browser and log in to Facebook
cd backend
npm run facebook-login

# The session state is saved to data/facebook_storage_state.json
# The scraper will reuse this session for subsequent runs
```

### Source Adapters

```python
from sources.base.adapter import BaseSourceAdapter
from sources.yad2.adapter import Yad2SourceAdapter

# Use adapter
adapter = Yad2SourceAdapter(db_path="./data/planwatch.sqlite3")
conn = adapter.get_conn()
result = adapter.run(conn, target=100000)
print(result.status, result.rows_new)
```

## מבנה הפרויקט

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
├── ingestion/                  # Data ingestion layer
│   ├── feeds/                  # Feed harvesters (yad2, facebook, onmap)
│   └── normalization/          # Normalization logic
├── storage/                    # Storage layer
│   ├── firestore_sync.py       # Firestore sync
│   ├── active_db.py            # Firestore-first DB abstraction
│   └── schema_versioning.py    # Schema versioning
├── db.py                       # Database schema (single source of truth)
├── dashboard.py                # Main API server
├── backend/                    # Node.js backend (Yad2 scraper)
│   └── src/
│       ├── server.js           # Express API server
│       ├── scraper.js          # Playwright browser management
│       ├── feed.js             # Yad2 feed fetcher
│       └── login.js            # Yad2 Radware login helper
├── public-site/                # Production frontend (Netlify)
│   ├── index.html
│   ├── search.html
│   ├── categories.html
│   ├── transactions.html
│   ├── opportunities.html
│   ├── map.html
│   ├── planning.html
│   ├── css/style.css
│   └── js/
│       ├── api.js
│       └── app.js
├── archive/                    # Archived/decommissioned code
│   ├── frontend-webapp/        # Old webapp (replaced by public-site)
│   ├── deploy-render/          # Old Render/Docker deployment
│   └── storage-sqlite-local/   # Old NormalizedStore (merged into db.py)
├── bots/                       # Telegram/WhatsApp bots
├── tests/                      # Test suite
└── data/                       # Data directory
    ├── planwatch.sqlite3       # Main database
    └── facebook_storage_state.json  # Facebook session
```

## מקורות נתונים

| מקור | סטטוס | תיאור |
|------|-------|-------|
| יד 2 | ✅ פעיל | מודעות מכירה והשכרה (87K+ listings) |
| Facebook Marketplace | ✅ פעיל | מודעות נדל"ן מ-Facebook |
| על המפה | ✅ פעיל | מודעות מכירה והשכרה |
| הלמ"ס | ✅ פעיל | מדדי שוק |
| בנק ישראל | ✅ פעיל | ריבית ומשכנתאות |
| תכנון עירוני | ✅ פעיל | תכניות בנייה |

## פיתוח

### הוספת מקור חדש

1. צור adapter ב-`sources/<source_name>/adapter.py`
2. ירש מ-`BaseSourceAdapter`
3. implements: `source_name`, `display_name`, `ensure_schema`, `fetch`, `normalize`, `count`, `_persist_listing`
4. הוסף ל-`jobs/daily_ingest.py`

### מבנה Adapter

```python
class MySourceAdapter(BaseSourceAdapter):
    @property
    def source_name(self) -> str:
        return "my_source"

    def fetch(self, conn, target, **kwargs):
        # Fetch raw data from source
        # Return list of raw listing dicts
        pass

    def normalize(self, raw):
        # Convert to canonical format
        # Must include canonical_id
        pass

    def _persist_listing(self, conn, listing):
        # Write to normalized_listings table
        pass

    def count(self, conn):
        # Return count dict
        pass
```

## בדיקות

```bash
# Run tests
pytest tests/

# Run smoke tests
python tests/smoke_tests.py
```

## פריסה

ראה `DEPLOYMENT_ARCHITECTURE.md` לפרטי פריסה מקומית ו-Netlify.

## רישיון

פרויקט פנימי - כל הזכויות שמורות.
