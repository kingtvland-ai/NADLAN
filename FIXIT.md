# פרומפט ביצוע — איחוד NADLAN לתוך NADLANFIX (Local Scraper + Firebase + Netlify)

> נכתב אחרי סריקת קוד בפועל (git clone + grep + diff), לא לפי FIXIT.md/LIST.md שבריפו —
> קבצים אלה מכילים ניתוח שגוי בחלקים קריטיים (למשל טוענים ש-`yad2_feed.py`/`facebook_feed.py`
> יושבים ב-root של NADLANFIX — הם לא, הם תחת `ingestion/feeds/`). התייחס לניתוח למטה כמקור אמת.

## 0. מה המטרה בפועל

- NADLANFIX הופך למוצר היחיד. FMS-MENY/yad2-planwatch-main ו-FacebookMScrap מתמזגים לתוכו ונעלמים כפרויקטים עצמאיים.
- הסקרייפינג (Yad2 + Facebook, ולפי הצורך OnMap) **רץ מקומית על מחשב אחד** (לא ב-Render, לא בקונטיינר), מושך **את כל הנתונים** (כל הנכסים ביד2 לפי ערים, כל הנכסים בפייסבוק לפי עיר) ודוחף אותם ל-**Firebase (Firestore)**.
- ה-Frontend (Netlify) והבוטים קוראים רק מ-Firestore/API — אף פעם לא פוגעים ישירות ב-Yad2/Facebook.
- Render, Docker, Litestream, ה-Node-in-container topology — כולם מיועדים לביטול. הריצה המקומית פותרת את הבעיה שבגללה כל זה נבנה מלכתחילה (עוד על זה בסעיף 2).

## 1. מה קיים בפועל בקוד (לא בתיעוד) — ממצאים

### 1.1 יש שתי מערכות מקבילות בתוך NADLANFIX, ורק אחת מהן רצה בפועל

**המערכת החיה (production):**
- Entry point אמיתי: `docker-entrypoint.sh` → `python dashboard.py` (קובץ ה-root, **223KB**, מונוליט).
- `dashboard.py` (root) עובד מול `db.py` (root, `import db` — לא `storage/sqlite_local.py`) ומול `ingestion/feeds/yad2_feed.py`, שקורא ל-Node service (`backend/src/server.js` → `backend/src/scraper.js`) ב-`127.0.0.1:4000`.
- `scraper.js` מפעיל **Chrome אמיתי (לא Chromium מובנה), headed, דרך Xvfb**, עם פרופיל דפדפן persistent — כי גישה ל-`gw.yad2.co.il/realestate-feed` (ה-feed הפרטי, **87,672 שורות** לעומת 14,445 בקרוסלת המודעות הממומנות) חוסמת headless עם Radware bot-check. זו הסיבה שהלוגיקה הזו מעולם לא הומרה לפייתון.
- יש scheduler בתוך `dashboard.py` עצמו (`AUTO_CYCLE_HOURS`, thread פנימי) — זה מה שרץ בפועל בפרודקשן, מגובה ע"י cron חיצוני שרק מכה ב-`POST /api/scheduler/run`.
- יש עוד scheduler נפרד ב-root: `scheduler.py` (19KB) — זה **דומיין אחר לגמרי**: pipeline של plans/appraisals/owners/index/grids/alerts (National planning data), לא סקרייפינג מודעות. לא קשור ל-yad2_feed/facebook_feed.
- Litestream (לא Firestore!) עושה replication רציף של ה-SQLite ל-GCS bucket — זה מנגנון גיבוי, לא sync תפעולי ל-Firestore.
- שני בוטים (Telegram, WhatsApp) קוראים ל-Python API על `127.0.0.1:8000` עם Basic Auth — לא ל-DB ישירות, לא ל-Firestore.
- Frontend חי: `webapp/` (סטטי, מוגש ע"י Netlify כבר היום — `webapp/netlify.toml` קיים ומצביע על Render כ-API backend דרך `config.js`).

**המערכת המתה (new architecture, לא מחוברת לכלום):**
- `sources/{yad2,facebook,onmap}/adapter.py` + `sources/base/adapter.py` — ממשק אחיד יפה (`fetch/normalize/dedupe/score/persist/export_latest`), אבל:
- `jobs/scheduler.py` (`UnifiedScheduler`) + `jobs/daily_ingest.py` — קיימים, עובדים ב-standalone (`python jobs/daily_ingest.py run --all`), **אבל לא מוזכרים ב-`docker-entrypoint.sh` ולא נקראים משום מקום אחר**. יעדי Nachal לדוגמה: `yad2: target 18000` — מול 87K+ בפועל בפרודקשן. מספרים לא מכוילים למציאות.
- `storage/firestore_sync.py` (466 שורות, `sync_queue` table + retry) — קוד firestore **אמיתי וסביר**, אבל נקרא רק מ-`storage/active_db.py` ומ-`_test_audit.py`. שום job לא קורא לו.
- `storage/active_db.py` — abstraction שקוראת מ-Firestore עם fallback ל-SQLite — גם הוא לא מחובר ל-`dashboard.py` ולא ל-`app/api/routes.py`.
- `requirements.txt`: **אין בו `google-cloud-firestore` בכלל.** כלומר גם אם היו קוראים לקוד הזה, ההתקנה הייתה נכשלת (יש `try/except ImportError` שמאפשר לזה "לא לקרוס", אבל בפועל = feature כבוי).
- `public-site/` — frontend שני, שלם (search/map/planning/transactions/opportunities html + js/api.js), קורא ל-`/api`, **אין לו netlify.toml**, לא ברור אם זה בכלל בשימוש מול `webapp/`.
- `archive/frontend/` — frontend שלישי (React+Vite, 212K), כבר מסומן כ-archive על ידי מי שיצר את זה. תשאיר בארכיון.
- `app/crm/*`, `app/auth/rbac.py`, `app/api/*` — שכבת CRM/auth חדשה, גם היא לא מחוברת ל-`dashboard.py`.

### 1.2 Facebook — תלות חוצה-פרויקטים אמיתית (לא רק תיאורטית)

ב-`ingestion/feeds/facebook_feed.py` יש קוד סקרייפינג אמיתי (Playwright, browser context persistent, `sync_playwright`) — הלוגיקה הליבתית **כן** קיימת ב-NADLANFIX ולא רק ב-FacebookMScrap. אבל:
- שורה 48-52: session state נטען מ-`data/facebook_storage_state.json`, עם קומנט מפורש שהקובץ הזה נוצר ע"י `fb-market session login` **בפרויקט FacebookMScrap** (`src/facebook_marketplace_scraper/cli.py` / `browser.py`). כלומר: ה-login/auth bootstrap flow לא הועתק לתוך NADLANFIX, רק ה-consumption של קובץ session מוכן.
- `ingestion/normalization/facebook_{metadata,comparables,scoring,valuation}.py` — כולם עם קומנט "Ported from FacebookMScrap's X.py" — כלומר כבר בוצע port חלקי, ולא רק "יש להעביר", כמו שנטען ב-FIXIT.md.

### 1.3 השוואת "מה חסר" מול "מה מיותר" (בפועל, לא לפי FIXIT.md)

מה **שכן** צריך לעבור מ-FacebookMScrap ל-NADLANFIX ועדיין לא עבר:
- ה-login/session-bootstrap flow (`browser.py`/`cli.py` ב-FacebookMScrap) — כרגע NADLANFIX מניח שקובץ session כבר קיים, ואין לו דרך משלו ליצור/לרענן אותו.
- `israel_cities.py` / `city_resolver.py` — worth checking אם הרשימה/הלוגיקה ב-`facebook_feed.py` כבר שקולה, או אם יש city coverage טוב יותר ב-FacebookMScrap שצריך לאמץ בשביל "כל הערים".

מה **שכבר קיים כפול** ב-NADLANFIX ולא צריך "להעביר" עוד פעם (FIXIT.md טעה בזה):
- `facebook_metadata/comparables/scoring/valuation` — כבר מפורטים (ported), לא stub.
- Yad2 harvester הליבה — כבר קיים ועובד ב-production (`yad2_feed.py` + Node scraper), לא צריך "ליצור source adapter מאפס", רק לחבר את מה שכבר קיים ל-Firestore.

## 2. למה "מחשב מקומי" זה בעצם פתרון, לא בעיה חדשה

ה-Docker/Xvfb/headed-Chrome topology כולו קיים **רק כדי** לדמות מסך אמיתי בענן בשביל לעבור את Radware. על מחשב מקומי יש כבר מסך אמיתי — Xvfb, ה-container, ה-disk-sharing hack ב-render.yaml (Python+Node על דיסק אחד כי Render disks לא משותפים בין שני services), וה-basic-auth-over-loopback — כל אלה נופלים אוטומטית. זו לא רק אופציה נוחה, זו הסרה נטו של המורכבות שהניעה את כל ה-Dockerfile.

## 3. תכנית עבודה — לפי סדר ביצוע, ממוקדת קוד

### שלב A — בחירת "מנצח" אחד לכל שכבה (לא לבנות עוד, לבחור ולמחוק)

לכל אחד מהזוגות/שלשות הבאים תבחר **מקור אמת אחד** ותעביר את השאר ל-`archive/` (או תמחק אם אין ערך היסטורי):

| שכבה | קיים כרגע | להחליט |
|---|---|---|
| DB layer | root `db.py` (מחובר לכל dashboard.py) מול `storage/sqlite_local.py` (לא מחובר) | לבחור אחד; קרוב לוודאי `db.py` כי הוא זה שמריץ בפועל, ואז למזג פנימה כל schema-addition ש-`sqlite_local.py`/`storage/*` הביאו (snapshots, schema_versioning) |
| Scheduler | `dashboard.py`'s internal thread + `scheduler.py` (root, national-data pipeline) + `jobs/scheduler.py` (unused) | לבחור scheduler אחד ל-listing ingestion (recommend: `jobs/scheduler.py`+`jobs/daily_ingest.py`, אחרי שמכיילים targets למספרים אמיתיים ומחברים בפועל), ולהשאיר את root `scheduler.py` נפרד — הוא domain אחר (planning data), לא מתחרה |
| Frontend | `webapp/` (חי, ב-Netlify כבר) מול `public-site/` (שלם אך לא deployed) מול `archive/frontend/` (כבר archived) | לבחור אחד ל-production על Netlify. אם `public-site/` יותר מלא/עדכני מבחינת features — הוא זוכה וממזגים לתוכו כל מה ש-`webapp/` יש ואין בו; אחרת ההפך |
| Firestore access | `storage/firestore_sync.py` (כתוב, לא מחובר) מול `storage/active_db.py` (כתוב, לא מחובר) | שניהם נשארים, אבל צריך לחבר בפועל (סעיף B) |

### שלב B — לחבר את מה שכבר כתוב ולא מופעל

1. הוסף ל-`requirements.txt`: `google-cloud-firestore` (ואם צריך גם `firebase-admin`).
2. קבע קובץ קונפיג אחד ל-service-account credentials (env var, לא בקוד/git).
3. ב-`jobs/daily_ingest.py` / `jobs/scheduler.py`: אחרי `persist()` לכל source, קרוא ל-`FirestoreSync` (מ-`storage/firestore_sync.py`) כדי לדחוף batch upsert. עדכן את `DEFAULT_SOURCES` targets למספרים אמיתיים (yad2 ~87K+14K, לא 18000; facebook — קבע יעד ריאלי לפי מה שסקירה מקומית תעשה).
4. ודא ש-`jobs/daily_ingest.py` בפועל קורא ל-Yad2/Facebook adapters שמפעילים את הלוגיקה האמיתית שכבר קיימת (`ingestion/feeds/yad2_feed.py`, `ingestion/feeds/facebook_feed.py`) — לא ליצור מימוש מקביל שלישי בתוך `sources/*/adapter.py`. אם ה-adapters היום רק "עוטפים" (wrap) את ה-feeds הקיימים — תוודא שהעטיפה שלמה (fetch מפעיל את כל ה-pagination/כל הערים, לא subset).
5. `storage/active_db.py`: לחבר ל-`app/api/routes.py` (או ל-endpoint ש-`webapp`/`public-site` בפועל קוראים לו) כדי שקריאות client יעברו דרך Firestore-first, לא SQLite ישירות.

### שלב C — הפיכת הסקרייפינג ל-"רץ מקומית בלבד"

1. הסר את התלות ב-Xvfb/headed-Chrome-in-container: על מחשב מקומי `scraper.js` יכול לרוץ `headed` ישירות (יש מסך אמיתי) — תעדכן קונפיג כדי לא לדרוש `DISPLAY=:99` מלאכותי כשרצים מקומית.
2. תפריד את `backend/src/server.js` (Node/Playwright) מהצורך לרוץ בתוך אותו קונטיינר כמו Python — מקומית שני התהליכים (Python על 8000, Node על 4000) יכולים לרוץ כשני processes רגילים (למשל דרך `run-all.ps1`/סקריפט bash מקביל ל-Mac/Linux, או `pm2`/`concurrently`), בלי Docker בכלל.
3. Facebook session bootstrap: להעביר את ה-login flow (מ-FacebookMScrap's `browser.py`/`cli.py`) פנימה ל-NADLANFIX כ-`ingestion/feeds/facebook_session.py` (או דומה) עם פקודת CLI ברורה ליצירת/רענון `data/facebook_storage_state.json` — כרגע NADLANFIX סתם *צורך* session מוכן, ואין לו איך ליצור אחד בעצמו.
4. תוודא ש-yad2/facebook feeds תומכים ב-iteration על **כל הערים** (לא city יחיד/בדיקה) — תבדוק את מנגנון ה-city list הקיים ב-`facebook_feed.py`/`israel_cities.py` (יש כזה גם ב-FacebookMScrap: `city_resolver.py`, `ARIM-SAFOT.csv`) מול הרשימה שכבר קיימת ב-NADLANFIX, ותוודא שאין city coverage חסר.
5. תחליף/תבטל Litestream (הוא רלוונטי רק ל-Render/GCS-backup-of-SQLite topology) — אם רוצים גיבוי ל-SQLite המקומי, זה יכול פשוט להיות גיבוי קובץ מקומי/cron פשוט; הוא לא קשור ל-Firestore sync.

### שלב D — הסרת Render/Docker (רק אחרי ש-A-C עובדים end-to-end מקומית)

1. `render.yaml`, `Dockerfile`, `Dockerfile.cron`, `docker-entrypoint.sh` → להעביר ל-`archive/deploy-render/` (לא למחוק לגמרי, לתעד "זה היה הפריסה הישנה").
2. Frontend (`webapp/` או `public-site/`, לפי מה שנבחר בשלב A) → Netlify. `config.js`/`API_BASE` צריך לזוז מ-Render URL ל-endpoint חדש (אם עדיין צריך API חי כלשהו — לרוב Netlify Functions קטנה שקוראת ל-Firestore, או client-side Firestore SDK ישירות).
3. Telegram/WhatsApp bots: כרגע תלויים ב-`PYTHON_API_BASE=http://127.0.0.1:8000` — אחרי המעבר, הם צריכים לקרוא ל-Firestore (או ל-Netlify function) במקום ל-Python service מקומי, **אלא אם** הבוטים גם הם רצים על אותו מחשב מקומי לצמיתות (לבדוק עם המשתמש מה מתוכנן).

### שלב E — ניקיון

1. לוודא ש-`sources/*/adapter.py` ו-`jobs/*` בפועל *הם* ה-production path, לא קוד מת נוסף — או למזג לגמרי לתוך `dashboard.py`'s flow, או להפוך אותם למחליף מלא של `dashboard.py`'s internal thread (לא שניהם בו-זמנית).
2. `app/crm/*`, `app/auth/rbac.py` — אם לא בשימוש היום, לתעד כ"מוכן לעתיד" ולא לחבר בכוח רק כדי "לסגור" אותו.
3. FMS-MENY ו-FacebookMScrap כתיקיות עצמאיות בריפו — אחרי שכל מה שנחוץ מהן אומת כמועבר (סעיף 1.2 + city coverage), להעביר את שתיהן ל-`archive/source-projects/` עם README קצר שמסביר שהלוגיקה חיה עכשיו ב-NADLANFIX.

## 4. קריטריוני קבלה (Definition of Done)

- [ ] `git clone` נקי + הרצה על מחשב מקומי (לא Render, לא Docker) מריצה scrape מלא ל-Yad2 (כל הערים, לא subset) בלי xvfb/headless-workarounds מיותרים
- [ ] אותו דבר ל-Facebook, כולל יכולת ליצור/לרענן session בעצמו (לא תלות בפרויקט אחר)
- [ ] יעדי ingestion (`DEFAULT_SOURCES` וכו') תואמים למספרים אמיתיים, לא placeholder
- [ ] נתונים בפועל נדחפים ל-Firestore (batch upsert, לא polling מה-client)
- [ ] Frontend אחד בלבד (לא שניים חיים במקביל) רץ על Netlify וקורא ל-Firestore/API, לא ישירות ל-Yad2/Facebook
- [ ] אין הפניה חיה ל-Render/Docker/Litestream בקוד הפעיל (מועבר לארכיון בלבד)
- [ ] FMS-MENY ו-FacebookMScrap מסומנים כארכיון, לא תלות runtime
- [ ] תיעוד README אחד עדכני שמתאר את הטופולוגיה החדשה בפועל (לא LIST.md הישנים — אלה כדאי למחוק או לסמן כ-superseded אחרי שהפרומפט הזה בוצע)