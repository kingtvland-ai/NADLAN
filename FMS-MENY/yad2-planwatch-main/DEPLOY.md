# העלאה לאוויר — מדריך מלא

> מדריך לפריסת PlanWatch מחוץ למחשב הזה. נכתב מול המערכת כפי שהיא ב-11/08/2026,
> לא מול תבנית כללית. כל מספר כאן נמדד, וכל קטע קוד שמופיע כאן קיים כקובץ אמיתי
> בריפו ונבדק — לא רק תועד.

---

## הסטאק — מה בעצם צריך לרוץ

שני תהליכים, שתי שפות, **בלי web framework ובלי DB server**, שנבחרו לרוץ
**באותו קונטיינר** במצב ענן. כל מספר כאן נמדד על ההתקנה הזאת.

### שני השירותים

| # | שירות | קובץ כניסה | פורט | מה זה |
|---|-------|-------------|------|--------|
| 1 | **PlanWatch** — הליבה | `dashboard.py` | **8000** | 93 endpoints + `dashboard.html`. **זו המערכת — היא מדברת עם הדפדפן ישירות.** |
| 2 | **Node — Yad2 scraper** | `backend/src/server.js` | **4000**, פנימי בלבד | Playwright עם פרופיל דפדפן מתמשך שעוקף את Radware ומגיע לפיד האמיתי של יד2 — **87,672 מודעות, כולל מוכרים פרטיים**, לעומת 14,445 בקרוסלת המודעות המקודמות. `dashboard.py` קורא לו שרת-לשרת (`yad2_feed.start_feed`), לא הדפדפן |

`frontend/` (React/Vite) **נגנז** ל-`archive/frontend/` — ראה "מה השתנה" בתחתית
המסמך. `dashboard.html` הוא הממשק היחיד, ואף אחד לא צריך יותר להריץ npm כדי
להעלות את המערכת לאוויר.

### Python — הליבה

| | |
|---|---|
| **גרסה** | **3.14.3**. במחשב הזה מותקן גם 3.13 אבל **בלי התלויות** — `C:\Python314\python.exe` הוא זה שמריץ. `python` לבדו אינו ב-PATH |
| **שרת HTTP** | `http.server.ThreadingHTTPServer` — **ספרייה סטנדרטית**. אין Flask, אין FastAPI, אין Django, אין gunicorn |
| **מסד** | **SQLite 3.50.4** דרך `sqlite3` מהסטנדרטית. WAL + `synchronous=NORMAL`. **50 טבלאות, 702 MB** |
| **היקף** | **67 מודולים, 26,558 שורות** בשורש הפרויקט |

**תלויות חיצוניות — ארבע בסך הכול** (`requirements.txt`):

| חבילה | מותקן | למה | קריטי? |
|--------|--------|-----|---------|
| `requests` | 2.32.5 | כל תקשורת HTTP, דרך `http_client.build_session()` | **כן** |
| `shapely` | 2.1.2 | point-in-polygon: הצלבת תכניות, `parcel_at_point`, STRtree | **כן** — בלעדיו שכבת התכנון נופלת |
| `pypdf` | לא מותקן כאן | קריאת PDF של הכרעות שמאות בלבד | לא — מיובא lazily, שאר המערכת עובדת |
| `playwright` (Python) | 1.58.0 | לא בשימוש בקוד הזה — שריד סביבה | לא |

`urllib3` (2.6.3) ו-`certifi` (2026.2.25) נגררים עם `requests`.

> **`shapely` דורש GEOS.** ב-`pip` על Windows זה מגיע ב-wheel; ב-`Dockerfile`
> (שורש הריפו) הוא מותקן דרך `libgeos-dev`. בלעדיו `import shapely` נכשל,
> ו-`listing_planning` / `parcel_at_point` מדווחים ״שכבה לא נטענה״ בשקט.

### Node — משמש רק את ה-scraper

| | |
|---|---|
| **גרסה מקומית** | v24.19.0, ESM (`"type": "module"`) — ב-Docker: Node 22 LTS |
| **תלויות** | `express` 4.19 · `cors` 2.8 · `node-cache` 5.1 · `playwright` (JS) ^1.47 |

**אין לו יותר תפקיד של פרוקסי לדפדפן.** `dashboard.html` פונה ל-Python בלבד
(same-origin, 8000) ולא ל-4000 בכלל — נבדק: אין באף מקום בקובץ קריאה לפורט
4000 או ל-`/api/yad2-feed` / `/api/local-listings` / `/api/integrations`,
שהם ה-endpoints היחידים ש-Node חושף. הצריכה היחידה שלו כיום היא
**שרת-לשרת**: `yad2_feed.start_feed()` ב-Python קורא ל-`127.0.0.1:4000`
כדי להפעיל את הקציר.

### תזמון ורקע

- **15 jobs** ב-`scheduler.py`, לכל אחד מרווח משלו (6 שעות עד שבוע).
- **מקומי** (`PLANWATCH_DEPLOY` לא מוגדר): thread ברקע בתוך `dashboard.py`
  כל 12 שעות, בדיוק כמו עד היום. `--no-scheduler` מכבה.
- **ענן** (`PLANWATCH_DEPLOY=render`): ה-thread **כבוי אוטומטית** —
  Render Cron קורא ל-`POST /api/scheduler/run` באותו קצב במקום. ראה
  "מצב פריסה" למטה ו-§5.

### מה **אין** בסטאק, ולמה זה חשוב לפריסה

| אין | המשמעות |
|-----|----------|
| DB server (Postgres/MySQL) | אין connection string — יש **קובץ**. **§1.3.** |
| Redis / broker | מטמון הוא dict בזיכרון התהליך; restart מאבד אותו (~100 שניות לבנייה מחדש) |
| CI / טסטים אוטומטיים | אין `pytest` בריפו — הבדיקות ידניות |
| ORM / migrations | `db.py` מריץ DDL ומיגרציות ידניות בכל `get_conn()` |

### להרים מקומית

```powershell
# הליבה — זה כל מה שצריך ברוב המקרים
& C:\Python314\python.exe dashboard.py            # http://127.0.0.1:8000

# Node — רק כשצריך קציר יד2 עם מודעות פרטיות
& "C:\Program Files\nodejs\node.exe" backend\src\server.js
```

> **`python` ו-`node` אינם ב-PATH כאן** — צריך את הנתיב המלא, אחרת מקבלים
> ‏`The term 'python' is not recognized`. `run-all.ps1` בשורש מרים את שניהם.

### מצב פריסה — דגל סביבה אחד, לא שתי ענפים בקוד

`dashboard.py` קורא משתנה סביבה **אחד**, `PLANWATCH_DEPLOY`, וקובע לפיו שלושה
דברים בבת אחת — ראה §3 לפרטים ולבדיקות שהריצו על כל אחד מהם:

| `PLANWATCH_DEPLOY` | `--host` ברירת מחדל | scheduler thread | אימות |
|---|---|---|---|
| **לא מוגדר** (ברירת מחדל — Tailscale/מקומי) | `127.0.0.1` | דלוק | אופציונלי — פעיל רק אם `PLANWATCH_BASIC_AUTH` הוגדר ידנית |
| **`render`** | `0.0.0.0` | כבוי (Render Cron מחליף) | **חובה** — `main()` מסרב לעלות בלי `PLANWATCH_BASIC_AUTH` |

זו לא בחירה חד-פעמית בזמן פיתוח — אותו קוד עולה בשני המצבים, וההבדל הוא
משתנה סביבה אחד שמוגדר בכל סביבה בנפרד.

### תרשים

```
                            הדפדפן
                               │
                               ▼
                    dashboard.html (Vanilla + Leaflet)
                               │  same-origin, פורט אחד
                               ▼
                    Python :8000  dashboard.py
                    stdlib ThreadingHTTPServer · 93 endpoints
                               │
              ┌────────────────┼────────────────┬──────────────────┐
              ▼                ▼                 ▼                  ▼
      SQLite 702 MB      http_client        scheduler         Node :4000
      50 טבלאות          (requests+TLS)     15 jobs           (שרת-לשרת בלבד,
              │                │                                start_feed)
              │     GovMap · מינהל התכנון · למ״ס                    │
              │     בנק ישראל · data.gov.il · ONMAP           Playwright + פרופיל
              ▼                                                דפדפן מתמשך → יד2
  real_estate.db (405 MB, קריאה בלבד, מחוץ לריפו)
```

---

## 0. תקציר ההחלטה

**המלצה: אל תפרוס את זה כאתר ציבורי כברירת מחדל. פרוס אותו כמערכת פרטית.**

לא מטעמי זהירות מופרזת — משלוש סיבות קונקרטיות שמפורטות ב-§1, והחזקה שבהן היא
ש**המאגר מכיל מידע אישי של אנשים פרטיים** שלא נתנו הסכמה. הדרך המהירה, הזולה
והנכונה היא מצב **מקומי / Tailscale** (~15 דקות, 0 ₪).

אם בכל זאת צריך URL ציבורי — מצב **`render`**, ורק אחרי ש-§3 (אימות, כבר בנוי
ונבדק) ו-§4 (מידע אישי) הוכרעו.

| מצב | מתאים ל | עלות/חודש | זמן הקמה | חושף מידע אישי? |
|-------|----------|-----------|-----------|------------------|
| **מקומי — Tailscale** | אתה, שותף, צוות קטן | **0 ₪** | ~15 דק׳ | לא |
| **`render`** | לקוחות, גישה מכל מקום | **~$7-8** (הפריט התשלומי היחיד בכל הארכיטקטורה) | 2–4 שעות | רק למי שנכנס |

---

## 1. שלושה חסמים אמיתיים — לקרוא לפני שמפעילים `PLANWATCH_DEPLOY=render`

### 1.1 אימות — נבנה, אבל הוא בררני ולא אוטומטי

`dashboard.py` הוא `ThreadingHTTPServer` מהספרייה הסטנדרטית. הוא **כן** יודע
לדרוש Basic Auth היום — §3 מתאר את המימוש שכבר קיים ונבדק — אבל זה קורה **רק**
כש-`PLANWATCH_DEPLOY=render` (שאז זה חובה ו-`main()` מסרב לעלות בלעדיו) או
כש-`PLANWATCH_BASIC_AUTH` הוגדר ידנית. **בלי אחד משני אלה, השרת פתוח לגמרי** —
בדיוק כמו תמיד, כולל נקודות הקצה שכותבות (`POST /api/tabu/extract`,
`/api/scheduler/run`, `/api/yad2/harvest`). זה בסדר גמור במצב מקומי; זו טעות
חמורה אם מישהו מפעיל `--host 0.0.0.0` על כתובת נגישה מבחוץ בלי לוודא שאחד
מהשניים מוגדר.

### 1.2 המאגר הוא אנשים, לא רק מספרים

נמדד עכשיו על `data/planwatch.sqlite3`:

| מה | כמה |
|----|-----|
| טלפונים של מפרסמים (ONMAP) | **2,829** |
| שמות מפרסמים | **2,803** |
| רמזי בעלי זכויות על חלקות ספציפיות | **30,515** |
| הערות ליד שנשמרו | לפי שימוש |

חלק ניכר מהם **אנשים פרטיים**. פרסום של הצלבה בין שם, טלפון, כתובת וגוש/חלקה
בכתובת ציבורית הוא לא רק סיכון עסקי — בישראל זה נכנס לתחולת **חוק הגנת הפרטיות
ותקנות אבטחת מידע (2017)**, ואם יש משתמשים באיחוד האירופי גם GDPR. `.gitignore`
של הפרויקט כבר חוסם את `data/` מהריפו מהסיבה הזאת בדיוק, ובצדק.

**מה לעשות:** §4.

### 1.3 מערכת קבצים ארעית מול 1.1 ג׳יגה של מאגרים

| קובץ | גודל | מיקום |
|------|------|--------|
| `data/planwatch.sqlite3` | **702 MB** | בתוך הפרויקט |
| `planwatch.sqlite3-wal` | 25 MB | לידו |
| `real_estate.db` (אדס · מדל״ן · קומו + 285,535 עסקאות) | **405 MB** | **מחוץ לריפו**: `PROJECT-CITY\real_estate_scraper\...` |
| `onmap.json` (הגדרת scraper + cookies) | קטן | **מחוץ לריפו**: `PROJECT-CITY\מאתר-API\curl2api\scrapers\` |

**ב-Render, Railway, Fly ו-Heroku מערכת הקבצים נמחקת בכל deploy ובכל restart.**
בלי דיסק מתמיד — כל מה שנגרף נעלם בפריסה הבאה. שני הקבצים האחרונים גם לא
קיימים בענן בכלל, כי הם מחוץ לריפו — ראה §7 למה זה משפיע ומה לא.

---

## 2. מצב מקומי / Tailscale (**מומלץ**)

הרעיון: לא להעלות כלום. להשאיר את המערכת רצה על המחשב, ולתת לעצמך (ולמי
שתבחר) גישה מכל מקום דרך רשת פרטית מוצפנת. `PLANWATCH_DEPLOY` נשאר לא מוגדר —
זו בדיוק ההתנהגות הקיימת, ללא שינוי.

**למה זה הפתרון הנכון כאן:** הוא פותר את שלושת החסמים בבת אחת. אין חשיפה
ציבורית, אין העברת מידע אישי לשרת של מישהו אחר, ואין בעיית דיסק — המאגר נשאר
במקום שבו הוא כבר יושב.

### הקמה

```powershell
# 1. התקנה במחשב הזה
winget install tailscale.tailscale
tailscale up

# 2. הרצת השירותים כך שיאזינו לכל הממשקים ולא רק ל-localhost
& C:\Python314\python.exe dashboard.py --host 0.0.0.0
```

מהטלפון/מחשב אחר: להתקין Tailscale, להתחבר לאותו חשבון, ולפתוח
`http://<machine-name>:8000`.

> **`--host 0.0.0.0` בלי Tailscale = חשיפה לכל הרשת המקומית.** רק אחרי
> ש-Tailscale פעיל, ורצוי עם חומת האש של Windows חוסמת 8000 מבחוץ:
> ```powershell
> New-NetFirewallRule -DisplayName "PlanWatch local only" -Direction Inbound `
>   -LocalPort 8000 -Protocol TCP -Action Block -RemoteAddress Internet
> ```

### שיתוף עם עוד אדם

```powershell
tailscale share            # או דרך admin console: Users -> Invite
```

**עלות: 0 ₪** (Tailscale חינם עד 100 מכשירים / 3 משתמשים).

### להשאיר את זה רץ תמיד

```powershell
# משימה מתוזמנת שמריצה את הדשבורד בהפעלת המחשב
schtasks /create /tn "PlanWatch" /sc onstart /ru $env:USERNAME /rl HIGHEST `
  /tr "C:\Python314\python.exe F:\AI-STUDIO-BUILDER-APP\YAD2\YAD2\dashboard.py --host 0.0.0.0"
```

---

## 3. שכבת אימות — ממומשת, נבדקה, ומופעלת ע"י דגל אחד

`dashboard.py` נושא היום `_authorised()` ו-`_require_auth()` על `Handler`
(Basic Auth, `hmac.compare_digest` ולא `==` — השוואת מחרוזות רגילה נעצרת בתו
הראשון שנבדל, וההפרש בזמן מאפשר לנחש סיסמה תו-תו מול endpoint ציבורי). תלות
חדשה: **אפס** — `hmac` ו-`base64` הן ספרייה סטנדרטית.

**מה מוגן:** כל נתיב, כולל `/` וכל `/api/*` — קריאה וכתיבה כאחד. **מה פטור:**
`OPTIONS` (preflight, לא נושא credentials) ו-`GET /api/health` (חייב להישאר
פתוח כדי ש-Render יוכל לבדוק שהתהליך חי; מדווח רק `{"ok": true}`, בלי לגעת
במאגר).

**מתי זה חובה:** `PLANWATCH_DEPLOY=render` — ו-`main()` **מסרב לעלות** בלי
`PLANWATCH_BASIC_AUTH` מוגדר, exit code 2, עם הודעה שמפנה לכאן. לא ניתן
להעלות שירות ציבורי בטעות בלי הגנה.

**מתי זה אופציונלי:** בכל מצב אחר — אם מגדירים `PLANWATCH_BASIC_AUTH` ידנית
(גם ב-Tailscale), השער פעיל בכל זאת, כהגנה נוספת למי שרוצה.

### נבדק בפועל

```bash
# מצב render מדומה, מקומי, על פורט חלופי:
PLANWATCH_DEPLOY=render PLANWATCH_BASIC_AUTH=admin:test123 \
  python dashboard.py --port 8001 --no-browser
```

| בקשה | ציפייה | תוצאה בפועל |
|------|--------|--------------|
| `GET /api/health` בלי credentials | 200 | ✅ 200 |
| `GET /api/data-version` בלי credentials | 401 | ✅ 401 |
| `GET /api/data-version` עם `-u admin:test123` | 200 | ✅ 200 |
| `POST /api/scheduler/run` בלי credentials | 401 | ✅ 401 |
| `GET /` בלי credentials | 401 | ✅ 401 |
| הרצה עם `PLANWATCH_DEPLOY=render` וללא `PLANWATCH_BASIC_AUTH` | סירוב לעלות, exit 2 | ✅ בדיוק כך |

הבדיקה השלישית (POST חסום) היא הקריטית: GET מוגן ו-POST פתוח הוא בדיוק הפער
שמאפשר לזר להפעיל קציר או לכתוב למאגר.

### reverse proxy — אופציונלי, לא נדרש

מי שרוצה TLS על דומיין משלו ולא סומך רק על Basic Auth יכול להוסיף Caddy
מלפנים בלי לגעת ב-`dashboard.py`:

```
planwatch.example.com {
    basicauth {
        admin $2a$14$REPLACE_WITH_YOUR_HASH   # caddy hash-password
    }
    reverse_proxy 127.0.0.1:8000
}
```

זה כפל הגנה מעל מה שכבר קיים בקוד, לא תחליף לו — `PLANWATCH_BASIC_AUTH` נשאר
נדרש במצב `render` בכל מקרה.

---

## 4. מידע אישי — להחליט לפני הפריסה, לא אחריה

שתי אפשרויות לגיטימיות. **בחר אחת במפורש:**

### 4.1 לפרוס בלי המידע האישי (הכי בטוח)

סקריפט שמייצר עותק "נקי" לפריסה:

```python
# scrub_for_deploy.py
import shutil, sqlite3
shutil.copy("data/planwatch.sqlite3", "data/planwatch-public.sqlite3")
c = sqlite3.connect("data/planwatch-public.sqlite3")
c.executescript("""
    UPDATE onmap_listings SET phone = NULL, contact_name = NULL;
    DELETE FROM yad2_listing_contacts;
    DROP TABLE IF EXISTS parcel_owners;
    DELETE FROM listing_lead_notes;
""")
c.execute("VACUUM")
c.close()
```

המערכת ממשיכה לעבוד: הטלפונים הם עמודה אחת מתוך עשרות, וכל שאר הניקוד
(`value_gap`, `gross_yield`, `market_momentum`, תכנון, בנצ׳מרקים) לא נוגע בהם.

### 4.2 לפרוס עם המידע, מאחורי אימות, ולתעד

אם המערכת היא כלי פנימי לצוות מזוהה — זה סביר, בתנאי ש:
- אימות מ-§3 פעיל (`PLANWATCH_BASIC_AUTH` מוגדר — חובה ממילא ב-`render`),
- יש רישום מי ניגש למה (`credentialed_audit` כבר קיים במאגר),
- קיים נוהל מחיקה לפי בקשה,
- יש בסיס חוקי לעיבוד (אינטרס לגיטימי מתועד).

---

## 5. Render — מוכן להרצה, שלב אחר שלב

מה יש בריפו כבר, ולא רק בתיעוד: **`Dockerfile`**, **`docker-entrypoint.sh`**,
**`Dockerfile.cron`**, **`render.yaml`** — ארבעתם בשורש הפרויקט.

### 5.1 קונטיינר אחד, שני תהליכים — למה לא שני Render services

Node קיים מסיבה אחת: `backend/src/scraper.js` מחזיק פרופיל Chromium מתמשך
שעוקף את בדיקת הבוט של Radware ומגיע לפיד האמיתי של יד2 — 87,672 מודעות,
כולל מוכרים פרטיים. המנגנון הזה לא נכתב מחדש ב-Python: פרופיל דפדפן מאומת
ומתמשך הוא בדיוק הדבר שזול לתחזק ויקר לשחזר, והסיכון בשכתוב לא קונה כאן כלום.

**Render מחבר דיסק מתמיד לשירות אחד בלבד.** שני web services לא יכולים לחלוק
את `planwatch.sqlite3` בין Python לבין Node בענן, בזמן שמקומית הם כבר חולקים
אותו כשני תהליכי OS נפרדים (`PYTHON_API_BASE` מקשר ביניהם). הפתרון: **אותה
טופולוגיה בדיוק, בקונטיינר אחד** — `docker-entrypoint.sh` מריץ את Node ברקע
ואז `exec python dashboard.py`, כך ש-Python הוא PID 1 ומקבל SIGTERM כראוי
מ-Render. קריסה של Node לא מפילה את Python — כל מה שמבוסס על נתונים שמורים
(כל מסך הנכסים, תיקי חלקה, כל ניקוד) ממשיך לעבוד; רק קציר *חדש* של מודעות
פרטיות ייכשל, ובבירור (״Needs the Node service up״), לא בשקט.

`Dockerfile` מתקין Node 22 LTS דרך NodeSource לצד `libgeos-dev` (ל-shapely),
ומריץ `npx playwright install --with-deps chromium` — הפקודה הזאת מתקינה גם
את הדפדפן וגם את כל ספריות ה-OS שהוא צריך, בזכות הרשאת root בזמן build.

### 5.2 render.yaml — שני שירותים, לא שלושה

```yaml
services:
  - type: web
    name: planwatch
    env: docker
    dockerfilePath: ./Dockerfile
    plan: starter
    healthCheckPath: /api/health
    disk:
      name: planwatch-data
      mountPath: /data
      sizeGB: 5
    envVars:
      - key: PLANWATCH_DB
        value: /data/planwatch.sqlite3
      - key: PLANWATCH_DEPLOY
        value: render
      - key: PLANWATCH_BASIC_AUTH
        sync: false               # מוגדר ידנית בלוח הבקרה, לא בריפו

  - type: cron
    name: planwatch-scheduled-refresh
    env: docker
    dockerfilePath: ./Dockerfile.cron
    schedule: "0 */12 * * *"      # תואם ל-AUTO_CYCLE_HOURS ב-dashboard.py
    envVars:
      - key: PLANWATCH_URL
        value: https://planwatch.onrender.com   # להחליף בכתובת האמיתית
      - key: PLANWATCH_BASIC_AUTH
        sync: false               # חייב להיות זהה לערך של השירות הראשי
```

**התיקון החשוב מול טיוטה קודמת של הקובץ הזה:** גרסה מוקדמת נתנה ל-cron job
משלו `disk:` עם אותו `name:` כמו ה-web service, מתוך הנחה ש״אותו שם = אותו
דיסק״. **זה לא נכון ב-Render** — זה יוצר דיסק שני, ריק, נפרד. `dockerCommand`
שמריץ `scheduler.py` ישירות על הדיסק הזה היה כותב למקום שאף אחד לא קורא ממנו,
בלי שגיאה גלויה.

**הפתרון:** `Dockerfile.cron` הוא image זעיר (Alpine + curl, בלי Python
ובלי Node) שרק **קורא ל-`POST /api/scheduler/run` על ה-web service הרץ** —
אותו endpoint שכפתור ״הרץ עכשיו״ בממשק קורא לו. השירות הרץ הוא זה שמחזיק את
הדיסק האמיתי פתוח, אז זה הוא זה שכותב. כל job עדיין בודק את המרווח שלו לבד —
פינג שמגיע כשאין עבודה חוזר מיד בלי לעשות כלום.

**נבדק** מקומית (לא ב-Docker בפועל — אין Docker על המכונה הזאת, ראה §5.4 —
אבל הפקודה שה-cron מריץ נבדקה מילה במילה מול שרת אמיתי):

```bash
PLANWATCH_URL="http://127.0.0.1:8004" PLANWATCH_BASIC_AUTH="admin:test123" \
  sh -c 'curl -fsS -X POST -u "$PLANWATCH_BASIC_AUTH" "$PLANWATCH_URL/api/scheduler/run"'
# → {"started": true, "only": null, "force": false}

# בלי credentials:
sh -c 'curl -fsS -X POST "http://127.0.0.1:8004/api/scheduler/run"'
# → curl: (22) The requested URL returned error: 401
```

### 5.3 העלאת המאגר הראשוני

הקובץ 702 MB ולא בריפו (וטוב שכך). שתי דרכים:

**א. Render Shell** (הכי פשוט, דורש תוכנית בתשלום)
```bash
7z a planwatch.7z data/planwatch.sqlite3      # מקומי, ~200MB דחוס
# ואז ב-Render Shell:
curl -o /data/planwatch.7z "<presigned-url>" && 7z x /data/planwatch.7z -o/data
```

**ב. לבנות מחדש בענן** (איטי אך נקי — ואין העברת מידע אישי)
```bash
python sync.py --full && python scheduler.py --all
```

> **לפני העלאה: `PRAGMA wal_checkpoint(TRUNCATE)`.** קובץ ה-WAL הוא 25 MB
> של כתיבות שטרם מוזגו. העתקת ה-`.sqlite3` לבדו משאירה אותן מאחור.

### 5.4 מה לא נבדק, ובכנות

**לא רץ build אמיתי של ה-Dockerfile** — אין Docker מותקן על המכונה הזאת
בזמן כתיבת המדריך. מה שכן נבדק: תחביר `sh -n` על `docker-entrypoint.sh`,
פענוח YAML תקין של `render.yaml`, וכל פקודת ה-`curl`/Basic-Auth שהקונטיינרים
מריצים — מול שרת Python אמיתי, לא מדומה. הפער היחיד שנשאר הוא ה-build עצמו
(רזולוציית apt/npm/playwright בזמן build), וזה הצעד הראשון שכדאי להריץ באמת
לפני שמסתמכים על הקובץ: `docker build .` מקומי, אם יש Docker זמין, לפני
push ל-Render.

### 5.5 גבול אמיתי של SQLite בענן

Render מריץ **דיסק רשת**. SQLite מניח קובץ מקומי, ונעילות על NFS/EBS הן מקור
ידוע לשחיתות מאגר. עם קורא אחד ומעט כתיבות זה עובד — ובזכות §5.1/5.2 יש
בדיוק **שירות אחד** שנוגע בדיסק, לא שניים. אם צריך יותר ריכוזיות ממשתמשים
מקבילים בעתיד — זה הרגע לעבור ל-Postgres, וזו עבודה אמיתית: 50 טבלאות
ו-`db.py` שכתוב סביב תחביר SQLite.

---

## 6. שדרוגים עתידיים — לא חלק מהמסלול הקריטי

שני כלים שנשקלו במפורש ונדחו מה-MVP, לא מחוסר התאמה עקרונית אלא כי שום דבר
כאן לא צריך אותם עדיין:

### 6.1 Firebase Authentication — Phase 2, כשיש כמה משתמשים בעלי זהות אישית

Basic Auth (§3) מספיק לגמרי לסיסמה משותפת אחת. Firebase Auth שווה משהו נוסף
רק כשצריך **משתמשים נפרדים בזהות אישית** — למשל לתת לכל לקוח login משלו.
המתכון, כשיגיע הצורך:

- `firebase-admin` כתלות אופציונלית ב-`requirements.txt`.
- מודול חדש `auth_firebase.py`: מאמת Firebase ID token, מחליף/מרחיב את
  `Handler._authorised()` הקיים.
- תוספת קטנה ל-`dashboard.html`: Firebase JS SDK מ-CDN (כמו Leaflet כבר
  היום), מסך login שמצרף `Authorization: Bearer <token>` לכל קריאת `api()`.

**לא ממומש כרגע.** נכנס לפעולה רק כשיש צורך אמיתי במשתמשים נפרדים — לא לפני.

### 6.2 Cloudflare — שכבת CDN/דומיין אופציונלית

אם/כשיש דומיין: להצביע אותו ל-Render דרך Cloudflare (DNS + proxy, תוכנית
חינמית) — DDoS protection, caching, TLS. **אפס שינוי קוד.** לא נדרש כדי
שהמערכת תעבוד; `*.onrender.com` עובד מצוין בלעדיו.

### 6.3 מה שנבדק ונדחה לגמרי — ולמה

| כלי | נבדק מול | למה נגנז |
|-----|----------|----------|
| **MongoDB** | 50 טבלאות SQLite יחסיות, JOINs מרובים לפי גוש/חלקה, אינדוקס מרחבי (STRtree) | Atlas החינמי נותן 512MB; המאגר הקיים הוא 702MB **כבר היום**, לפני גדילה. שכתוב הליבה היחסית למסמכים הוא חודשים של עבודה בסיכון גבוה מול תועלת לא ברורה. |
| **Netlify** (ל-`dashboard.html`) | אירוח פרונט סטטי נפרד ללוח הבקרה הפנימי | אין artifact סטטי לאחסן — `dashboard.html` מוגש server-side מ-Python (`PAGE.read_bytes()` בכל בקשה), לא bundle שצריך CDN נפרד. `frontend/` (React) שהיה יכול להצדיק את זה נגנז — ראה למטה. **זה נשאר נכון ללוח הבקרה בלבד** — `webapp/` (§11) הוא כן artifact סטטי אמיתי, ושם Netlify כן הבחירה הנכונה. |

---

## 7. מה **לא** יעבוד בענן, ולמה

| רכיב | מצב בענן | הסיבה |
|------|----------|--------|
| מודעות יד2 **שכבר נאספו** (61,478 שורות ב-`yad2_listings`) | ✅ | נקראות מה-SQLite שעולה עם הדיסק. לא תלוי ברשת בזמן בקשה. |
| קציר **חדש** של הפיד האמיתי (מוכרים פרטיים, `start_feed`) | ✅ | **נפתר** — Node רץ בתוך אותו קונטיינר, ראה §5.1. |
| קציר **רגיל** (`yad2_feed.start`, יעד 18,000, סוכנויות בלבד) | ❌ | דורש את מחבר curl2api המקומי (8099/8100) — כלי חיצוני, מחוץ לריפו, לא סטנדרטי. לא הוגדר מסלול פריסה בשבילו. |
| `external_listings_view` (4,988 מודעות + 285,535 עסקאות) | ❌ | `real_estate.db` יושב ב-`PROJECT-CITY`, מחוץ לריפו. צריך להעלות 405 MB בנפרד או לוותר על המקור. **בלעדיו נופל גם ״עסקאות שבוצעו בסביבה״ במסך גוש/חלקה.** |
| `onmap_feed` / `onmap_rent` | ❌ בלי הגדרה | `onmap.json` עם ה-cookies מחוץ לריפו. בלעדיו אין קציר ואין תשואות. **אל תשים אותו בריפו** — הוא credential. משתנה סביבה או Secret File. |
| scheduler — 15 jobs | ✅ | `PLANWATCH_DEPLOY=render` מכבה את ה-thread אוטומטית; `Dockerfile.cron` מריץ את אותו מחזור מבחוץ. אין דגל שצריך לזכור. |
| אימות | ✅ | חובה ואכוף בקוד ב-`render`, לא הוראה בתיעוד. §3. |
| GovMap / חיפוש כתובת + קדסטר ארצי | ✅ | שני endpoints ציבוריים ללא טוקן: `es.govmap.gov.il/TldSearch` לכתובת ו-`ags.govmap.gov.il/Identify/IdentifyByXY` (POST) לגוש/חלקה. עובדים מכל מקום |
| בנק ישראל / למ״ס / data.gov.il | ✅ | הכול ציבורי |

---

## 8. עלויות — מספרים אמיתיים

| שירות | תוכנית | $/חודש |
|-------|--------|--------|
| Render Web (Python+Node, קונטיינר אחד + דיסק 5GB) | Starter | 7 + 1.25 |
| Render Cron (Alpine+curl, זעיר) | | 0 |
| Cloudflare (אופציונלי, DNS+CDN) | Free | 0 |
| Firebase Auth (אופציונלי, Phase 2) | Free עד 50k MAU | 0 |
| **סה״כ מצב `render`** | | **≈ $8.25 (30 ₪)** |
| **מצב מקומי (Tailscale)** | | **0** |

> **Render Free לא מתאים.** השירות נרדם אחרי 15 דקות, וההתעוררות לוקחת ~50
> שניות — על endpoint שהמאגר שלו נבנה ב-100 שניות קרות זה אומר בקשה ראשונה של
> שתי דקות וחצי. בנוסף, בתוכנית החינמית **אין דיסק מתמיד**, כלומר §1.3.

**חלופות שקולות:** Fly.io (volumes אמיתיים, נוח ל-SQLite, ~$5),
Hetzner CX22 (VPS מלא, €4, שליטה מלאה — הכי משתלם אם נוח לך עם Linux),
Railway (הכי פשוט, אך תמחור לפי שימוש שמפתיע).

---

## 9. לפני שלוחצים deploy — צ׳קליסט

```
[ ] PLANWATCH_DEPLOY=render מוגדר, PLANWATCH_BASIC_AUTH מוגדר בלוח הבקרה (לא בריפו)
[ ] הוחלט במפורש: 4.1 (נקי) או 4.2 (עם מידע, מתועד) למידע האישי
[ ] .gitignore מכבד: data/, .env, .browser-profile/, *.sqlite3
[ ] git log --stat | grep -i sqlite   → ריק (מאגר לא נכנס להיסטוריה)
[ ] onmap.json כ-Secret File, לא כקובץ בריפו
[ ] PRAGMA wal_checkpoint(TRUNCATE) לפני העתקת המאגר
[ ] docker build . מצליח מקומית לפני push (§5.4 — לא נבדק כאן, חסר Docker)
[ ] /api/health עונה 200 בלי credentials
[ ] /api/data-version ו-/api/scheduler/run (POST) מחזירים 401 בלי credentials
[ ] render.yaml: PLANWATCH_URL בשירות ה-cron מעודכן לכתובת האמיתית
[ ] גיבוי: המאגר בענן הוא עותק, לא המקור
```

### בדיקה שהאימות באמת תופס

```bash
curl -i https://your-app.onrender.com/api/health                # → 200, בלי credentials
curl -i https://your-app.onrender.com/api/data-version          # → 401
curl -i -u user:pass https://your-app.onrender.com/api/data-version  # → 200
curl -i -X POST https://your-app.onrender.com/api/scheduler/run # → 401 בלי credentials
```

הרביעית היא החשובה. GET מוגן ו-POST פתוח הוא בדיוק הפער שמאפשר לזר להפעיל
קציר או לכתוב למאגר.

---

## 10. אז מה לעשות עכשיו

1. **היום, 15 דקות:** מצב מקומי/Tailscale. `--host 0.0.0.0`, חוק חומת אש.
   מקבל גישה מכל מקום, אפס עלות, אפס חשיפה.
2. **כשיש לקוח:** §4 (החלטה על מידע אישי) → `docker build .` מקומי לוודא
   שהאימג׳ עולה → `PLANWATCH_DEPLOY=render` ב-Render, לפי §5.
3. **כשיש כמה משתמשים נפרדים:** §6.1, Firebase Auth.

הסדר הזה לא שרירותי: כל שלב בו מייצר משהו עובד, ואף שלב לא דורש להתחיל מחדש
את הקודם — ואותו קוד, לא ענף נפרד, רץ בכל אחד מהם.

---

## 11. אתר משתמשים (Netlify) + בוטים (Telegram/WhatsApp)

זה נפרד לגמרי מ-`dashboard.html` (§0-§10 למעלה) — אתר ציבורי חדש, `webapp/`,
לחיפוש נכסים בלבד, עם כניסה בשם משתמש/סיסמה שהמפעיל (אתה) מנפיק ידנית דרך
לשונית **״משתמשי האתר״** בלוח הבקרה הפנימי. לוח הבקרה עצמו לא משתנה — עדיין
Basic Auth יחיד, עדיין רק אתה נכנס.

### 11.1 שכבת האימות השנייה

`accounts.py` + טבלאות `users`/`sessions` חדשות ב-`db.py` מממשות מסלול אימות
שני, נפרד מ-`PLANWATCH_BASIC_AUTH`: `POST /api/user/login` מחזיר טוקן
(`Authorization: Bearer <token>`), נבדק ב-`_require_user_auth()`. שני קבוצות
נתיבים חדשות ב-`dashboard.py`:

- `PUBLIC_ROUTES` — בלי אימות בכלל (`/api/health`, `/api/user/login`).
- `USER_ROUTES` — חיפוש/רשימות (`/api/search`, `/api/combined-sale-listings`
  ועוד) — מקבלים **או** טוקן Bearer תקין (האתר הציבורי) **או** Basic Auth
  של המפעיל (הבוטים, שרצים על אותה מכונה ונושאים את `PLANWATCH_BASIC_AUTH`
  כמו כל תהליך פנימי אחר — לא צריכים חשבון משתמש קצה משלהם).

כל שאר ה-endpoints (כולל `/api/admin/users*` לניהול המשתמשים) נשארים מוגנים
ב-Basic Auth הרגיל, בלי שינוי.

### 11.2 פריסת `webapp/` ל-Netlify

`webapp/` הוא HTML/CSS/JS טהור, בלי שלב build — גרירה ישירה ל-Netlify
(או `netlify deploy`) מספיקה. **לפני הפריסה, לערוך `webapp/config.js`**:

```js
window.PLANWATCH_CONFIG = {
  API_BASE: "https://planwatch.onrender.com",  // הכתובת האמיתית מ-Render
  WHATSAPP_BOT_NUMBER: "",   // מתמלא אחרי 11.4
  TELEGRAM_BOT_USERNAME: "", // מתמלא אחרי 11.3
};
```

ה-API כבר פתוח ל-CORS מכל מקור (`Access-Control-Allow-Origin: *`,
`dashboard.py`), כך שאין צורך ב-redirect/proxy ב-Netlify — `netlify.toml`
בתיקייה רק מגדיר `publish = "."`.

**ליצור את המשתמש הראשון:** לוח הבקרה → לשונית ״משתמשי האתר״ → צור משתמש.
המשתמש מקבל את שם המשתמש/סיסמה ישירות ממך (אין הרשמה עצמית).

### 11.3 בוט Telegram (`bots/telegram`)

1. לפתוח שיחה עם **@BotFather** ב-Telegram, `/newbot`, לקבל טוקן.
2. `PLANWATCH_TELEGRAM_TOKEN` — ב-Render: sync:false, מוגדר ידנית בלוח
   הבקרה (כמו `PLANWATCH_BASIC_AUTH`). מקומית: ב-`.env`.
3. הבוט רץ כתהליך נוסף בתוך אותו קונטיינר — `docker-entrypoint.sh` מפעיל
   אותו ברקע, בדיוק כמו Node scraper. בלי טוקן הוא יוצא בשקט (exit 0),
   **לא** מפיל את הקונטיינר.
4. את שם המשתמש של הבוט (למשל `PlanWatchBot`) למלא ב-`TELEGRAM_BOT_USERNAME`
   ב-`webapp/config.js`.

### 11.4 בוט WhatsApp (`bots/whatsapp`) — Baileys, לא ה-API הרשמי

**החלטה מודעת של המפעיל:** ספריית לקוח לא-רשמית (`@whiskeysockets/baileys`),
מקושרת למספר ה-WhatsApp האישי שלך דרך סריקת QR חד-פעמית — לא Meta Business
API. זה מפר את תנאי השימוש של WhatsApp ומסתכן בחסימת המספר; ההחלטה התקבלה
במפורש מול הסיכון הזה.

**קישור ראשוני:**
1. לוודא ש-`WHATSAPP_AUTH_DIR=/data/whatsapp-auth` מוגדר (כבר ב-`render.yaml`
   — זה הדיסק המתמיד, אז לא צריך לסרוק מחדש אחרי כל restart).
2. אחרי שהקונטיינר עולה, לפתוח `https://<your-app>.onrender.com/api/admin/whatsapp-qr`
   בדפדפן (נדרש Basic Auth הרגיל של לוח הבקרה) — מציג את קוד ה-QR הנוכחי.
3. לסרוק עם WhatsApp בטלפון (הגדרות ← מכשירים מקושרים). אחרי חיבור מוצלח
   ה-QR נמחק אוטומטית והנתיב מחזיר 404 עד שיהיה צורך בקישור מחדש (למשל אחרי
   ניתוק ידני מהטלפון).
4. את מספר הטלפון (ספרות בלבד, כולל קידומת מדינה — למשל `972501234567`)
   למלא ב-`WHATSAPP_BOT_NUMBER` ב-`webapp/config.js`.

**נבדק מקומית:** התחברות אמיתית ל-WhatsApp, יצירת QR PNG תקין, והגשתו דרך
`/api/admin/whatsapp-qr` (200 עם ה-PNG הנכון, 401 בלי Basic Auth, 404 לפני
שקיים QR) — כל אלה נבדקו מול תהליכים רצים בפועל, לא רק נקראו.

### 11.5 מה שני הבוטים חולקים

שניהם קוראים ל-API הפנימי (`http://127.0.0.1:8000` דרך `PYTHON_API_BASE`)
עם `PLANWATCH_BASIC_AUTH`, בדיוק כמו Node scraper — לא ה-webapp, לא טוקן
משתמש קצה. חיפוש מבוצע דרך `/api/combined-sale-listings?locality=...`,
ומוחזרות עד 5 תוצאות פורמטיות (כתובת, מחיר, חדרים, שטח, קישור למודעה).
קריסה של אחד מהם לא מפילה את הקונטיינר — אותו דפוס בדיוק כמו Node ב-§5.1.

---

## מה השתנה במעבר הזה

צמצום סטאק מכוון (Netlify/Render/MongoDB/Cloudflare/Firebase → תפקיד אחד
לכל כלי שבאמת נדרש):

- **`dashboard.py`** קיבל `PLANWATCH_DEPLOY`, `_authorised()`/`_require_auth()`
  ו-fail-fast על אימות חסר ב-`render` — ממומש ונבדק, לא רק מתועד.
- **`Dockerfile`**, **`docker-entrypoint.sh`**, **`Dockerfile.cron`**,
  **`render.yaml`** — ארבעה קבצים חדשים בשורש הפרויקט.
- **`frontend/`** (React/Vite) עבר ל-`archive/frontend/`. `dashboard.html`
  הוא הממשק היחיד.
- **MongoDB ו-Netlify** נדחו במפורש — הנימוקים ב-§6.3, **אבל רק ביחס
  ל-`dashboard.html`**. §11 מוסיף `webapp/` — אתר משתמשים סטטי אמיתי,
  ושם Netlify כן הבחירה הנכונה — ושני בוטים (Telegram, WhatsApp/Baileys)
  שרצים כתהליכים נוספים באותו קונטיינר.
