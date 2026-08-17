# LIST

## תמצית

הפרויקט כבר מתפקד כ-Core system עם רוב הליבה, אבל הוא עדיין לא סגור כ-platform אחיד. 
המשימה הבאה היא לא להוסיף עוד מודולים, אלא לסדר את architecture, ליישר את הנתונים, לקבוע Source of Truth, ולהגדיר operating flow יומי־סדיר על בסיס NADLANFIX בלבד.

הגדרת היעד הסופית:
- NADLANFIX הוא הפרויקט הראשי
- Yad2, Facebook, ONMAP, וכד' הם source adapters
- SQLite הוא local raw archive
- Firestore הוא read-optimized operational layer
- הסריקה מתבצעת פעם ביום על המחשב המקומי
- המשתמשים קוראים רק ל-API/DB, לא לאתרים עצמם

---

## 1. מה כבר נעשה

### 1.1 Core logic already exists

ב-NADLANFIX יש כבר:
- [NADLANFIX/yad2_feed.py](NADLANFIX/yad2_feed.py)
- [NADLANFIX/facebook_feed.py](NADLANFIX/facebook_feed.py)
- [NADLANFIX/dashboard.py](NADLANFIX/dashboard.py)
- [NADLANFIX/db.py](NADLANFIX/db.py)
- [NADLANFIX/lead_intel.py](NADLANFIX/lead_intel.py)
- [NADLANFIX/sources/base/adapter.py](NADLANFIX/sources/base/adapter.py)
- [NADLANFIX/sources/yad2/adapter.py](NADLANFIX/sources/yad2/adapter.py)
- [NADLANFIX/sources/facebook/adapter.py](NADLANFIX/sources/facebook/adapter.py)

### 1.2 Core validation passed

בדיקה שבוצעה:
- `python -m compileall .`
- `import dashboard, sources.base.adapter, sources.yad2.adapter, sources.facebook.adapter`

תוצאה:
- compileall עבר ללא שגיאות
- import passed
- output: `IMPORT_OK`

מסקנה: אין תלות קוד/פייתון ישירה בתיקיות אחרות בזמן ריצה.

---

## 2. מה שנשאר לתקן

## שלב 1 — קביעת ארכיטקטורה ברורה

### משימה 1.1: להגדיר NADLANFIX כפרויקט המוצר הראשי

צריך להבטיח שכל רכיב מתפקד תחת NADLANFIX בלבד, ללא תלות “עיוורת” במקור חיצוני.

#### החלטות:
- source adapters live under [NADLANFIX/sources](NADLANFIX/sources)
- source ownership is explicit
- all sources share same canonical schema
- all pipelines pass through one orchestrator

### משימה 1.2: להחליט על Source of Truth

החובה היא:
- SQLite = local raw + historical archive
- Firestore = read-optimized current state
- clients read only from Firestore/API

### משימה 1.3: להפריד ממשקים

יש לשפר את ההפרדה בין:
- source fetchers
- normalizers
- dedupe layer
- scoring layer
- persistence layer
- API layer
- Firestore sync layer

---

## שלב 2 — Unified source adapter והסכמה

### משימה 2.1: לכל source יש adapter

מומלץ שכל source יהיה עם הממשק הבא:

- fetch()
- normalize()
- dedupe()
- score()
- persist()
- export_latest()

### משימה 2.2: Canonical model אחד

כל מודל צריך להיות ממופה למבנה קבוע, דוגמת:

- canonical_id
- source
- external_id
- title
- city
- neighborhood
- price
- price_history
- sqm
- rooms
- url
- first_seen
- last_seen
- status
- score
- category
- condition

### משימה 2.3: אחדות ב- dedupe

יש להוסיף dedupe layer שמגדיר:
- same property across Facebook/Yad2 should resolve to one canonical record
- maintain source mapping table
- maintain `source_links` or `canonical_aliases`

---

## שלב 3 — Daily scheduler

### משימה 3.1: job runner יומי

יש להוסיף layer חדש בשם:
- jobs/
  - scheduler.py
  - daily_ingest.py
  - retry.py
  - health.py

### משימה 3.2: סריקה פעם ביום

גרפים מומלצים:
- 02:00 Yad2
- 02:15 Facebook
- 02:30 ONMAP / market
- 02:45 normalize + dedupe
- 03:00 Firestore sync
- 03:10 run health check

### משימה 3.3: lock + retry

חייבים:
- single run lock per source
- retry on failure (3 tries)
- backoff delay
- source health state
- success/failure timestamps

### משימה 3.4: health state

כל source צריך להחזיק:
- last_success
- last_failure
- status
- next_run
- last_error
- retry_count

---

## שלב 4 — SQLite local storage

### משימה 4.1: קבועים ו-סטנדרטים

SQLite צריך להיות ה-raw store, עם טבלאות:
- raw_source_rows
- normalized_listings
- listing_history
- price_history
- source_runs
- source_health
- snapshots
- dedupe_map
- errors

### משימה 4.2: snapshots

יש להוסיף snapshots יומיים:
- raw_snapshot
- normalized_snapshot
- export_snapshot

### משימה 4.3: schema versioning

כל שינוי ב-schema צריך לשמור:
- schema_version
- app_version
- migrated_at

---

## שלב 5 — Firestore sync

### משימה 5.1: define collections

מומלץ:

#### listings
- id
- canonical_id
- source
- external_id
- title
- city
- neighborhood
- price
- price_history
- sqm
- rooms
- url
- first_seen
- last_seen
- score
- category
- condition
- status
- updated_at

#### source_runs
- run_id
- source
- started_at
- finished_at
- status
- rows_seen
- rows_inserted
- rows_updated
- error

#### source_health
- source
- last_success
- last_failure
- status
- message

#### alerts
- alert_id
- listing_id
- type
- severity
- created_at
- resolved_at
- status

### משימה 5.2: sync policy

Firestore לא אמור להכיל:
- raw rows
- full historical store
- huge snapshots

Firestore צריך להכיל:
- current data
- latest enriched version
- health state
- alerts
- user-facing results

### משימה 5.3: sync cadence

מי שמגיב ל-requests לא צריך להתעדכן כל שניה.

אפשרות טובה:
- sync after each successful daily run
- batch upserts
- delete stale listings if no longer active

---

## שלב 6 — Client read model

### משימה 6.1: no direct website calls

כל לקוח, UI, App, script, צריך לקרוא רק ל-API/DB. 
לא לקרוא ל-Yad2/Facebook ישירות.

### משימה 6.2: API endpoints

מומלץ להוסיף:
- GET /api/listings
- GET /api/listings/{id}
- GET /api/stats
- GET /api/source-health
- GET /api/alerts
- POST /api/run
- GET /api/runs

### משימה 6.3: read from Firestore

ה-API צריך להריץ query על Firestore / cached normalized data ולא על אתרי המקור.

---

## שלב 7 — ops and quality

### משימה 7.1: validation

לפני כל sync ל-Firestore צריך לבדוק:
- אין שדות חסרים קריטיים
- price is numeric
- city exists
- title exists
- duplicate check passed
- invalid records are quarantined

### משימה 7.2: alerting

יש להוסיף:
- source failed
- total listings down
- stale data over threshold
- no successful run for X days

### משימה 7.3: observability

החלטה מומלצת:
- log all runs
- log all failures
- log all exports
- track K/V metrics

---

## שלב 8 — cleanup של legacy / old project leftovers

### משימה 8.1: remove or reduce project references

הפרויקטים הקודמים נותרו בעיקר כ-memory/history, לא כ-dependency. 
צריך להבהיר מה מתקיים כלאחר מכן:
- historic reference
- archive
- active source code

### משימה 8.2: standardize root structure

להביא את המבנה ל-clean state:

NADLANFIX/
├── app/
├── sources/
├── jobs/
├── storage/
├── config/
├── tests/
├── data/
├── README.md
├── FIXIT.md
├── LIST.md
└── run_all.py

### משימה 8.3: לאסוף debug scripts

צריך לייצר separation בין:
- production code
- debug utilities
- archive

---

## שלב 9 — מסלול ביצוע עד סיום

### Phase A — Architecture cleanup
- [ ] define final NADLANFIX as source of truth
- [ ] finalize source adapter pattern
- [ ] define canonical model
- [ ] define local run registry

### Phase B — Daily orchestration
- [ ] create jobs package
- [ ] implement scheduler
- [ ] implement run lock
- [ ] implement retry logic
- [ ] implement health tracking

### Phase C — Storage layer
- [ ] SQLite raw tables
- [ ] normalized tables
- [ ] snapshots
- [ ] raw-to-normal pipeline

### Phase D — Firestore
- [ ] define collections
- [ ] implement exporter
- [ ] batch upserts
- [ ] stale cleanup
- [ ] validation before export

### Phase E — API and UI
- [ ] create API layer
- [ ] filter active listings
- [ ] expose run health
- [ ] expose alerts

### Phase F — Production hardening
- [ ] smoke tests
- [ ] CI check
- [ ] README final
- [ ] deploy baseline

---

## 3. מה חשוב לקבוע כעת

### קביעה 1: NADLANFIX הוא המוצר היחיד

לא עוד “פרויקט + עותק + מקורות” — רק main product.

### קביעה 2: כל source runs locally, once daily

לא בכל request. לא מצפין on-demand. 
הקצב הוא יומי, עובר ב-jobs, מוגבל בזמן ובקצב זמין.

### קביעה 3: Firestore הוא read layer

Firestore אינו ה-source-of-truth; הוא המראה של המוצר ללקוח.

### קביעה 4: לאורחים מבחוץ אין קריאת אתרים

ה-API של מערכת צריך לספק את הנתונים, לא ה-client.

---

## 4. עדיפויות

### Priority 0 — חובה
- source ownership
- scheduler
- source-of-truth
- clean adapter layer
- local SQLite as raw store
- Firestore export layer

### Priority 1 — חובה לשיפור
- monitoring
- validation
- dedupe model
- alerts
- README cleanup

### Priority 2 — שיפור ארוך טווח
- snapshots
- worker queue
- performance optimization
- full observability dashboard

---

## 5. סיכום התוכנית

העבודה הנותרת היא לא “להוסיף עוד סריקות”, אלא ליישר את כל המערכת לארכיטקטורה מונחית־מוצר:

- source adapters
- one canonical model
- raw SQLite store
- synchronized Firestore read layer
- daily scheduler
- API only for consumers
- no direct website access from apps

זה יהיה ההבדל בין פרויקט שעומר קוד לבין מערכת תפעולית אמיתית.

---

## 6. שלב הבא המומלץ

הרצף המומלץ הוא:

1. ליישר את [NADLANFIX/sources](NADLANFIX/sources)
2. לייצר [NADLANFIX/jobs](NADLANFIX/jobs)
3. לייצר [NADLANFIX/storage](NADLANFIX/storage)
4. לייצר Firestore sync service
5. להפעיל full daily job
6. להפוך את [NADLANFIX/dashboard.py](NADLANFIX/dashboard.py) ל-API + UI ממשק של המוצר

שאם ייעשה נכון, NADLANFIX יהפוך ל-core platform מוכן לקצב יום-יומי ורחב יותר.
