# מפת הפרויקט - NADLANFIX

**עדכון אחרון:** 2026-08-17  
**סטטוס:** 🟢 פעיל (Local-first + Firestore + Netlify)

---

## 📍 כל המידע על מיקום הקבצים והמודולים

### 🎯 Core Application Files

| קובץ | תיאור | סטטוס |
|------|--------|--------|
| `dashboard.py` | ה-API הראשי של כל המערכת (Port 8000) | ✅ פעיל |
| `db.py` | Schema של בסיס הנתונים SQLite + normalized_listings | ✅ פעיל |
| `requirements.txt` | תלויות Python (כולל google-cloud-firestore) | ✅ עדכני |
| `run-all.ps1` | Script להרצת כל השירותים (Windows) | ✅ פעיל |

### 📂 מבנה הספריות

```
NADLANFIX/
│
├── 🌐 DATA SOURCES
│   └── sources/
│       ├── base/
│       │   ├── adapter.py       # BaseSourceAdapter interface
│       │   ├── models.py        # Canonical listing models
│       │   └── exceptions.py    # Source exceptions
│       ├── yad2/
│       │   └── adapter.py       # Yad2 implementation → normalized_listings
│       ├── facebook/
│       │   └── adapter.py       # Facebook Marketplace impl. → normalized_listings
│       ├── onmap/
│       │   └── adapter.py       # ONMAP implementation → normalized_listings
│       └── shared/
│           └── utils.py         # Shared utilities
│
├── ⚙️ SCHEDULED JOBS
│   └── jobs/
│       ├── scheduler.py         # Unified scheduler (retry, health) + Firestore sync
│       ├── daily_ingest.py      # Daily ingestion orchestrator
│       ├── health.py            # Health monitoring
│       ├── validation.py        # Data validation
│       └── __init__.py
│
├── 💾 STORAGE LAYER
│   └── storage/
│       ├── firestore_sync.py    # Firestore batch sync (active)
│       ├── active_db.py         # Firestore-first DB abstraction
│       └── schema_versioning.py # Schema versioning
│
├── 🗄️ DATABASE
│   └── db.py                    # Single source of truth for all tables
│       ├── plans, plan_status_log, subscriptions, notifications
│       ├── yad2_listings, facebook_listings, onmap_listings
│       ├── normalized_listings  # Unified listings table
│       ├── listing_history, price_history
│       ├── source_health, sync_queue, sync_history
│       └── schema_versions, app_metadata, snapshots, backup_history
│
├── 🌍 PUBLIC WEBSITE (Production)
│   └── public-site/
│       ├── index.html           # Homepage / Login
│       ├── search.html          # Property search
│       ├── map.html             # Interactive map
│       ├── opportunities.html   # Top opportunities
│       ├── transactions.html    # Listings feed
│       ├── planning.html        # Planning info
│       ├── categories.html      # Category select
│       ├── css/style.css
│       ├── js/
│       │   ├── api.js           # Frontend API client
│       │   └── app.js           # Frontend logic
│       └── netlify.toml         # Netlify deployment config
│
├── 📊 ADMIN DASHBOARD
│   └── dashboard.py             # Single-file admin dashboard + API
│
├── 🛠️ BACKEND (Node.js)
│   └── backend/
│       ├── src/
│       │   ├── server.js        # Express API (Yad2 feed proxy)
│       │   ├── scraper.js       # Playwright browser management
│       │   ├── feed.js          # Yad2 feed fetcher
│       │   ├── login.js         # Yad2 Radware login helper
│       │   └── facebook-login.js # Facebook session helper
│       └── package.json
│
├── 🤖 BOTS
│   └── bots/
│       ├── telegram/            # Telegram bot
│       └── whatsapp/            # WhatsApp bot
│
├── 🧪 TESTS
│   └── tests/
│       └── smoke_tests.py
│
├── 📁 DATA
│   └── data/
│       ├── planwatch.sqlite3    # Main database
│       └── facebook_storage_state.json  # Facebook session
│
└── 📦 ARCHIVE
    ├── frontend-webapp/         # Old webapp (replaced by public-site)
    ├── deploy-render/           # Old Render/Docker deployment
    └── storage-sqlite-local/    # Old NormalizedStore (merged into db.py)
```
│
├── 🖥️ NODE.JS BACKEND
│   └── backend/
│       ├── src/
│       │   ├── server.js        # Express server & proxy
│       │   ├── feed.js          # Yad2 feed scraper
│       │   ├── cache.js         # Caching layer
│       │   ├── login.js         # Facebook login
│       │   └── localListings.js # Local listings
│       ├── package.json
│       └── ui-v3.mjs            # UI utilities
│
├── ⚙️ CONFIGURATION
│   └── config/
│       ├── environment.py       # Environment settings
│       ├── production.py        # Production hardening
│       └── __init__.py
│
├── 🗂️ LEGACY CODE
│   └── legacy/
│       ├── archive/             # Old implementations
│       ├── debug/               # Debug utilities
│       ├── probes/              # Health probes
│       ├── tools/               # Utility scripts
│       └── __init__.py
│
├── 📈 OBSERVABILITY
│   └── observability/
│       ├── metrics.py           # Metrics collection
│       └── __init__.py
│
├── 🤖 BOT INTEGRATIONS
│   └── bots/
│       ├── telegram/            # Telegram bot
│       ├── whatsapp/            # WhatsApp bot
│       └── shared/              # Shared bot utilities
│
├── 📚 TESTS
│   └── tests/
│       ├── smoke_tests.py       # Core functionality tests
│       └── test_production_readiness.py
│
├── 🐳 DEPLOYMENT
│   ├── Dockerfile               # Container specification
│   ├── Dockerfile.cron          # Cron container
│   ├── docker-entrypoint.sh     # Container startup
│   ├── render.yaml              # Render deployment config
│   └── litestream.yml           # DB backup replication
│
├── 💾 DATA
│   └── data/
│       └── planwatch.sqlite3    # Main SQLite database
│
└── 📄 DOCUMENTATION
    ├── README.md                # This file
    ├── PROJECT_MAP.md           # Project structure (this file)
    ├── INSTALLATION_GUIDE.md    # Installation instructions
    ├── CURRENT_STATUS.md        # Current status & issues
    └── DATA_COLLECTION_WITHOUT_AI.md
```

---

## 🔌 API Endpoints

### Health & Status

| Endpoint | Method | Auth | תיאור |
|----------|--------|------|-------|
| `/api/health` | GET | ❌ | Server health check |
| `/api/bootstrap` | GET | ❌ | Initial app bootstrap data |

### Listings

| Endpoint | Method | Auth | תיאור |
|----------|--------|------|-------|
| `/api/local-listings` | GET | ✅ | Local normalized listings |
| `/api/listings` | GET | ✅ | All listings (search/filter) |
| `/api/deals` | GET | ✅ | Top opportunities |

### CRM

| Endpoint | Method | Auth | תיאור |
|----------|--------|------|-------|
| `/api/crm/leads` | GET | ✅ | List leads |
| `/api/crm/leads` | POST | ✅ | Create lead |
| `/api/crm/tasks` | GET | ✅ | List tasks |
| `/api/crm/pipelines` | GET | ✅ | Pipeline stages |
| `/api/crm/stats` | GET | ✅ | CRM statistics |

### Data Sources

| Endpoint | Method | Auth | תיאור |
|----------|--------|------|-------|
| `/api/yad2/harvest` | GET | ✅ | Yad2 harvest status |
| `/api/yad2/harvest` | POST | ✅ | Start Yad2 harvest |
| `/api/facebook/harvest` | GET | ✅ | Facebook harvest status |
| `/api/facebook/harvest` | POST | ✅ | Start Facebook harvest |

### Admin

| Endpoint | Method | Auth | תיאור |
|----------|--------|------|-------|
| `/admin/dashboard` | GET | ✅ | Admin dashboard |

---

## 📊 Database Schema

### Tables - Listings

- **`normalized_listings`** - Canonical listings from all sources
- **`yad2_listings`** - Raw Yad2 listings
- **`facebook_listings`** - Raw Facebook listings
- **`onmap_listings`** - Raw ONMAP listings
- **`yad2_price_history`** - Price tracking history

### Tables - CRM

- **`users`** - User accounts
- **`sessions`** - Session tokens
- **`crm_leads`** - Leads management
- **`crm_tasks`** - Tasks management
- **`crm_notes`** - Comments & notes
- **`crm_pipeline_stages`** - Pipeline configuration

### Tables - Planning & Reference

- **`plans`** - Building plans (תוכניות)
- **`muni_parcels`** - Municipal parcels
- **`muni_permits`** - Building permits
- **`muni_buildings`** - Building records
- **`muni_dangerous`** - Dangerous buildings

### Tables - System

- **`scheduler_runs`** - Job execution history
- **`scheduler_health`** - Source health status
- **`property_catalog`** - Image catalog
- **`settings`** - Key-value configuration

---

## 🔀 Data Flow Diagram

```
┌─────────────────────────────────────────────────────────┐
│                 External Sources                        │
│  (Yad2, Facebook Marketplace, ONMAP)                   │
└───────────────────┬─────────────────────────────────────┘
                    │
        ┌───────────┼───────────┐
        ↓           ↓           ↓
    ┌────────┐  ┌────────┐  ┌────────┐
    │ Yad2   │  │Facebook│  │ ONMAP  │
    │Adapter │  │Adapter │  │Adapter │
    └────┬───┘  └────┬───┘  └────┬───┘
        │           │           │
        └───────────┼───────────┘
                    ↓
         ┌──────────────────────┐
         │  fetch() - Get Raw   │
         │     Listings         │
         └──────────┬───────────┘
                    ↓
         ┌──────────────────────┐
         │ normalize() - Map    │
         │ to Canonical Format  │
         └──────────┬───────────┘
                    ↓
         ┌──────────────────────┐
         │ validate() - Check   │
         │  Data Quality        │
         └──────────┬───────────┘
                    ↓
         ┌──────────────────────┐
         │ persist() - Write    │
         │ to SQLite Database   │
         └──────────┬───────────┘
                    ↓
     ┌──────────────────────────────┐
     │   SQLite Database            │
     │ (planwatch.sqlite3)          │
     │                              │
     │ ├── normalized_listings      │
     │ ├── yad2_listings            │
     │ ├── facebook_listings        │
     │ └── onmap_listings           │
     └──────────┬───────────────────┘
                ↓
     ┌──────────────────────────────┐
      │   Dashboard API              │
      │   (dashboard.py:8000)        │
      │                              │
      │ ├── /api/local-listings      │
      │ ├── /api/crm/leads           │
      │ └── /api/deals               │
      └──────────┬───────────────────┘
                 ↓
      ┌──────────────────────────────┐
      │   Client Applications        │
      │                              │
      │ ├── Public Website           │
      │ │   (public-site/ → Netlify) │
      │ │                            │
      │ └── Admin Dashboard          │
      │     (dashboard.py)           │
      └──────────────────────────────┘
```

---

## 🚀 Key Features by Module

### sources/ - Data Ingestion

- ✅ Unified adapter interface for all sources
- ✅ Automatic schema creation
- ✅ Fetch, normalize, validate, persist pipeline
- ✅ Cross-source deduplication
- ✅ Error handling & retry logic

### jobs/ - Orchestration

- ✅ Unified scheduler with configurable intervals
- ✅ Per-source health monitoring
- ✅ Retry logic with exponential backoff
- ✅ Run history tracking
- ✅ Stale data alerts

### storage/ - Data Access

- ✅ Local SQLite optimized queries
- ✅ Firestore sync (optional)
- ✅ Data snapshots for backup
- ✅ Connection pooling
- ✅ WAL mode for concurrency

### app/auth/ - Security

- ✅ Role-based access control (RBAC)
- ✅ Session token management
- ✅ Password hashing (scrypt)
- ✅ Protected endpoints

### app/crm/ - Lead Management

- ✅ Lead CRUD operations
- ✅ Task management
- ✅ Pipeline stage tracking
- ✅ Notes & comments
- ✅ Owner management

---

## 🔧 Configuration Points

### Environment Variables

```bash
# Database
PLANWATCH_DB=./data/planwatch.sqlite3

# Server
PLANWATCH_DEPLOY=local|render|docker
PYTHONUNBUFFERED=1

# Security
PLANWATCH_BASIC_AUTH=username:password

# Bots
PLANWATCH_TELEGRAM_TOKEN=...
WHATSAPP_AUTH_DIR=./data/whatsapp-auth
WHATSAPP_QR_PATH=./data/whatsapp-auth/qr.png
```

### Dashboard Configuration

```python
# dashboard.py - Main server configuration
HOST = '0.0.0.0'
PORT = 8000
ALLOWED_HOSTS = ['*']
```

### Scheduler Configuration

```python
# jobs/scheduler.py
max_retries = 3                    # Max retry attempts
retry_delay_seconds = 60           # Delay between retries
job_timeout_seconds = 3600         # 1 hour timeout
stale_threshold_hours = 24         # Consider data stale
```

---

## 📡 Integration Points

### External APIs

- **Yad2** - Real estate listings feed
- **Facebook Graph API** - Marketplace listings
- **ONMAP** - Property portal
- **Google Maps API** - Geocoding
- **Bank of Israel API** - Interest rates
- **Municipality APIs** - Building permits

### Deployment Platforms

- **Local** - Primary development and production environment
- **Netlify** - Static site hosting for public-site frontend
- **Firebase** - Firestore for operational data sync

---

## 🧪 Testing

### Smoke Tests

```bash
python -m pytest tests/smoke_tests.py -q
```

Tests cover:
- Source adapter instantiation
- Canonical model creation
- Scheduler migration
- Validation layer
- Dedupe layer
- Normalized store
- Health monitoring

### Production Readiness

```bash
python -m pytest tests/test_production_readiness.py -q
```

### Coverage

```bash
python -m pytest --cov=app --cov=sources tests/
```

---

## 📝 Development Notes

### Adding a New Source

1. Create `sources/<source_name>/adapter.py`
2. Inherit from `BaseSourceAdapter`
3. Implement: `source_name`, `display_name`, `fetch`, `normalize`, `count`
4. Add to `jobs/daily_ingest.py`

### Adding a CRM Route

1. Create handler in `app/crm/api/routes.py`
2. Register in `dashboard.py` handler
3. Add RBAC check with `@require_role('admin')`

### Database Migrations

1. Add new schema to `db.py` or source adapter
2. Run migrations via `ensure_schema()`
3. Test with smoke tests

---

## 🔐 Security Checklist

- [x] RBAC implemented
- [x] HTTPS ready (Netlify handles certificates)
- [x] Password hashing (scrypt)
- [x] Session token rotation
- [x] Basic auth for admin endpoints
- [x] CORS configured
- [x] SQL injection protection (parameterized queries)
- [ ] Rate limiting (TODO)
- [ ] DDoS protection (Netlify handles)

---

## 📞 Quick Support

- **Server won't start**: Check `PLANWATCH_DB` path and port availability
- **No listings showing**: Run `python -m jobs.daily_ingest run --all`
- **Authentication issues**: Check `PLANWATCH_BASIC_AUTH` environment variable
- **Database corruption**: Restore from local backup
