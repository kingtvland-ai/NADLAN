# FIXIT

## תמצית

מבחינת מבנה ו-Layering, NADLANFIX כבר מכיל את רוב הליבה של FMS-MENY וגם את ה-Facebook Marketplace logic שהועבר פנימה. זה אומר שאנחנו לא מתחילים מאפס. הבעיה העיקרית היא לא חוסר תשתית, אלא חוסר ארכיטקטורה אחידה, חוסר standardization, חוסר תהליך יומי/סדיר, תלות במודולים משוכפלים, ו-absence of clean operational layer שמחבר בין קצירה (scrape), NORM, DB, and public app.

היעד הסופי צריך להיות:

- NADLANFIX הוא המוצר הראשי
- Yad2 + Facebook + OnMap + other sources הם source adapters
- SQLite הוא raw local store
- Firestore הוא read-optimized operational DB
- כל קצב הסריקה נעשה פעם ביום על השרת המקומי, לא בזמן אמת מהלקוח
- המשתמשים פותחים רק את ה-DB/API, ולא מבצעים קריאות ישירות לאתרי המקור

---

## 1. מצב נוכחי

### 1.1 סטטוס ההשוואה

כפי שנבדק בפועל:
- NADLANFIX: 125 קבצים
- FMS-MENY: 124 קבצים
- FacebookMScrap: 39 קבצים

חסרים ב-NADLANFIX מול FMS-MENY: 4 קבצים בלבד:
- debug-regions.js
- debug-scrape.js
- package-lock.json
- test-onmap-normalize.mjs

ב-NADLANFIX ישנם גם 5 קבצים ייעודיים של Facebook:
- facebook_comparables.py
- facebook_feed.py
- facebook_metadata.py
- facebook_scoring.py
- facebook_valuation.py

מסקנה:
- אין חוסר פונקציונלי עקרוני
- המערכת כבר “מכילה” את רוב הדברים
- החסר הוא ארכיטקטורה, ניהול lifecycle, ו-תפעול יומי מסודר

### 1.2 מה קיים כבר

ב-NADLANFIX יש כבר: 
- Yad2 harvester: yad2_feed.py
- Dashboard/API: dashboard.py
- Lead intelligence: lead_intel.py
- DB layer: db.py
- Sync jobs: sync.py
- Facebook scrapers: facebook_feed.py + facebook_scoring.py + facebook_valuation.py + facebook_metadata.py + facebook_comparables.py

כלומר, ה-Pipeline הבסיסי כבר קיים. הבעיה היא שהכל נמצא במצב של “מיזוג/פורט”, לא במצב של “מוצר גמיש וסטנדרטי”.

---

## 2. הבעיות המרכזיות

### 2.1 חוסר standardization בתחום כללי

יש פרויקט שכולל:
- Yad2
- Facebook
- dashboard
- API
- webapp
- backend
- archive/frontend
- utilities

זה יוצר חוסר clarity בנוגע ל:
- מה הוא ה-Source of Truth
- מה הוא ה-UI הראשי
- מהו ה-DB הראשי
- מהו ה-Job שמריץ את הסריקה יום-יומית
- מי אחראי על maintenance

הפרויקט צריך להיות חכם, לא רק “מכיל דברים”.

### 2.2 תלות אימPLICIT בין פרויקטים

ב-facebook_feed.py יש רמז ישיר לכך שה-session state נוצר בפרויקט אחר (FacebookMScrap). זה יוצר:
- תלות אקטיבית עם פרויקט אחר
- trouble on new machine
- הקפאה במקרה שאין קובץ session
- בעיות onboarding

זה צריך להיפתר על ידי:
- יצירת session-management standard בתוך NADLANFIX עצמו
- או package dependency מותקן
- או סקריפט שנוצר והאחראי על auth state בתוך NADLANFIX

### 2.3 duplication of logic

יש הרבה קוד שחזור מה-FacebookMScrap לתוך NADLANFIX. זה לא רק copy, אלא copy without clean ownership. לדוגמה:
- facebook_feed.py
- facebook_scoring.py
- facebook_valuation.py
- facebook_metadata.py
- facebook_comparables.py

בעקבות זה:
- maintenance קשה יותר
- באגים חוזרים
- שינויים לא מתפזרים
- אין single implementation source

### 2.4 אין lifecycle mature של daily source ingestion

המערכת מורכבת מדברים ש”מסתובבים”, אך אין:
- scheduler standard
- run state per source
- retry policy
- lock mechanism
- run status API
- last_success / last_failure tracking
- failure escalation

לכן אין דרך אמינה להגיד: “הסקריפים ירוצו פעם ביום בצורה מאובטחת”.

### 2.5 אין separation בין source layer לבין read layer

המערכת מקבלת נתונים ישירות מאתרים ובמקרים מסוימים אפשר לדרוך על source calls גם מה-client layer. זה לא מתאים.

העיצוב הנכון:
- source jobs run only server-side
- local DB stores raw and normalized data
- Firestore stores latest operational version
- clients read only from DB/API

### 2.6 אין versioning ברורה לעדכון נתונים

שתי בעיות חשובות:
- לא שם לב ל-schema evolution
- לא תמיד יש קונצנזוס אם ה-DB הוא raw/final/derived

צריך להחזיק:
- schema version
- source version
- run version
- normalized version
- snapshot version

### 2.7 אין strategy ברור ל-Firestore

היום הכיוון “להעלות ל-Firestore” לא מוגדר בצורה ברורה. צריך לקבוע:
- איזה נתונים עוברים ל-Firestore
- אילו נתונים נשארים ב-SQLite בלבד
- מהו cycle sync
- איזה collections יהיו קיימים
- עד כמה לעדכן נתונים

---

## 3. מה צריך להתקן בפועל

## P0 – repair items (חובה)

### 3.1 העבר את NADLANFIX ממש כמיזוג למוצר אחת

קבועים עקרונות:
- NADLANFIX הוא המוצר הראשי
- כל Source adapters נבנים underneath
- כל source известно ומסומן
- כל UI/API מופעלים דרך ליבת המוצר

### 3.2 יצירת unified scheduler

צריך ליצור:
- jobs/daily_ingest.py
- jobs/source_runner.py
- jobs/scheduler.py
- jobs/run_state.py

הjobs צריכים:
- קוד יומי / קוד מטלות
- locked run protection
- retry on failure
- status per source
- last_success / last_failure
- next_run

### 3.3 יצירת source ownership per module

לכל source צריך להיות owner ו-standard:
- Yad2
- Facebook
- ONMAP
- market data
- other feeds

Each source must expose standard interface:
- fetch()
- normalize()
- dedupe()
- score()
- persist()
- export()

### 3.4 קביעת Source of Truth

סוגיית ה-Source of Truth צריכה להיות החלטה ברורה:
- RAW DB = SQLite local
- OPERATIONAL DB = Firestore
- CLIENTS = never call external websites

אין מצב שבו הלקוח או ה-client ממשיכים לעבור לאתרי חיצוניים. 
כל חיבור חיצוני הוא ב-server-side בלבד.

### 3.5 Firestore sync flow

יש להוסיף standard export layer:
- export_to_firestore.py
- sync_latest_listings.py
- sync_runs.py
- sync_health.py

כל batch צריך:
- upsert records by canonical ID
- delete stale listings if delisted
- keep history in SQLite
- not overload Firestore

---

## P1 – improvements (ממש חשובים)

### 3.6 נקי תיעוד ותהליך setup

לשכתב את README של NADLANFIX כך שיכלול:
- setup instructions
- dependency install
- runtime order
- which UI is the canonical one
- how to run scheduler
- how to run Firestore sync
- environment variables
- troubleshooting

### 3.7 הכנס layer app/accounting

יש להוסיף:
- user API layer
- role-based access
- public/private endpoints
- config validation

### 3.8 Health monitoring

צריך להוסיף:
- source health endpoint
- last success time
- last failure time
- source status ready/failed/stale
- alert on source failure

### 3.9 Data validation layer

יש להוסיף validation per run:
- no empty listings
- no missing required fields
- schema verification before export
- price sanity checks
- duplicates check

---

## P2 – optimization and long-term upgrade

### 3.10 store snapshots

בנוסף ל-SQLite current, צריך גם snapshots:
- raw daily snapshot
- normalized snapshot
- export snapshot

זה חשוב כדי למנוע degradation מהמשך הזמן.

### 3.11 typed models

החלפה של dict-heavy structure ל-typed models (dataclass / pydantic) כדי להפחית תקלות.

### 3.12 queue and worker model

אם יש הרבה sources, צריך:
- Queue for jobs
- worker pool
- separate ingestion tasks
- priorities

מניעת deadlocks / run overlap.

### 3.13 cache policy

ה-Firestore לא אמור להתעדכן בכל קריאה. צריך:
- batch updates
- update every run only
- TTL per collection
- versioned records

---

## 4. ארכיטקטורה מוצעת סופית

## 4.1 כללי

NADLANFIX 
├── app/
│   ├── api/
│   ├── ui/
│   └── auth/
├── sources/
│   ├── yad2/
│   ├── facebook/
│   ├── onmap/
│   └── shared/
├── pipeline/
│   ├── fetch
│   ├── normalize
│   ├── dedupe
│   ├── score
│   ├── enrich
│   └── persist
├── storage/
│   ├── sqlite_local
│   ├── firestore_sync
│   └── snapshots
├── jobs/
│   ├── scheduler.py
│   ├── daily_ingest.py
│   ├── retry.py
│   └── health.py
├── config/
│   ├── settings.py
│   ├── env.py
│   └── secrets.py
├── observability/
│   ├── logs
│   ├── metrics
│   └── alerts
└── tests/
    ├── integration
    ├── unit
    └── smoke

### 4.2 Source adapter contract

כל source צריך לעמוד בממשק סטנדרטי:

- fetch() -> raw records
- normalize(raw) -> canonical listing records
- dedupe(listings) -> merged records
- score(listings) -> score + reasons
- persist(listings) -> SQLite write
- export_latest() -> Firestore payload

### 4.3 Local storage contract

SQLite local should hold:
- raw_source_rows
- normalized_listings
- listing_history
- price_history
- source_runs
- errors
- dedupe_map
- snapshots

Firestore should hold:
- latest listings
- active alerts
- run summaries
- health state
- user-facing data only

---

## 5. Daily ingestion model

### 5.1 Run pattern

כל source מקבל:
- schedule time
- retry policy
- max pages / max items
- allowed window
- single run lock
- failure behavior

### 5.2 Example schedule

- 02:00 — Yad2 refresh job
- 02:15 — Facebook refresh job
- 02:30 — ONMAP / market refresh job
- 02:45 — merge + normalize + score
- 03:00 — push latest records to Firestore
- 03:10 — health verification

### 5.3 Safe behaviour

אם source fail:
- log failure
- store last_error
- retry 3 times
- do not block complete pipeline
- continue with others
- mark source stale

---

## 6. Firestore schema proposal

### 6.1 collections

#### listings
Fields:
- id
- source
- external_id
- canonical_id
- title
- city
- neighborhood
- price
- price_history
- sqm
- rooms
- status
- is_active
- created_at
- updated_at
- score
- category
- condition
- url

#### source_runs
Fields:
- run_id
- source
- started_at
- finished_at
- status
- items_seen
- items_inserted
- items_updated
- errors

#### source_health
Fields:
- source
- last_success
- last_failure
- status
- retry_count
- message

#### alerts
Fields:
- alert_id
- listing_id
- type
- severity
- value
- created_at
- delivered_at
- is_resolved

### 6.2 data policy

Firestore should contain only:
- current active listings
- derived score info
- alert info
- health info
- runs summary

SQLite remains the deep archive.

---

## 7. קצב סריקה והשתמשים

### 7.1 קצב

כמו שהתבקש, צריך שיתאפשר:
- סריקה של כל המאגרים פעם ביום
- בקצב שמתאים למחשב מקומי
- לא חצי-זמן / השהיות קצרות
- ממשק אחיד ומנוהל

### 7.2 מימוש אפשרי

- Windows Task Scheduler / cron / run-as-service
- תהליך one-shot per source
- אין polling from client
- לא עושה סריקה “על כל בקשה”

### 7.3 resource control

כדי להתאים למחשב מקומי, יש לקבוע:
- max workers per source
- stagger start times
- concurrency cap
- one browser per source
- cooldown between pages
- limit per job per run

---

## 8. מסלול סריקה לאתרי חיצוניים

הסכימה הנכונה היא:

- scraper job runs on server machine
- data stored locally
- data syncs to Firestore
- end users do not hit original websites

אסור: 
- יישומון לקוח מפנה ישירות ל-Yad2 / Facebook וכו'
- repeated direct fetch from clients

זה מה שיביא ל-rate limit ו-failure in production.

---

## 9. בעיות קיימות שצריך לתקן מיידית

### 9.1 ניהול auth state

data/facebook_storage_state.json צריך להיות hosted within NADLANFIX, with full ownership and clear lifecycle.

### 9.2 קבצים מיותרים/לא מסודרים

יש קבצים/דפדפנים/שגיאות שהסתבכו מתהליך ה-merge. חלק מהם כבר חסרים, חלקם נראים כמו debug utilities. צריך:
- לאסוף debug scripts
- להפריד מה-מוצר הראשי
- לקבוע מה קובץ production ומה test/debug

### 9.3 README confusion

הדוקומנטציה של NADLANFIX כיום היא mix של:
- Yad2
- PlanWatch
- Facebook
- backend
- frontend
- archive

זה צריך להיות restructured.

### 9.4 תזמון ו-דחיפות

ספציפית צריך שכבות:
- scheduler
- retry
- status
- delayed run
- manual run trigger

### 9.5 schema drift

יש מצב שבו כל source מייצר מעט שונה. צריך standard schema layer שממיר כל source ל-canonical model אחד.

---

## 10. תכנית עבודה מומלצת (implementation roadmap)

## Phase 1: Stabilize current project

Tasks:
- איחוד ownership של כל source
- קביעת source list
- קביעת source of truth
- סגירת באגים בסיסיים של import paths
- בדיקת startup flow

Deliverable:
- project runs from clean clone
- single startup flow

## Phase 2: Daily orchestration

Tasks:
- הוספת scheduler
- per-source run lock
- retry logic
- health tracking
- run log

Deliverable:
- all sources run once per day
- safe rescheduling

## Phase 3: Standardize normalization

Tasks:
- unify listing model
- canonical ID
- dedupe logic
- score logic
- history tracking

Deliverable:
- one canonical listing form for all sources

## Phase 4: SQLite as raw archive

Tasks:
- define raw tables
- define normalized tables
- define dedupe tables
- define snapshot tables

Deliverable:
- full historical raw and normalized store

## Phase 5: Firestore sync

Tasks:
- define collections
- define API payload format
- implement exporter
- implement batch upserts
- implement stale cleanup

Deliverable:
- users consume Firestore data, not source websites

## Phase 6: Public API and clients

Tasks:
- API endpoints
- user-facing filters
- alerts
- market summaries
- dashboards

Deliverable:
- app can run without direct source calls

---

## 11. priorities by order

### Priority 0 — must do
- source ownership cleanup
- scheduler
- single source-of-truth
- SQLite local store
- Firestore sync layer

### Priority 1 — must improve
- health monitoring
- dedupe + canonical IDs
- README cleanup
- validation

### Priority 2 — nice to have
- snapshot system
- queue workers
- advanced alerts
- multi-source merge analytics

---

## 12. concrete design for the system you asked for

### 12.1 The exact final behavior

- once per day, local machine executes source pulls
- pulls Yad2 + Facebook + all relevant feeds
- normalizes and deduplicates data
- stores raw + processed version in SQLite
- syncs only active/derived data to Firestore
- users query Firestore/API
- no one opens Yad2 or Facebook directly for daily data access

### 12.2 This is the required model

Data lifecycle:

1. Source fetch
2. normalize
3. dedupe
4. enrich/score
5. local SQLite write
6. Firestore export
7. API reads from Firestore
8. clients never call websites

---

## 13. recommended implementation decisions

### Decision 1: SQLite local is supreme

Use SQLite for:
- raw data
- historical data
- source state
- complete historical snapshots

### Decision 2: Firestore is read-optimized

Use Firestore for:
- active listings
- alerts
- run status
- current state
- user consumption

### Decision 3: Use scheduler, not request-driven scraping

Nobody should trigger live scraping by page load. That is the root of instability and rate-limit pressure.

### Decision 4: Keep a stable canonical listing model

Use one ID per listing and one standard schema. This is the single most important technical fix after the scheduler.

---

## 14. final call to action

המצב הנוכחי של NADLANFIX הוא לא פגום לחלוטין, אבל הוא לא מוכן כ-product-ready architecture. הוא כבר קרוב מאוד ל-מה שרצינו, אבל צריך:

- ארכיטקטורה ברורה
- ownership מסודר
- scheduler יומי
- schema standard
- SQLite + Firestore split
- read-only access for clients
- source isolation
- monitoring and health

אם נבצע את זה, אז NADLANFIX יהפוך ל-platform אמיתי, חזק, וניתן להתרחבות, כאשר שני הפרויקטים (FMS-MENY ו-FacebookMScrap) משולבים בתוכו כ-modules תקינים ולא כ-copy-paste.

---

## 15. קובץ פעולה מפורט לתיקון

### 15.1נדרש לבצע לפי סדר

1. לעבור על כל source module ולסמן מיהו owner
2. להקים unified scheduler
3. להגדיר source-of-truth
4. להגדיר canonical schema
5. להקים local SQLite architecture
6. להקים Firestore sync layer
7. להוסיף health+alerts
8. להוסיף validation
9. לשכתב README
10. לבדוק smoke tests and prod deployment

### 15.2 תזמון עבודה מומלץ

- שבוע 1: architecture + source ownership + schema cleanup
- שבוע 2: daily scheduler + retries + health
- שבוע 3: Firestore sync + read API
- שבוע 4: validation + smoke tests + production config

---

## 16. מסקנה סופית

ההבדל בין “פרויקט שעובד” לבין “מערכת מוכנה לייצור” הוא לא רק קוד — הוא ארכיטקטורה, תהליך, and operational discipline.

NADLANFIX כבר נמצא על הדרך הנכונה. 
העבודה הבאה היא לא לכתוב עוד קוד, אלא ליישר את כל ה-threads למבנה אחיד של:

- source jobs
- normalized data
- local raw storage
- Firestore export
- API for clients
- no direct external calls

זה יהיה המפתח להצלחה.
