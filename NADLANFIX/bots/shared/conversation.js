// Platform-agnostic conversation flow, shared by the Telegram and WhatsApp
// bots so the question order, filter values and button labels can't drift
// between the two. Three modes, chosen first:
//   listings -> city -> source -> rooms -> page size -> results (paged)
//   renewal  -> city -> results (paged) - ranked urban-renewal complexes
//   parcel   -> type גוש/חלקה -> one-off dossier (map link, plans, permits)
// Each bot only has to translate the {prompt, options} shape returned here
// into its own native rendering (inline_keyboard for Telegram, numbered
// plain text for WhatsApp) and call back into `handleInput`.
import { QUICK_CITIES, matchCitiesScored } from './cities.js';
import { parseGushHelka } from './parcel.js';
import {
  SOURCES, ROOM_RANGES, RESULTS_PER_PAGE, RESULTS_PAGE_SIZES,
  searchListings, formatRow,
  searchRenewalOpportunities, formatRenewalRow, RENEWAL_RESULTS_PER_PAGE,
  lookupParcel, formatParcelSummary,
} from './api.js';

export function modeStep() {
  return {
    prompt: '🏡 מה תרצו לחפש היום?\n\n'
      + '🏠 נכסים למכירה — מודעות מכל האתרים (יד2, על המפה ועוד) במקום אחד, כולל מחיר, חדרים ופרטי קשר\n'
      + '🏗 הזדמנויות התחדשות עירונית — מתחמי פינוי-בינוי/תמ"א, מדורגים לפי כדאיות\n'
      + '📍 חיפוש לפי גוש/חלקה — כל מה שידוע על חלקה ספציפית: תוכניות, היתרי בנייה, וקישור למפה',
    options: [
      { id: 'MODE::listings', label: '🏠 נכסים למכירה' },
      { id: 'MODE::renewal', label: '🏗 הזדמנויות התחדשות עירונית' },
      { id: 'MODE::parcel', label: '📍 חיפוש לפי גוש/חלקה' },
    ],
  };
}

// Kept as the module's original entry point name so bot.js's /start
// handling and imports don't need to change - it's now "the first step",
// which happens to be the mode picker rather than the city picker.
export const cityStep = modeStep;

function localityStep(mode) {
  const options = QUICK_CITIES.map((city) => ({ id: `CITY::${city}`, label: city }));
  options.push({ id: 'CITY::__all__', label: '🌍 כל הארץ' });
  options.push({ id: 'CITY::__other__', label: '🔎 עיר אחרת (הקלד שם)' });
  const prompt = mode === 'renewal'
    ? '🏙 באיזו עיר לחפש הזדמנויות התחדשות עירונית?\n\nאפשר לבחור מהרשימה, או להקליד שם עיר בכל שלב - כל הערים בישראל נתמכות, גם אם הן לא ברשימה.'
    : '🏙 באיזו עיר לחפש?\n\nאפשר לבחור מהרשימה, או להקליד שם עיר בכל שלב - כל הערים בישראל נתמכות, גם אם הן לא ברשימה.';
  return { prompt, options };
}

export function sourceStep() {
  const options = SOURCES.map((s, i) => ({ id: `SOURCE::${i}`, label: s.label }));
  return { prompt: '📡 מאיזה מקור לחפש?\n\nאפשר לבחור אתר ספציפי, או "כל המקורות" כדי לראות את כולם יחד, ללא כפילויות.', options };
}

export function roomsStep() {
  const options = ROOM_RANGES.map((r, i) => ({ id: `ROOMS::${i}`, label: r.label }));
  return { prompt: '🛏 כמה חדרים מעניינים אתכם?', options };
}

export function resultsCountStep() {
  const options = RESULTS_PAGE_SIZES.map((n, i) => ({ id: `PAGESIZE::${i}`, label: `${n} תוצאות` }));
  return { prompt: '🔢 כמה תוצאות להציג בכל פעם?\n\nתמיד אפשר לבקש עוד בהמשך, או לשנות את זה שוב.', options };
}

function parcelTextStep() {
  return {
    prompt: '📍 הקלידו גוש וחלקה - למשל: "גוש 6941 חלקה 23", או פשוט "6941/23".\n\n'
      + 'אשלוף עבורכם תוכניות שחלות על החלקה, היתרי בנייה בסביבה, הזדמנויות התחדשות עירונית קרובות, וקישור למפה. החיפוש עשוי לקחת עד 15 שניות - הוא בודק כמה מקורות רשמיים בו-זמנית.',
    options: [],
  };
}

function resultsFooterOptions(session, total, pageSize) {
  const options = [];
  if (session.offset + pageSize < total) {
    options.push({ id: 'ACTION::more', label: `➕ עוד ${pageSize} תוצאות` });
  }
  if (session.mode === 'listings') options.push({ id: 'ACTION::pagesize', label: '🔢 שנה כמות תוצאות' });
  options.push({ id: 'ACTION::filters', label: session.mode === 'renewal' ? '🔧 שנה עיר' : '🔧 שנה מסננים' });
  options.push({ id: 'ACTION::new', label: '🔁 חיפוש חדש' });
  return options;
}

function summarizeFilters(session) {
  const bits = [
    session.locality || 'כל הארץ',
    session.source ? (SOURCES.find((s) => s.value === session.source)?.label || session.source) : null,
    session.minRooms || session.maxRooms
      ? (ROOM_RANGES.find((r) => r.min === session.minRooms && r.max === session.maxRooms)?.label
         || `${session.minRooms || '0'}-${session.maxRooms || '∞'} חדרים`)
      : null,
  ].filter(Boolean);
  return bits.join(' · ');
}

// Runs the sale-listings search for the session's current filters. Each row
// is cross-referenced against dashboard.py's peer/market analytics (discount
// vs. asking price, rank score, gross yield, days on market) - see
// formatRow in api.js.
export async function runSearch(session) {
  try {
    const { rows, total } = await searchListings({
      locality: session.locality, source: session.source,
      minRooms: session.minRooms, maxRooms: session.maxRooms,
      offset: session.offset, limit: session.pageSize,
    });
    session.step = 'results';
    if (!rows.length) {
      return {
        prompt: `😕 לא נמצאו תוצאות עבור: ${summarizeFilters(session)}.\nאפשר לנסות עיר אחרת או להרחיב את המסננים.`,
        options: [{ id: 'ACTION::filters', label: '🔧 שנה מסננים' }, { id: 'ACTION::new', label: '🔁 חיפוש חדש' }],
      };
    }
    const header = `📋 ${total} תוצאות עבור: ${summarizeFilters(session)}\nמציג ${session.offset + 1}-${session.offset + rows.length}:`;
    const body = rows.map((row, i) => formatRow(row, session.offset + i)).join('\n\n');
    return { prompt: `${header}\n\n${body}`, options: resultsFooterOptions(session, total, session.pageSize) };
  } catch (err) {
    console.error('search failed:', err.message);
    return {
      prompt: '⚠️ החיפוש נכשל כרגע. נסו שוב בעוד רגע.',
      options: [{ id: 'ACTION::filters', label: '🔧 שנה מסננים' }, { id: 'ACTION::new', label: '🔁 חיפוש חדש' }],
    };
  }
}

// Ranked urban-renewal complexes for the session's chosen city - the same
// model the webapp's "הזדמנויות התחדשות" tab uses.
export async function runRenewalSearch(session) {
  try {
    const { rows, total, hasRenewal, note } = await searchRenewalOpportunities({
      locality: session.locality, offset: session.offset, limit: RENEWAL_RESULTS_PER_PAGE,
    });
    session.step = 'results';
    if (!hasRenewal) {
      return {
        prompt: `ℹ️ ${note || 'מאגר ההתחדשות העירונית לא זמין כרגע.'}`,
        options: [{ id: 'ACTION::new', label: '🔁 חיפוש חדש' }],
      };
    }
    if (!rows.length) {
      return {
        prompt: `😕 לא נמצאו מתחמי התחדשות עבור: ${session.locality || 'כל הארץ'}.`,
        options: [{ id: 'ACTION::filters', label: '🔧 שנה עיר' }, { id: 'ACTION::new', label: '🔁 חיפוש חדש' }],
      };
    }
    const header = `🏗 ${total} מתחמי התחדשות עבור: ${session.locality || 'כל הארץ'}\nמציג ${session.offset + 1}-${session.offset + rows.length}, ממוינים לפי ציון הזדמנות (הכי כדאי קודם):`;
    const body = rows.map((row, i) => formatRenewalRow(row, session.offset + i)).join('\n\n');
    return { prompt: `${header}\n\n${body}`, options: resultsFooterOptions(session, total, RENEWAL_RESULTS_PER_PAGE) };
  } catch (err) {
    console.error('renewal search failed:', err.message);
    return {
      prompt: '⚠️ החיפוש נכשל כרגע. נסו שוב בעוד רגע.',
      options: [{ id: 'ACTION::filters', label: '🔧 שנה עיר' }, { id: 'ACTION::new', label: '🔁 חיפוש חדש' }],
    };
  }
}

// One-off parcel dossier. This call routinely takes 8-15s (GovMap/Mavat/
// appraisal/tender/municipal lookups) - `onInterim`, when given, is awaited
// with a "searching" message *before* the request goes out, so the user
// isn't staring at silence.
export async function runParcelLookup(session, gush, helka, onInterim) {
  session.gush = gush;
  session.helka = helka;
  if (onInterim) await onInterim('🔎 מחפש נתוני גוש/חלקה... זה עשוי לקחת עד 15 שניות.');
  try {
    const data = await lookupParcel({ gush, helka });
    session.step = 'results';
    return {
      prompt: formatParcelSummary(data),
      options: [{ id: 'ACTION::filters', label: '🔧 חפש גוש/חלקה אחר' }, { id: 'ACTION::new', label: '🔁 חיפוש חדש' }],
    };
  } catch (err) {
    console.error('parcel lookup failed:', err.message);
    return {
      prompt: `⚠️ לא הצלחתי למצוא נתונים לגוש ${gush} חלקה ${helka}. ${err.message || ''}`.trim(),
      options: [{ id: 'ACTION::filters', label: '🔧 נסו גוש/חלקה אחר' }, { id: 'ACTION::new', label: '🔁 חיפוש חדש' }],
    };
  }
}

// Resolves free-typed text into a city. A confident match - exact, or a
// clear prefix/substring with no close runner-up (e.g. typing "תל אביב"
// against the curated "תל אביב-יפו") - resolves immediately instead of
// asking "did you mean?" for the single most common way people actually
// type that city's name. Only a genuine tie or a fuzzy/typo-distance match
// triggers the confirm list. Free text never has to be in the curated list
// at all - an unmatched name is still sent to the API as-is (see cities.js).
export function resolveCityText(text) {
  const trimmed = (text || '').trim();
  if (!trimmed) return null;
  const scored = matchCitiesScored(trimmed, 5);
  if (!scored.length) return { resolved: trimmed };
  const [top, second] = scored;
  const confident = top.score <= 1 && (!second || second.score - top.score >= 1.5);
  if (confident || scored.length === 1) return { resolved: top.city };
  return {
    suggestions: {
      prompt: `🤔 לא בטוח/ה איזו עיר התכוונת אליה - "${trimmed}"? אפשר גם לבחור "חפש בדיוק" אם זו העיר הנכונה.`,
      options: [
        ...scored.map((s) => ({ id: `CITY::${s.city}`, label: s.city })),
        { id: `CITY::${trimmed}`, label: `חפש בדיוק "${trimmed}"` },
      ],
    },
  };
}

function resetFilters(session) {
  Object.assign(session, {
    step: 'mode', mode: '', locality: '', source: '', minRooms: '', maxRooms: '',
    pageSize: session.pageSize || RESULTS_PER_PAGE, offset: 0, gush: '', helka: '',
  });
}

// Central reducer: given the raw selection id (from a button tap or a
// WhatsApp numbered reply already mapped back to one - see
// bots/whatsapp/bot.js#resolveIncomingToken) or free text, mutates `session`
// and returns the next {prompt, options} to show. `onInterim`, if given, is
// called with a status string before a slow lookup (currently only the
// parcel dossier) - see runParcelLookup.
export async function handleInput(session, raw, { onInterim } = {}) {
  const text = (raw || '').trim();

  if (text === 'ACTION::new' || /^\/(start|menu|reset|help)/i.test(text) || /^(תפריט|התחל|עזרה|help)$/i.test(text)) {
    resetFilters(session);
    return modeStep();
  }
  if (text === 'ACTION::filters') {
    session.offset = 0;
    if (session.mode === 'parcel') {
      session.step = 'parcel_text';
      return parcelTextStep();
    }
    session.step = 'city';
    return localityStep(session.mode);
  }
  if (text === 'ACTION::pagesize') {
    session.step = 'pagesize';
    return resultsCountStep();
  }
  if (text === 'ACTION::more') {
    session.offset += session.mode === 'renewal' ? RENEWAL_RESULTS_PER_PAGE : session.pageSize;
    return session.mode === 'renewal' ? runRenewalSearch(session) : runSearch(session);
  }
  if (text.startsWith('MODE::')) {
    const mode = text.slice('MODE::'.length);
    session.mode = mode;
    session.offset = 0;
    if (mode === 'parcel') {
      session.step = 'parcel_text';
      return parcelTextStep();
    }
    session.step = 'city';
    return localityStep(mode);
  }
  if (text === 'CITY::__other__') {
    session.step = 'city_text';
    return { prompt: '🏙 הקלידו שם עיר - אפשר כל עיר, עיירה או יישוב בישראל:', options: [] };
  }
  if (text.startsWith('CITY::')) {
    const value = text.slice('CITY::'.length);
    session.locality = value === '__all__' ? '' : value;
    session.offset = 0;
    if (session.mode === 'renewal') return runRenewalSearch(session);
    session.step = 'source';
    return sourceStep();
  }
  if (text.startsWith('SOURCE::')) {
    const idx = Number(text.slice('SOURCE::'.length));
    session.source = SOURCES[idx]?.value ?? '';
    session.step = 'rooms';
    session.offset = 0;
    return roomsStep();
  }
  if (text.startsWith('ROOMS::')) {
    const idx = Number(text.slice('ROOMS::'.length));
    const range = ROOM_RANGES[idx] || ROOM_RANGES[0];
    session.minRooms = range.min;
    session.maxRooms = range.max;
    session.offset = 0;
    // First-time setup asks page size once; returning from "🔢 שנה כמות
    // תוצאות" mid-results only touches pageSize, so rooms never re-asks it.
    session.step = 'pagesize';
    return resultsCountStep();
  }
  if (text.startsWith('PAGESIZE::')) {
    const idx = Number(text.slice('PAGESIZE::'.length));
    session.pageSize = RESULTS_PAGE_SIZES[idx] || RESULTS_PER_PAGE;
    session.offset = 0;
    return runSearch(session);
  }

  // Plain typed text: interpret in context of the current step/mode.
  if (session.mode === 'parcel' && session.step !== 'results') session.step = 'parcel_text';
  if (session.step === 'parcel_text') {
    const parsed = parseGushHelka(text);
    if (!parsed) {
      return {
        prompt: '🤔 לא הצלחתי לזהות גוש/חלקה בטקסט הזה. נסו שוב, למשל: "גוש 6941 חלקה 23".',
        options: [],
      };
    }
    return runParcelLookup(session, parsed.gush, parsed.helka, onInterim);
  }
  if (!session.mode) return modeStep();
  if (session.step === 'city' || session.step === 'city_text' || session.step === 'results') {
    const resolution = resolveCityText(text);
    if (!resolution) return localityStep(session.mode);
    if (resolution.suggestions) {
      session.step = 'city_text';
      return resolution.suggestions;
    }
    session.locality = resolution.resolved;
    session.offset = 0;
    if (session.mode === 'renewal') return runRenewalSearch(session);
    session.step = 'source';
    return sourceStep();
  }
  // Any other step in listings mode: treat stray text as "start over with
  // this as the city".
  Object.assign(session, { step: 'source', locality: text, source: '', minRooms: '', maxRooms: '', offset: 0 });
  return sourceStep();
}
