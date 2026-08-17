# NADLANFIX Public Site

אתר ציבורי למשתמשי NADLANFIX - פלטפורמת נדל"ן חכמה.

## מבנה האתר

```
public-site/
├── index.html          # דף התחברות
├── search.html         # חיפוש נכס (עיר, רחוב, מספר)
├── categories.html     # בחירת קטגוריה (4 כפתורים)
├── transactions.html   # כל העסקאות עם סינון
├── opportunities.html  # 15 הזדמנויות מובילות
├── map.html           # מפה אינטראקטיבית
├── planning.html      # תכנון ו-GIS
├── css/
│   └── style.css      # עיצוב ראשי
└── js/
    ├── api.js         # שכבת API
    └── app.js         # לוגיקת האפליקציה
```

## זרימת המשתמש

1. **התחברות** (`index.html`) - שם משתמש וסיסמה
2. **חיפוש** (`search.html`) - בחירת עיר, רחוב, מספר
3. **קטגוריות** (`categories.html`) - 4 כפתורים לבחירה
4. **דף קטגוריה** - תוצאות לפי בחירה

## קטגוריות

- **כל העסקאות** - חיפוש כל המודעות עם סינון לפי מחיר, חדרים, שטח
- **הזדמנויות מובילות** - 15 הנכסים עם הניקוד הטוב ביותר
- **מפה** - תצוגת מפה עם סמני נכסים
- **תכנון ו-GIS** - תכניות בנייה ומידע תכנוני

## עיצוב

- עיצוב מודרני ואלגנטי
- פלטת צבעים: כחול כהה + זהב
- תמיכה מלאה בעברית (RTL)
- אנימציות חלקות ומעברים
- עיצוב רספונסיבי למובייל

## API

האתר מתחבר ל-`dashboard.py` של הפרויקט הראשי דרך ה-endpoints:
- `POST /api/user/login` - התחברות
- `GET /api/localities` - רשימת ערים
- `GET /api/combined-sale-listings` - כל העסקאות
- `GET /api/deals` - עסקאות מדורגות
- `GET /api/plans` - תכניות תכנון
- `GET /api/parcel-polygon` - פוליגון חלקה

## הפעלה

### מקומי

```bash
cd NADLANFIX/public-site
python -m http.server 8080
```

### Netlify

הפריסה ב-Netlify מתבצעת אוטומטית מהתיקייה `public-site/`. יש להגדיר ב-Netlify:
1. Build command: (ריק - אתר סטטי)
2. Publish directory: `public-site/`
3. Environment variable: `API_BASE` → כתובת ה-API של הפרויקט

ראה `netlify.toml` לקונפיגורציה מלאה.
