# Firebase + GAS Implementation Guide
## Step-by-Step Setup with Code Examples

**עדכון אחרון:** 2026-08-17  
**Version:** 1.0 - Production Ready

---

## 📋 תוכן העניינים

1. [Firebase Setup](#firebase-setup)
2. [Local Sync to Firebase](#local-sync-to-firebase)
3. [Render API Updates](#render-api-updates)
4. [Google Apps Script](#google-apps-script)
5. [Security & Best Practices](#security--best-practices)
6. [Troubleshooting](#troubleshooting)

---

## Firebase Setup

### Step 1: Create Firebase Project

```bash
# 1. Go to https://console.firebase.google.com
# 2. Click "Create a project"
# 3. Name: "nadlanfix-2026"
# 4. Enable Google Analytics (optional)
# 5. Create project
```

### Step 2: Enable Firestore Database

In Firebase Console:
```
1. Left sidebar → "Build" → "Firestore Database"
2. Click "Create database"
3. Location: "eur3" (Europe)
4. Security rules: Choose "Start in production mode"
5. Create
```

### Step 3: Get Firebase Credentials

```bash
# In Firebase Console:
# 1. Project Settings (gear icon)
# 2. Service Accounts tab
# 3. Click "Generate new private key"
# 4. Save as config/firebase-config.json

# The JSON file looks like:
{
  "type": "service_account",
  "project_id": "nadlanfix-2026",
  "private_key_id": "abc123...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...",
  "client_email": "firebase-adminsdk-xyz@nadlanfix-2026.iam.gserviceaccount.com",
  "client_id": "123456789",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
  "client_x509_cert_url": "https://www.googleapis.com/..."
}
```

**SECURITY:** Never commit this file to git!

```bash
# Add to .gitignore
echo "config/firebase-config.json" >> .gitignore
echo ".env" >> .gitignore
```

### Step 4: Create Database Collections

In Firebase Console:
```
1. Click "Create collection"
2. Collection ID: "listings"
3. Click "Add document" then "Cancel" (we'll add data programmatically)

Repeat for:
- "metadata" collection
  - Documents: "sync_log", "stats"
```

---

## Local Sync to Firebase

### Step 1: Install Firebase SDK

```bash
# Add to requirements.txt
pip install firebase-admin

# Or install directly
pip install firebase-admin==6.0.0
```

### Step 2: Create Sync Module

**File:** `jobs/sync_firebase.py`

```python
import json
import logging
import time
import sqlite3
from datetime import datetime
from typing import List, Dict
import firebase_admin
from firebase_admin import credentials, db
import os

logger = logging.getLogger(__name__)

class FirebaseSyncEngine:
    """Sync listings from local SQLite to Firebase"""
    
    def __init__(self, firebase_config_path: str):
        """
        Initialize Firebase connection.
        
        Args:
            firebase_config_path: Path to firebase-config.json
        """
        if not os.path.exists(firebase_config_path):
            raise FileNotFoundError(f"Firebase config not found: {firebase_config_path}")
        
        # Initialize Firebase only once
        if not firebase_admin._apps:
            cred = credentials.Certificate(firebase_config_path)
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://nadlanfix-2026.firebaseio.com'
            })
        
        self.db = db.reference()
        self.batch_size = 500  # Firebase write limit
    
    def sync_all_listings(self, local_db_path: str) -> Dict:
        """
        Sync all listings from SQLite to Firebase.
        
        Returns:
            {
                'status': 'success' | 'error',
                'listings_synced': int,
                'timestamp': ISO 8601,
                'error': str (if error)
            }
        """
        
        try:
            logger.info("Starting Firebase sync...")
            
            # Read from local SQLite
            listings = self._read_from_sqlite(local_db_path)
            logger.info(f"Read {len(listings)} listings from SQLite")
            
            # Convert to Firebase format
            firebase_listings = self._convert_to_firebase_format(listings)
            
            # Upload in batches
            synced_count = self._upload_to_firebase(firebase_listings)
            logger.info(f"Uploaded {synced_count} listings to Firebase")
            
            # Update metadata
            self._update_metadata(synced_count)
            
            result = {
                'status': 'success',
                'listings_synced': synced_count,
                'timestamp': datetime.now().isoformat()
            }
            
            logger.info(f"✓ Firebase sync complete: {result}")
            return result
        
        except Exception as e:
            error_msg = f"Firebase sync failed: {str(e)}"
            logger.error(error_msg, exc_info=True)
            return {
                'status': 'error',
                'error': error_msg,
                'timestamp': datetime.now().isoformat()
            }
    
    def _read_from_sqlite(self, db_path: str) -> List[Dict]:
        """Read all listings from local SQLite"""
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        listings = conn.execute("""
            SELECT * FROM normalized_listings
            ORDER BY updated_at DESC
        """).fetchall()
        
        result = [dict(row) for row in listings]
        conn.close()
        
        return result
    
    def _convert_to_firebase_format(self, listings: List[Dict]) -> Dict:
        """Convert SQLite rows to Firebase document format"""
        
        firebase_data = {}
        
        for listing in listings:
            # Use external_id as Firebase document key
            doc_id = f"{listing['source']}_{listing['external_id']}"
            
            # Only keep fields we need (reduce storage)
            firebase_listing = {
                'external_id': listing['external_id'],
                'source': listing['source'],
                'title': listing['title'],
                'description': listing['description'][:500],  # Limit description
                'price': listing['price'],
                'city': listing['city'],
                'neighborhood': listing.get('neighborhood'),
                'address': listing['address'],
                'area_sqm': listing['area_sqm'],
                'rooms': listing.get('rooms'),
                'bathrooms': listing.get('bathrooms'),
                'floor': listing.get('floor'),
                'parking': listing.get('parking'),
                'condition': listing.get('condition'),
                'property_type': listing.get('property_type'),
                'url': listing['url'],
                'image_url': listing.get('image_url'),
                'lat': listing.get('lat'),
                'lon': listing.get('lon'),
                'updated_at': listing['updated_at'],
                'posted_at': listing.get('posted_at'),
                'score': listing.get('score'),
            }
            
            # Remove None values to save storage
            firebase_listing = {k: v for k, v in firebase_listing.items() if v is not None}
            
            firebase_data[doc_id] = firebase_listing
        
        return firebase_data
    
    def _upload_to_firebase(self, firebase_data: Dict) -> int:
        """Upload listings to Firebase in batches"""
        
        total = len(firebase_data)
        uploaded = 0
        
        # Convert dict to list for batch processing
        items = list(firebase_data.items())
        
        for i in range(0, len(items), self.batch_size):
            batch = items[i:i + self.batch_size]
            batch_dict = dict(batch)
            
            try:
                # Upload batch
                self.db.child('listings').update(batch_dict)
                uploaded += len(batch)
                
                logger.info(f"  Uploaded {uploaded}/{total} listings")
                
                # Rate limiting (Firebase allows ~100 writes/sec)
                time.sleep(0.1)
            
            except Exception as e:
                logger.error(f"Batch upload failed: {e}")
                raise
        
        return uploaded
    
    def _update_metadata(self, listings_count: int):
        """Update metadata in Firebase"""
        
        try:
            # Record sync log
            self.db.child('metadata').child('sync_log').push({
                'timestamp': datetime.now().isoformat(),
                'listings_count': listings_count,
                'status': 'success'
            })
            
            # Update stats
            self.db.child('metadata').child('stats').set({
                'total_listings': listings_count,
                'last_updated': datetime.now().isoformat(),
                'last_updated_utc_timestamp': int(time.time())
            })
            
            logger.info("Metadata updated")
        
        except Exception as e:
            logger.warning(f"Failed to update metadata: {e}")
```

### Step 3: Integrate into Daily Harvest

**File:** `jobs/daily_harvest_all.py` (Update existing)

```python
# Add to the end of daily harvest
from jobs.sync_firebase import FirebaseSyncEngine

def run_daily_harvest():
    """Daily harvest with Firebase sync"""
    
    # ... existing code ...
    
    # Step: Sync to Firebase
    firebase_config = os.getenv('FIREBASE_CONFIG_PATH', 'config/firebase-config.json')
    if os.path.exists(firebase_config):
        logger.info("Syncing to Firebase...")
        firebase_engine = FirebaseSyncEngine(firebase_config)
        firebase_result = firebase_engine.sync_all_listings(
            os.getenv('PLANWATCH_DB', 'data/planwatch.sqlite3')
        )
        logger.info(f"Firebase sync result: {firebase_result}")
    else:
        logger.warning("Firebase config not found, skipping sync")
    
    return {
        'harvest': harvest_result,
        'firebase': firebase_result
    }
```

---

## Render API Updates

### Step 1: Install Firebase SDK

Add to `requirements.txt`:

```
firebase-admin==6.0.0
```

### Step 2: Update Dashboard API

**File:** `dashboard.py` (Update existing)

```python
from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, db
import os
import logging
from functools import lru_cache
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Initialize Firebase
def init_firebase():
    """Initialize Firebase connection"""
    firebase_config = os.getenv('FIREBASE_CONFIG_PATH', 'config/firebase-config.json')
    
    if os.path.exists(firebase_config):
        if not firebase_admin._apps:
            cred = credentials.Certificate(firebase_config)
            firebase_admin.initialize_app(cred)
        return db.reference()
    
    logger.warning("Firebase not configured, API will be read-only")
    return None

firebase_db = init_firebase()

# === CACHE LAYER ===
class CacheManager:
    """Simple in-memory cache for Firebase queries"""
    
    def __init__(self, ttl_seconds=3600):
        self.ttl = ttl_seconds
        self.cache = {}
    
    def get(self, key):
        """Get cached value if not expired"""
        if key not in self.cache:
            return None
        
        value, timestamp = self.cache[key]
        if datetime.now() - timestamp > timedelta(seconds=self.ttl):
            del self.cache[key]
            return None
        
        return value
    
    def set(self, key, value):
        """Cache a value with timestamp"""
        self.cache[key] = (value, datetime.now())
    
    def clear(self, key=None):
        """Clear cache"""
        if key:
            self.cache.pop(key, None)
        else:
            self.cache.clear()

cache = CacheManager(ttl_seconds=3600)  # 1 hour TTL

# === PUBLIC ENDPOINTS ===

@app.route('/api/health', methods=['GET'])
def health():
    """Health check endpoint"""
    try:
        if firebase_db:
            # Check Firebase connectivity
            firebase_db.child('metadata').get()
        
        return jsonify({
            'status': 'ok',
            'timestamp': datetime.now().isoformat(),
            'database': 'firebase' if firebase_db else 'none'
        }), 200
    
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get aggregated statistics (cached)"""
    
    # Check cache first
    cached = cache.get('stats')
    if cached:
        return jsonify({**cached, 'cached': True}), 200
    
    try:
        if not firebase_db:
            return jsonify({'error': 'Firebase not configured'}), 500
        
        # Get stats from Firebase metadata
        stats_data = firebase_db.child('metadata').child('stats').get().val()
        
        if not stats_data:
            stats_data = {'total_listings': 0, 'last_updated': None}
        
        # Add computed stats
        stats_data['cached'] = False
        stats_data['cache_ttl'] = cache.ttl
        
        # Cache for next hour
        cache.set('stats', stats_data)
        
        return jsonify(stats_data), 200
    
    except Exception as e:
        logger.error(f"Failed to get stats: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/local-listings', methods=['GET'])
@require_auth
def get_listings():
    """
    Get paginated listings from Firebase.
    
    Note: Firebase doesn't support offset queries well.
    For production, consider using Firestore with proper indexing.
    """
    
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 50, type=int)
    city = request.args.get('city', None)
    
    try:
        if not firebase_db:
            return jsonify({'error': 'Firebase not configured'}), 500
        
        # Get all listings (Firebase limitation)
        # For better performance, use Firestore instead
        listings_data = firebase_db.child('listings').get().val()
        
        if not listings_data:
            return jsonify({
                'listings': [],
                'total': 0,
                'page': page,
                'pages': 0
            }), 200
        
        # Convert to list and filter
        listings = []
        for doc_id, doc_data in listings_data.items():
            # Apply filters
            if city and doc_data.get('city') != city:
                continue
            
            doc_data['id'] = doc_id
            listings.append(doc_data)
        
        # Sort by updated_at (newest first)
        listings.sort(
            key=lambda x: x.get('updated_at', ''),
            reverse=True
        )
        
        total = len(listings)
        
        # Paginate
        start = (page - 1) * limit
        end = start + limit
        page_listings = listings[start:end]
        
        return jsonify({
            'listings': page_listings,
            'total': total,
            'page': page,
            'limit': limit,
            'pages': (total + limit - 1) // limit,
            'from_cache': False
        }), 200
    
    except Exception as e:
        logger.error(f"Failed to get listings: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/search', methods=['GET'])
@require_auth
def search_listings():
    """
    Full-text search in listings.
    
    Firebase doesn't support full-text search natively.
    This does in-memory search on all listings.
    """
    
    query = request.args.get('q', '').strip()
    
    if not query or len(query) < 2:
        return jsonify({'error': 'Query too short (min 2 chars)'}), 400
    
    try:
        if not firebase_db:
            return jsonify({'error': 'Firebase not configured'}), 500
        
        # Get all listings
        listings_data = firebase_db.child('listings').get().val()
        
        if not listings_data:
            return jsonify({'results': [], 'count': 0}), 200
        
        # Search in-memory
        query_lower = query.lower()
        results = []
        
        for doc_id, doc_data in listings_data.items():
            # Search in title, description, address
            searchable = (
                (doc_data.get('title', '') or '').lower() + ' ' +
                (doc_data.get('description', '') or '').lower() + ' ' +
                (doc_data.get('address', '') or '').lower()
            )
            
            if query_lower in searchable:
                doc_data['id'] = doc_id
                results.append(doc_data)
        
        # Sort by relevance (title matches first)
        results.sort(key=lambda x: (
            query_lower not in (x.get('title', '') or '').lower(),
            x.get('updated_at', '')
        ), reverse=True)
        
        return jsonify({
            'results': results[:100],  # Limit to 100
            'count': len(results),
            'query': query
        }), 200
    
    except Exception as e:
        logger.error(f"Search failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/clear-cache', methods=['POST'])
@require_admin_auth
def clear_cache():
    """Clear API cache (admin only)"""
    cache.clear()
    return jsonify({'status': 'cache cleared'}), 200
```

### Step 3: Add to Environment Variables

In Render dashboard, add:

```
FIREBASE_CONFIG_PATH=/data/firebase-config.json
```

Upload the JSON file to Render's persistent disk:
```bash
# Local machine
scp config/firebase-config.json render@planwatch.onrender.com:/data/
```

---

## Google Apps Script

### Step 1: Create Google Apps Script Project

```
1. Go to https://script.google.com
2. Click "New project"
3. Name: "NADLANFIX Heartbeat"
4. Create
```

### Step 2: Add Heartbeat Code

**File:** `keep_render_alive.gs`

```javascript
// keep_render_alive.gs
// Purpose: Keep Render web service alive with periodic HTTP requests

const RENDER_API_URL = 'https://planwatch.onrender.com/api/health';
const LOG_SHEET_ID = PropertiesService.getScriptProperties().getProperty('LOG_SHEET_ID');
const SLACK_WEBHOOK_URL = PropertiesService.getScriptProperties().getProperty('SLACK_WEBHOOK_URL');

function keepRenderAlive() {
  /**
   * Sends HTTP request to Render API every 12 minutes.
   * This prevents Render from spinning down due to inactivity.
   * 
   * Cost: FREE (included in GAS quota)
   * Execution time: ~1 second
   * Quota: 10,000 executions/day free (we use ~120/day)
   */
  
  Logger.log('🕐 [' + new Date().toISOString() + '] Starting heartbeat...');
  
  try {
    const options = {
      method: 'get',
      muteHttpExceptions: true,
      timeout: 10
    };
    
    const response = UrlFetchApp.fetch(RENDER_API_URL, options);
    const status = response.getResponseCode();
    
    if (status === 200) {
      Logger.log('✅ Render OK - Status: ' + status);
      logHeartbeat('success', status, null);
    } else {
      Logger.log('⚠️  Render Warning - Status: ' + status);
      logHeartbeat('warning', status, null);
      
      if (SLACK_WEBHOOK_URL) {
        notifySlack('⚠️ Render returned status: ' + status);
      }
    }
    
  } catch (error) {
    Logger.log('❌ Render Error: ' + error);
    logHeartbeat('error', 0, error.toString());
    
    if (SLACK_WEBHOOK_URL) {
      notifySlack('🔴 Render unreachable: ' + error);
    }
  }
}

function logHeartbeat(status, statusCode, error) {
  /**
   * Log heartbeat to Google Sheet for monitoring.
   * Keeps history of all Render checks.
   */
  
  if (!LOG_SHEET_ID) {
    Logger.log('Warning: LOG_SHEET_ID not set');
    return;
  }
  
  try {
    const spreadsheet = SpreadsheetApp.openById(LOG_SHEET_ID);
    const sheet = spreadsheet.getSheetByName('Heartbeat Log') || 
                  spreadsheet.insertSheet('Heartbeat Log');
    
    // Add headers if first row
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(['Timestamp', 'Status', 'Status Code', 'Error', 'Response Time']);
    }
    
    // Append log entry
    const timestamp = new Date().toISOString();
    sheet.appendRow([timestamp, status, statusCode, error || '', '~1s']);
    
    // Keep only last 1000 rows
    if (sheet.getLastRow() > 1000) {
      sheet.deleteRows(2, 100);
    }
    
    Logger.log('✓ Logged to sheet: ' + timestamp);
    
  } catch (error) {
    Logger.log('Failed to log to sheet: ' + error);
  }
}

function notifySlack(message) {
  /**
   * Send notification to Slack channel.
   * Only for errors/warnings, not every success.
   */
  
  if (!SLACK_WEBHOOK_URL) {
    return;
  }
  
  try {
    const payload = {
      text: message + '\n_' + new Date().toISOString() + '_',
      username: 'NADLANFIX Monitor',
      icon_emoji: ':robot_face:'
    };
    
    const options = {
      method: 'post',
      muteHttpExceptions: true,
      payload: JSON.stringify(payload)
    };
    
    const response = UrlFetchApp.fetch(SLACK_WEBHOOK_URL, options);
    Logger.log('Slack notification sent: ' + response.getResponseCode());
    
  } catch (error) {
    Logger.log('Slack notification failed: ' + error);
  }
}

function testHeartbeat() {
  /**
   * Manual test function.
   * Run this once to verify setup is working.
   */
  Logger.log('🧪 Testing heartbeat...');
  keepRenderAlive();
  Logger.log('Test complete. Check execution logs above.');
}

function setupTrigger() {
  /**
   * Set up automatic triggers.
   * Call this once to install the heartbeat.
   */
  
  // Remove existing triggers
  const triggers = ScriptApp.getProjectTriggers();
  triggers.forEach(trigger => {
    ScriptApp.deleteTrigger(trigger);
  });
  
  // Create new trigger: every 12 minutes
  ScriptApp.newTrigger('keepRenderAlive')
    .timeBased()
    .everyMinutes(12)
    .create();
  
  Logger.log('✅ Trigger set: keepRenderAlive every 12 minutes');
  Logger.log('Total executions per day: ~120 (quota: 10,000)');
}

function getCurrentStats() {
  /**
   * Fetch current stats from Render API.
   * Use this to manually check system status.
   */
  
  try {
    const response = UrlFetchApp.fetch(
      'https://planwatch.onrender.com/api/stats',
      { muteHttpExceptions: true }
    );
    
    if (response.getResponseCode() === 200) {
      const data = JSON.parse(response.getContentText());
      Logger.log('Current stats:');
      Logger.log(JSON.stringify(data, null, 2));
      return data;
    } else {
      Logger.log('Failed to get stats: ' + response.getResponseCode());
      return null;
    }
    
  } catch (error) {
    Logger.log('Error: ' + error);
  }
}
```

### Step 3: Configure GAS Project

```javascript
// In Google Apps Script Editor:

// 1. Set Properties
// Click "Project Settings" (gear icon)
// Add these:
// RENDER_API_URL = https://planwatch.onrender.com/api/health
// LOG_SHEET_ID = [Your Google Sheet ID]
// SLACK_WEBHOOK_URL = [Your Slack webhook, optional]

// 2. Run setupTrigger() once
// Click "Run" → select setupTrigger
// Authorize permissions

// 3. Verify
// Check "Execution log"
// Should show: ✅ Trigger set

// 4. Test
// Run testHeartbeat() to verify it works
```

---

## Security & Best Practices

### Firebase Security Rules

Update security rules in Firebase Console:

```javascript
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    
    // Public read access to listings
    match /listings/{document=**} {
      allow read: if true;  // Public
      allow write: if false; // No writes from client
    }
    
    // Public read to metadata/stats
    match /metadata/stats {
      allow read: if true;  // Public stats
      allow write: if false;
    }
    
    // Sync log (admin only)
    match /metadata/sync_log/{document=**} {
      allow read: if request.auth != null;  // Auth required
      allow write: if request.auth != null;
    }
  }
}
```

### Environment Variables Checklist

```bash
# Local machine (.env)
FIREBASE_CONFIG_PATH=config/firebase-config.json
PLANWATCH_DB=data/planwatch.sqlite3

# Render dashboard
FIREBASE_CONFIG_PATH=/data/firebase-config.json
PYTHONUNBUFFERED=1

# Google Apps Script (Project Settings)
LOG_SHEET_ID=1234567890abcdef
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
```

### Security Best Practices

1. **Never commit firebase-config.json**
   ```bash
   echo "config/firebase-config.json" >> .gitignore
   ```

2. **Rotate Firebase keys annually**
   ```bash
   firebase key:rotate --project=nadlanfix-2026
   ```

3. **Monitor Firebase usage**
   ```
   Firebase Console → Usage & Billing
   Set daily budget limit: $5 (way higher than needed)
   ```

4. **Monitor GAS executions**
   ```
   Apps Script → Executions
   Look for errors/timeouts
   ```

---

## Troubleshooting

### Issue 1: Firebase Connection Failed

**Error:** `ConnectionError: Failed to connect to Firebase`

**Solution:**
```bash
# Verify firebase-config.json exists
ls -la config/firebase-config.json

# Verify JSON is valid
python -c "import json; json.load(open('config/firebase-config.json'))"

# Test connection
python -c "
import firebase_admin
from firebase_admin import credentials, db
cred = credentials.Certificate('config/firebase-config.json')
firebase_admin.initialize_app(cred)
print('✓ Firebase connected')
"
```

### Issue 2: GAS Trigger Not Running

**Error:** Trigger doesn't execute on schedule

**Solution:**
```javascript
// In Google Apps Script:
1. Click "Triggers" (clock icon)
2. Look for "keepRenderAlive"
3. If missing, run setupTrigger() again
4. Check "Executions" tab for errors
5. Verify LOG_SHEET_ID is correct
```

### Issue 3: Firebase Quota Exceeded

**Error:** `too many writes` or `quota exceeded`

**Solution:**
```bash
# Check Firebase usage
firebase use nadlanfix-2026
firebase functions:log

# Reduce batch size if needed
# In sync_firebase.py:
batch_size = 250  # Reduce from 500

# Add delays between batches
time.sleep(0.5)  # Increase from 0.1
```

### Issue 4: Render API Returning 500

**Error:** `Firebase not initialized`

**Solution:**
```bash
# Verify firebase-config.json uploaded to Render
# In Render dashboard:
# 1. Shell tab
# 2. ls -la /data/firebase-config.json

# Verify environment variable set
# FIREBASE_CONFIG_PATH=/data/firebase-config.json

# Restart Render service
# In Render dashboard → click "Restart service"
```

---

## Verification Checklist

- [ ] Firebase project created (nadlanfix-2026)
- [ ] Firestore database initialized
- [ ] Service account key generated
- [ ] firebase-config.json saved locally
- [ ] Firebase SDK installed (pip install firebase-admin)
- [ ] Local sync tested (python jobs/sync_firebase.py)
- [ ] Render API updated and deployed
- [ ] firebase-config.json uploaded to Render
- [ ] Google Apps Script project created
- [ ] Heartbeat trigger set up
- [ ] GAS test executed successfully
- [ ] Slack webhook configured (optional)
- [ ] Security rules applied
- [ ] Firebase usage alerts set
- [ ] Monitoring sheet created

---

## Cost Summary

| Component | Cost |
|-----------|------|
| Render Web | $12/mo |
| Firebase (free tier) | $0 |
| Google Apps Script | $0 |
| **Total** | **$12/mo** |

**At 3x growth:**
- Firebase would cost ~$0.50/mo
- Still under $1/mo!

---

## Next Steps

1. Complete Firebase setup above
2. Test sync locally
3. Deploy to Render
4. Set up GAS heartbeat
5. Monitor for 1 week
6. Optimize queries if needed
