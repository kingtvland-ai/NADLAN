# מדריך התקנה מלא - NADLANFIX

**עדכון אחרון:** 2026-08-17  
**תקפות:** עבור Python 3.10+ ו-Node.js 18+

---

## 📋 תוכן העניינים

1. [התקנה מקומית](#התקנה-מקומית)
2. [התקנה on Render](#התקנה-על-render)
3. [התקנה on Netlify](#התקנה-על-netlify)
4. [סביבת Docker](#סביבת-docker)
5. [תצורה וכיוונון](#תצורה-וכיוונון)
6. [בדיקות](#בדיקות)
7. [פתרון בעיות](#פתרון-בעיות)

---

## התקנה מקומית

### דרישות מוקדמות

- **Python 3.10 or higher**
  ```bash
  python --version
  # Python 3.10.x or higher
  ```

- **Node.js 18 or higher**
  ```bash
  node --version
  # v18.x or higher
  npm --version
  # 9.x or higher
  ```

- **Git**
  ```bash
  git --version
  # git version 2.30+
  ```

- **SQLite 3**
  ```bash
  sqlite3 --version
  # 3.20+
  ```

### שלב 1: Clone Repository

```bash
# Clone the repository
git clone <repo-url>
cd NADLANFIX

# Or if you have SSH configured
git clone git@github.com:<username>/NADLANFIX.git
cd NADLANFIX
```

### שלב 2: Setup Python Environment

```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Windows:
venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Verify activation (should show venv in prompt)
```

### שלב 3: Install Python Dependencies

```bash
# Update pip first
pip install --upgrade pip

# Install requirements
pip install -r requirements.txt

# Verify installation
python -c "import requests; import shapely; print('✓ Core dependencies OK')"
```

### שלב 4: Install Node Dependencies

```bash
# Navigate to backend directory
cd backend

# Install Node dependencies
npm install

# Verify Playwright browsers
npx playwright install chromium

# Navigate back to root
cd ..
```

### שלב 5: Initialize Database

```bash
# Create data directory
mkdir -p data

# Initialize database schema
python -c "from db import get_conn, ensure_schema; conn = get_conn(); ensure_schema(conn); print('✓ Database initialized')"

# Verify database created
ls -la data/planwatch.sqlite3
```

### שלב 6: Run the System

#### Option A: All Services Together (Windows)

```powershell
# Make sure you're in the NADLANFIX root directory
# Run the master script
.\run-all.ps1

# This will open 3 PowerShell windows:
# 1. Python dashboard.py (Port 8000)
# 2. Node backend (Port 4000)
# 3. Frontend dev server (Port 3000, if needed)
```

#### Option B: Manually in Separate Terminals

**Terminal 1 - Python Backend:**
```bash
python dashboard.py --port 8000 --no-browser
# Output: Serving on http://0.0.0.0:8000
```

**Terminal 2 - Node Backend (optional):**
```bash
cd backend
npm start
# Output: Listening on http://0.0.0.0:4000
```

**Terminal 3 - Run Ingestion (optional):**
```bash
python -m jobs.daily_ingest run --all
# Starts: yad2, facebook, onmap harvesters
```

### שלב 7: Access the System

```
Dashboard:  http://localhost:8000
API:        http://localhost:8000/api/
Health:     http://localhost:8000/api/health
```

---

## התקנה על Render

### דרישות מוקדמות

1. **Render Account** - רישום חינם ב- [render.com](https://render.com)
2. **GitHub Repository** - Push code to GitHub
3. **GitHub Token** - For Render to access private repos (if needed)

### שלב 1: Prepare Repository

```bash
# Ensure all files are committed
git add .
git commit -m "Ready for Render deployment"
git push origin main
```

### שלב 2: Create Render Web Service

1. Login to [Render Dashboard](https://dashboard.render.com)
2. Click **New +** → **Web Service**
3. Select your GitHub repository
4. Configure:

```
Name:                    planwatch
Runtime:                 Docker
Build Command:           (Leave empty - uses Dockerfile)
Start Command:           (Leave empty - uses docker-entrypoint.sh)
Plan:                    Standard ($12/month, has persistent disk)
Region:                  Frankfurt (closest to Israel)
```

### שלב 3: Add Environment Variables

In Render dashboard, add these environment variables:

```
PLANWATCH_DB                 = /data/planwatch.sqlite3
PLANWATCH_DEPLOY             = render
PLANWATCH_BASIC_AUTH         = username:password (set securely)
PYTHONUNBUFFERED             = 1
PLANWATCH_TELEGRAM_TOKEN     = (optional - from @BotFather)
WHATSAPP_AUTH_DIR            = /data/whatsapp-auth
WHATSAPP_QR_PATH             = /data/whatsapp-auth/qr.png
```

### שלב 4: Add Persistent Disk

1. In Render dashboard, go to **Disks**
2. Click **Add Disk**
3. Configure:

```
Name:                    planwatch-data
Mount Path:              /data
Size:                    5GB (sufficient for growth)
```

### שלב 5: Create Cron Job

1. Click **New +** → **Cron Job**
2. Configure:

```
Name:                    planwatch-scheduled-refresh
Build Command:           (Leave empty)
Start Command:           See docker-entrypoint.sh
Schedule:                0 */12 * * * (every 12 hours)
Environment Variables:
  PLANWATCH_URL        = https://planwatch.onrender.com
  PLANWATCH_BASIC_AUTH = (same as web service)
```

### שלב 6: Deploy

1. Click **Deploy**
2. Watch build logs for errors
3. Once deployed, visit: `https://planwatch.onrender.com`

---

## התקנה על Netlify

### For Public Website Only

Netlify hosts the static public site separately from the API.

### שלב 1: Prepare Static Files

```bash
# Copy public-site files
cp -r public-site ./dist

# Or if building from source
cd public-site
npm install
npm run build
cd ..
```

### שלב 2: Connect to Netlify

1. Login to [Netlify](https://netlify.com)
2. Click **Add new site** → **Import an existing project**
3. Select your GitHub repository
4. Configure:

```
Base directory:              public-site/
Build command:               (leave empty - no build needed)
Publish directory:           public-site/
```

### שלב 3: Configure Environment

In Netlify UI, set **Build & deploy** → **Build environment variables**:

```
REACT_APP_API_BASE           = https://planwatch.onrender.com
REACT_APP_ENVIRONMENT        = production
```

### שלב 4: Deploy

Click **Deploy** - Netlify will:
1. Clone repository
2. Copy public-site files
3. Deploy to CDN
4. Provide URL like: `https://nadlanfix.netlify.app`

---

## סביבת Docker

### Build Locally

```bash
# Build image
docker build -t nadlanfix:latest .

# Run container
docker run -p 8000:8000 -p 4000:4000 \
  -v $(pwd)/data:/data \
  -e PLANWATCH_DB=/data/planwatch.sqlite3 \
  nadlanfix:latest

# Container will start:
# - Python on 8000
# - Node on 4000
# - Scheduler in background
```

### Run Docker Compose (if available)

```bash
docker-compose up --build
```

### Verify Container

```bash
# Check running containers
docker ps

# View logs
docker logs <container-id>

# Execute command in container
docker exec -it <container-id> bash
```

---

## תצורה וכיוונון

### Environment Variables

Create `.env` file in root directory:

```bash
# Database
PLANWATCH_DB=./data/planwatch.sqlite3

# Server
PLANWATCH_DEPLOY=local
PYTHONUNBUFFERED=1
HOST=0.0.0.0
PORT=8000

# Security (optional - leave empty for no auth)
PLANWATCH_BASIC_AUTH=

# External Services (optional)
PLANWATCH_TELEGRAM_TOKEN=
WHATSAPP_AUTH_DIR=./data/whatsapp-auth
WHATSAPP_QR_PATH=./data/whatsapp-auth/qr.png

# Facebook Marketplace (optional)
PLANWATCH_FB_STORAGE_STATE=./data/facebook_storage_state.json
PLANWATCH_FB_QUERY=מכירות בתים
PLANWATCH_FB_MIN_PRICE=400000
PLANWATCH_FB_TARGET=500
```

### Configuration Files

#### `config/environment.py`
Main configuration file - Edit to change behavior:

```python
DEPLOY_MODE = 'local'        # local, render, docker
DEBUG = True
LOGGING_LEVEL = 'INFO'
ENABLE_BOTS = False          # Telegram/WhatsApp bots
```

#### `config/production.py`
Production-specific settings:

```python
RETENTION_DAYS = 90          # Keep data for 90 days
ENABLE_COMPRESSION = True
ENABLE_CACHING = True
```

#### `jobs/scheduler.py`
Job scheduling configuration:

```python
INTERVAL_HOURS = {
    'yad2': 24,              # Run every 24 hours
    'facebook': 24,
    'onmap': 24,
}
```

---

## בדיקות

### Run Smoke Tests

```bash
# Quick smoke tests
python -m pytest tests/smoke_tests.py -v

# Output should show:
# test_source_adapters ✓
# test_canonical_model ✓
# test_validation ✓
# test_normalized_store ✓
# etc.
```

### Run Production Tests

```bash
# Production readiness checks
python -m pytest tests/test_production_readiness.py -v

# Checks:
# - API endpoints respond
# - Database accessible
# - Scheduler works
# - Auth required endpoints
```

### Manual API Testing

```bash
# Health check (no auth needed)
curl http://localhost:8000/api/health

# List listings (auth required)
curl -H "Authorization: Basic $(echo -n 'user:pass' | base64)" \
  http://localhost:8000/api/local-listings

# CRM endpoints
curl -H "Authorization: Basic ..." \
  http://localhost:8000/api/crm/leads
```

### Database Verification

```bash
# Check tables exist
python -c "
import sqlite3
conn = sqlite3.connect('data/planwatch.sqlite3')
tables = conn.execute(
    'SELECT name FROM sqlite_master WHERE type=\"table\"'
).fetchall()
for t in tables:
    print(t[0])
"

# Check row counts
python -c "
import sqlite3
conn = sqlite3.connect('data/planwatch.sqlite3')
for table in ['normalized_listings', 'yad2_listings', 'plans']:
    c = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    print(f'{table}: {c} rows')
"
```

---

## פתרון בעיות

### Problem: Port Already in Use

**Error:** `Address already in use`

**Solution:**
```bash
# Find process using port 8000
lsof -i :8000  # macOS/Linux
netstat -ano | findstr :8000  # Windows

# Kill the process
kill -9 <PID>  # macOS/Linux
taskkill /PID <PID> /F  # Windows

# Or use different port
python dashboard.py --port 8001
```

### Problem: Database Locked

**Error:** `database is locked`

**Solution:**
```bash
# SQLite can have stale locks, restart helps
rm -f data/planwatch.sqlite3-wal data/planwatch.sqlite3-shm

# Or use WAL mode cleanup
python -c "
import sqlite3
conn = sqlite3.connect('data/planwatch.sqlite3')
conn.execute('PRAGMA optimize')
conn.close()
print('Database optimized')
"
```

### Problem: Virtual Environment Not Found

**Error:** `No module named 'requests'`

**Solution:**
```bash
# Reactivate virtual environment
source venv/bin/activate  # macOS/Linux
venv\Scripts\activate     # Windows

# Reinstall requirements
pip install -r requirements.txt
```

### Problem: Node Modules Missing

**Error:** `Cannot find module 'express'`

**Solution:**
```bash
cd backend
npm install
npm ci  # Clean install from package-lock.json
cd ..
```

### Problem: Playwright Browsers Not Found

**Error:** `Browser is not installed`

**Solution:**
```bash
# Install browsers
npx playwright install chromium

# Or reinstall all
npx playwright install
```

### Problem: Empty Listings

**Error:** No listings showing in dashboard

**Solution:**
```bash
# Run ingestion manually
python -m jobs.daily_ingest run --all

# Check source status
python -c "
from jobs.daily_ingest import run_daily_ingest
result = run_daily_ingest()
print(result['summary'])
"

# Verify database has data
python -c "
import sqlite3
conn = sqlite3.connect('data/planwatch.sqlite3')
c = conn.execute('SELECT COUNT(*) FROM yad2_listings').fetchone()[0]
print(f'Yad2 listings: {c}')
"
```

### Problem: Authentication Failed

**Error:** 401 Unauthorized

**Solution:**
```bash
# Check PLANWATCH_BASIC_AUTH
echo $PLANWATCH_BASIC_AUTH

# Set credentials
export PLANWATCH_BASIC_AUTH="admin:password123"

# Test with curl
curl -u admin:password123 http://localhost:8000/api/crm/leads
```

### Problem: Render Deployment Failed

**Error:** Build fails on Render

**Solution:**
1. Check build logs in Render dashboard
2. Ensure `requirements.txt` is in root
3. Ensure `Dockerfile` is valid
4. Check environment variables are set
5. Try manual Docker build locally:

```bash
docker build -t nadlanfix:test .
docker run -p 8000:8000 nadlanfix:test
```

---

## ✅ Verification Checklist

After installation, verify everything works:

- [ ] Python environment activated
- [ ] All dependencies installed (`pip list | grep requests`)
- [ ] Database initialized (`ls -la data/planwatch.sqlite3`)
- [ ] Server starts (`python dashboard.py`)
- [ ] Health check passes (`curl http://localhost:8000/api/health`)
- [ ] Tests pass (`pytest tests/smoke_tests.py -q`)
- [ ] Can access listings (`curl http://localhost:8000/api/local-listings`)

---

## 📞 Support

If you encounter issues:

1. Check [CURRENT_STATUS.md](CURRENT_STATUS.md) for known issues
2. Run diagnostic:

```bash
python -c "
import sys
import sqlite3
import requests
print(f'Python: {sys.version}')
print(f'SQLite: {sqlite3.sqlite_version}')
print(f'Requests: {requests.__version__}')
print('✓ All imports OK')
"
```

3. Check logs: `tail -f dashboard.log`
4. Review error messages carefully - they usually point to the issue
