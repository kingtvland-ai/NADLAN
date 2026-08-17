// Shared PlanWatch API client for both bots - same trust model as before
// (server-side process on the same box, PLANWATCH_BASIC_AUTH over loopback,
// see dashboard.py's _require_user_auth). Extracted out of bot.js so the two
// bots stop carrying two copies of the same fetch/format logic.
const PYTHON_API_BASE = process.env.PYTHON_API_BASE || 'http://127.0.0.1:8000';
const BASIC_AUTH = process.env.PLANWATCH_BASIC_AUTH || '';

function authHeaders(extra = {}) {
  const headers = { ...extra };
  if (BASIC_AUTH) headers.Authorization = 'Basic ' + Buffer.from(BASIC_AUTH).toString('base64');
  return headers;
}

// Mirrors the source picker in webapp/index.html - "" (ALL) is what
// _combined_sale_listings in dashboard.py treats as "no filter, all three
// row sets". The value is what gets sent as ?source=; label is the button.
export const SOURCES = [
  { id: 'all', label: '🌐 כל המקורות', value: '' },
  { id: 'yad2', label: '🏷 יד 2', value: 'יד 2' },
  { id: 'onmap', label: '🗺 על המפה', value: 'על המפה' },
  { id: 'ad', label: '📣 AD', value: 'ad' },
  { id: 'madlan', label: '🏘 מדלן', value: 'madlan-authorized' },
  { id: 'komo', label: '🏡 קומו', value: 'komo' },
];

export const ROOM_RANGES = [
  { id: 'all', label: '♾️ כל כמות החדרים', min: '', max: '' },
  { id: '1-2', label: '🛏 1-2 חדרים', min: '1', max: '2' },
  { id: '3', label: '🛏 3 חדרים', min: '3', max: '3' },
  { id: '4', label: '🛏 4 חדרים', min: '4', max: '4' },
  { id: '5+', label: '🛏 5+ חדרים', min: '5', max: '' },
];

// Default page size when the user hasn't picked one yet - see
// conversation.js#resultsCountStep, which lets them switch to 10/20/50.
export const RESULTS_PER_PAGE = 5;
export const RESULTS_PAGE_SIZES = [5, 10, 20, 50];

export async function searchListings({ locality = '', source = '', minRooms = '', maxRooms = '', offset = 0, limit = RESULTS_PER_PAGE } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (locality) params.set('locality', locality);
  if (source) params.set('source', source);
  if (minRooms) params.set('min_rooms', minRooms);
  if (maxRooms) params.set('max_rooms', maxRooms);
  const res = await fetch(`${PYTHON_API_BASE}/api/combined-sale-listings?${params}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`PlanWatch API returned ${res.status}`);
  const data = await res.json();
  const rows = [
    ...(data.legacy_rows || []),
    ...(data.live_rows || []),
    ...(data.onmap_rows || []),
  ].slice(0, limit);
  return { rows, total: data.total ?? rows.length };
}

// Yad2/ONMAP rows carry city/sqm; the AD/Madlan/Komo (legacy) rows carry
// locality/area_m2 - three sources, two field names for the same two facts.
// The market-comparison fields (discount_pct, rank_score, gross_yield_pct,
// dom_days) come from dashboard.py's cross-referencing against peer listings
// and the CBS/BoI benchmarks (see listing_analytics.py/listing_yield.py) -
// the webapp already renders them as badges; the bots previously dropped
// them on the floor and showed address/price/rooms/area only.
export function formatRow(row, index) {
  const area = row.area_m2 ?? row.sqm ?? null;
  const bits = [
    row.address || row.neighborhood || row.locality || row.city || 'נכס',
    row.price ? `₪${Number(row.price).toLocaleString('he-IL')}` : null,
    row.rooms ? `${row.rooms} חדרים` : null,
    area ? `${area} מ"ר` : null,
  ].filter(Boolean);
  const numbering = typeof index === 'number' ? `${index + 1}. ` : '';
  let text = numbering + bits.join(' · ');

  const extras = [];
  if (row.discount_pct != null && row.discount_pct > 0) {
    extras.push(`🔥 ${Math.round(row.discount_pct)}% מתחת למחיר השוק`);
  }
  if (row.peer_gap_pct != null) {
    const sign = row.peer_gap_pct > 0 ? '+' : '';
    extras.push(`📊 פער מול נכסים דומים: ${sign}${Math.round(row.peer_gap_pct)}%`);
  }
  if (row.gross_yield_pct != null) {
    extras.push(`💰 תשואה גולמית: ${row.gross_yield_pct}%`);
  }
  if (row.dom_days != null) {
    extras.push(`⏱ ${row.dom_days} ימים בשוק`);
  }
  if (row.rank_score != null) {
    extras.push(`⭐ ציון: ${Math.round(row.rank_score)}`);
  }
  if (extras.length) text += `\n${extras.join(' · ')}`;
  // ONMAP rows publish the advertiser's phone directly on the listing (the
  // legacy AD/Madlan/Komo rows get one too, when dashboard.py's gallery
  // fetch already found it - see the "phones already learned" comment in
  // _combined_sale_listings). It was present in the API response all along;
  // formatRow simply never read it.
  if (row.phone) text += `\n📞 ${row.phone}`;
  if (row.url) text += `\n🔗 ${row.url}`;
  return text;
}

// Full parcel dossier - plans covering it, nearby renewal complexes, permit
// history. This hits GovMap/Mavat/appraisal/tender/municipal sources live
// (dashboard.py's _parcel), so it routinely takes 8-15s - see
// conversation.js#runParcelLookup, which sends an interim "searching"
// message before awaiting this.
export async function lookupParcel({ gush, helka }) {
  const params = new URLSearchParams({ gush, helka });
  const res = await fetch(`${PYTHON_API_BASE}/api/parcel?${params}`, { headers: authHeaders() });
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).error || ''; } catch { /* body wasn't JSON */ }
    throw new Error(detail || `PlanWatch API returned ${res.status}`);
  }
  return res.json();
}

export const RENEWAL_RESULTS_PER_PAGE = 5;

// Ranked urban-renewal complexes for a locality - the same ranking the
// webapp's "הזדמנויות התחדשות" tab shows (opportunity.renewal_opportunities).
export async function searchRenewalOpportunities({ locality = '', offset = 0, limit = RENEWAL_RESULTS_PER_PAGE } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (locality) params.set('locality', locality);
  const res = await fetch(`${PYTHON_API_BASE}/api/opportunities?${params}`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`PlanWatch API returned ${res.status}`);
  const data = await res.json();
  return { rows: data.rows || [], total: data.total ?? 0, hasRenewal: data.has_renewal !== false, note: data.note || '' };
}

export function formatRenewalRow(row, index) {
  const bits = [
    row.name || row.pl_name || 'מתחם התחדשות',
    row.locality || null,
    row.units_existing_n ? `${row.units_existing_n} יח״ד קיימות` : null,
    row.units_planned_n ? `→ ${row.units_planned_n} מתוכננות` : null,
    row.multiplier ? `מכפיל ×${row.multiplier}` : null,
    row.status || null,
  ].filter(Boolean);
  const numbering = typeof index === 'number' ? `${index + 1}. ` : '';
  let text = numbering + bits.join(' · ');
  if (row.rank_score != null) text += `\n⭐ ציון הזדמנות: ${Math.round(row.rank_score)}`;
  if (row.map_url) text += `\n🗺 מפה: ${row.map_url}`;
  if (row.pl_url || row.url) text += `\n📄 תוכנית: ${row.pl_url || row.url}`;
  return text;
}

// Formats the parcel dossier into a short chat summary - the full response
// carries dozens of keys (plans, appraisals, tenders, municipal permits...);
// a bot reply surfaces counts and the two links (map, land-registry extract)
// rather than dumping the dossier, same posture as formatRow's "extras".
export function formatParcelSummary(data) {
  const lines = [`📍 גוש ${data.gush} חלקה ${data.helka}${data.locality ? ` · ${data.locality}` : ''}`];
  const counts = [
    data.plans?.length ? `${data.plans.length} תוכניות חלות` : null,
    data.renewal?.length ? `${data.renewal.length} מתחמי התחדשות בסביבה` : null,
    data.permits?.length ? `${data.permits.length} היתרי בנייה בסביבה` : null,
    data.dangerous?.length ? `⚠️ ${data.dangerous.length} מבנים מסוכנים בסביבה` : null,
  ].filter(Boolean);
  if (counts.length) lines.push(counts.join(' · '));
  if (data.plans?.length) {
    lines.push('', 'תוכניות עיקריות:');
    for (const plan of data.plans.slice(0, 3)) {
      lines.push(`• ${plan.pl_number || ''} ${plan.pl_name || ''} — ${plan.short_status || plan.station || 'סטטוס לא ידוע'}`.trim());
    }
  }
  if (data.renewal?.length) {
    lines.push('', 'הזדמנויות התחדשות בסביבה:');
    for (const r of data.renewal.slice(0, 3)) {
      lines.push(`• ${r.name || r.pl_name || 'מתחם'}${r.units_existing ? ` — ${r.units_existing} יח״ד קיימות` : ''}`);
    }
  }
  lines.push('');
  if (data.govmap_link) lines.push(`🗺 מפה: ${data.govmap_link}`);
  if (data.tabu_link) lines.push(`📜 נסח טאבו: ${data.tabu_link}`);
  return lines.join('\n');
}

export async function readSetting(key) {
  try {
    const res = await fetch(`${PYTHON_API_BASE}/api/admin/settings`, { headers: authHeaders() });
    if (!res.ok) return null;
    const data = await res.json();
    return data[key] ?? null;
  } catch (err) {
    console.error(`could not read setting "${key}" from PlanWatch settings:`, err.message);
    return null;
  }
}

export async function writeSetting(key, value) {
  try {
    const res = await fetch(`${PYTHON_API_BASE}/api/admin/settings`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ [key]: value }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return true;
  } catch (err) {
    console.error(`could not publish setting "${key}" to PlanWatch settings:`, err.message);
    return false;
  }
}
