// Shared Israeli locality list + matcher, used by both bots for the "manual
// city entry" step. This is not the full Central Bureau of Statistics list
// of 1,200+ localities (kibbutzim/moshavim with a handful of residents would
// bloat the picker without ever appearing in the listing feeds) - it is the
// ~150 cities/towns actually worth a button or a suggestion. Free text is
// never restricted to this list: whatever the user types is still sent to
// /api/combined-sale-listings as-is (see api.js#searchListings), exactly as
// before this file existed. This list only powers buttons and "did you mean".
// Names use spaces, never hyphens, even where the colloquial/Wikipedia
// spelling has one ("תל אביב-יפו", "יהוד-מונוסון") - confirmed live against
// this app's own data: /api/combined-sale-listings and /api/opportunities
// both do a LIKE-based substring match against government-sourced locality
// columns that are spelled with spaces, so the hyphenated form silently
// undercounts (תל אביב-יפו: 608 rows) against the space form (תל אביב יפו:
// 6,909 rows) or misses entirely (יהוד-מונוסון: 34 vs יהוד מונוסון: 203;
// on the renewal endpoint, מודיעין-מכבים-רעות/תל אביב-יפו return 0 both).
export const ISRAEL_CITIES = [
  'ירושלים', 'תל אביב יפו', 'חיפה', 'ראשון לציון', 'פתח תקווה', 'אשדוד', 'נתניה', 'באר שבע',
  'בני ברק', 'חולון', 'רמת גן', 'אשקלון', 'רחובות', 'בת ים', 'בית שמש', 'כפר סבא',
  'הרצליה', 'חדרה', 'מודיעין מכבים רעות', 'נצרת', 'לוד', 'רמלה', 'רעננה', 'נהריה',
  'הוד השרון', 'גבעתיים', 'קריית אתא', 'אילת', 'עפולה', 'נס ציונה', 'רמת השרון', 'כרמיאל',
  'קריית גת', 'טבריה', 'קריית ביאליק', 'קריית ים', 'קריית מוצקין', 'אור יהודה', 'צפת', 'נתיבות',
  'דימונה', 'טירת כרמל', 'מעלה אדומים', 'אריאל', 'שדרות', 'יבנה', 'אום אל פחם', 'טייבה',
  'טירה', 'באקה אל גרביה', 'סח\'נין', 'נוף הגליל', 'עכו', 'קריית שמונה', 'קריית מלאכי', 'אופקים',
  'מגדל העמק', 'יקנעם עילית', 'בית שאן', 'ג\'לג\'וליה', 'כפר קאסם', 'רהט', 'ערד', 'מצפה רמון',
  'זכרון יעקב', 'פרדס חנה כרכור', 'אבן יהודה', 'כפר יונה', 'גני תקווה', 'אלעד', 'מודיעין עילית',
  'ביתר עילית', 'גבעת שמואל', 'שהם', 'יהוד מונוסון', 'אור עקיבא', 'נשר', 'טמרה', 'שפרעם',
  'כפר יאסיף', 'ג\'דיידה מכר', 'אבו סנאן', 'קלנסווה', 'כפר ברא', 'מעלות תרשיחא', 'שלומי',
  'ראש פינה', 'חצור הגלילית', 'קצרין', 'מבשרת ציון', 'גבעת זאב', 'אפרת', 'הר אדר', 'להבים',
  'עומר', 'ירוחם', 'כוכב יאיר', 'צור הדסה', 'קדימה צורן', 'פרדסיה', 'קריית עקרון', 'גדרה',
  'באר יעקב', 'מזכרת בתיה', 'גן יבנה', 'דבוריה', 'כפר מנדא', 'כאבול', 'עראבה', 'דיר אל אסד',
  'מג\'אר', 'כפר כנא', 'יפיע', 'ריינה', 'בועיינה נוג\'ידאת', 'אבו גוש', 'ג\'סר אלזרקא', 'פוריידיס',
  'חורפיש', 'פקיעין', 'ג\'וליס', 'מג\'דל שמס', 'תל שבע', 'חורה', 'לקיה', 'כסייפה', 'שגב שלום',
  'קרני שומרון', 'אלקנה', 'עמנואל', 'ראש העין', 'בנימינה גבעת עדה', 'עתלית', 'קריית טבעון',
  'יבנאל', 'קריית ארבע',
];

function normalize(str) {
  return String(str || '')
    .trim()
    .replace(/["'׳״]/g, '')
    .replace(/[-\s]+/g, ' ')
    .toLowerCase();
}

function levenshtein(a, b) {
  const m = a.length;
  const n = b.length;
  if (!m) return n;
  if (!n) return m;
  const row = new Array(n + 1);
  for (let j = 0; j <= n; j++) row[j] = j;
  for (let i = 1; i <= m; i++) {
    let prev = row[0];
    row[0] = i;
    for (let j = 1; j <= n; j++) {
      const tmp = row[j];
      row[j] = a[i - 1] === b[j - 1]
        ? prev
        : 1 + Math.min(prev, row[j], row[j - 1]);
      prev = tmp;
    }
  }
  return row[n];
}

// Returns up to `max` {city, score} candidates from ISRAEL_CITIES ranked by
// relevance to `query`: exact match (0), then "starts with"/"is a prefix
// of" (1), then substring either way (2), then closest edit-distance for
// plain typos (3+). Lower is better. Used both to auto-resolve a confident
// typed city and to build a short "did you mean" list when it is not
// confident - see conversation.js#resolveCityText.
export function matchCitiesScored(query, max = 5) {
  const q = normalize(query);
  if (!q) return [];
  const scored = ISRAEL_CITIES.map((city) => {
    const c = normalize(city);
    let score;
    if (c === q) score = 0;
    else if (c.startsWith(q) || q.startsWith(c)) score = 1;
    else if (c.includes(q) || q.includes(c)) score = 2;
    else score = 3 + levenshtein(c, q) / Math.max(c.length, q.length, 1);
    return { city, score };
  });
  scored.sort((a, b) => a.score - b.score);
  return scored.filter((s) => s.score <= 3.5).slice(0, max);
}

// Label-only convenience wrapper kept for callers that don't need scores.
export function matchCities(query, max = 5) {
  return matchCitiesScored(query, max).map((s) => s.city);
}

// The handful of cities shown as first-tap buttons before the user types
// anything - the busiest markets in the listing feeds.
export const QUICK_CITIES = ['תל אביב יפו', 'ירושלים', 'חיפה', 'ראשון לציון', 'באר שבע', 'נתניה', 'פתח תקווה', 'אשדוד'];
