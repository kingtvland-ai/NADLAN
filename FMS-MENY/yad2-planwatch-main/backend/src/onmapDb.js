// קורא את מודעות ONMAP ממאגר PlanWatch (data/planwatch.sqlite3) במקום
// מהמקור החיצוני 8099, שמחזיר רק ~38 רשומות בגלל ש-pagination מושבת
// בהגדרת ה-scraper. הגריפה עצמה (onmap_feed.py) כבר מאחסנת את כל מה
// שה-API הסכים לדפדף אליו — עד 4 מיון × $skip=3000 — כך שהמאגר מכיל
// את כל המודעות שהמקור חושף.
import { DatabaseSync } from 'node:sqlite';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

//: נתיב המאגר ניתן להחלפה דרך הסביבה; ברירת המחדל היא נתיב העבודה של
//: PlanWatch (נתיב יחסי מ-backend/src אל data/planwatch.sqlite3).
const DB_PATH = process.env.PLANWATCH_DB_PATH
  || path.resolve(__dirname, '../../data/planwatch.sqlite3');

let db = null;

function getDb() {
  if (!db) db = new DatabaseSync(DB_PATH, { readOnly: true });
  return db;
}

/**
 * טוען את כל רשומות ONMAP הפעילות מהמאגר בפורמט הגולמי שהנורמלייזר
 * של localListings יודע להמיר (אותם raw rows שהגריפה שמרה ב-raw_json).
 *
 * @param {{ page?: number, limit?: number }} options
 * @returns {{ data: object[], record_count: number }}
 */
export function loadOnmapRows({ page, limit } = {}) {
  const conditions = ['delisted_at IS NULL'];
  const params = [];

  // המאגר ממוין כבר מהחדש לישן; limit/page אופציונליים (ברירת המחדל: הכל).
  let sql = 'SELECT raw_json FROM onmap_listings';
  if (conditions.length) sql += ` WHERE ${conditions.join(' AND ')}`;
  sql += ' ORDER BY created_at DESC';
  if (Number(limit) > 0) {
    sql += ' LIMIT ?';
    params.push(Number(limit));
  }
  if (Number(page) > 1 && Number(limit) > 0) {
    sql += ' OFFSET ?';
    params.push((Number(page) - 1) * Number(limit));
  }

  const rows = getDb().prepare(sql).all(...params);
  const data = [];
  for (const row of rows) {
    try {
      const parsed = JSON.parse(row.raw_json);
      if (parsed && typeof parsed === 'object') data.push(parsed);
    } catch {
      // רשומה פגומה במאגר — מדלגים עליה בלי להפיל את כל הבקשה.
    }
  }
  return { data, record_count: data.length };
}