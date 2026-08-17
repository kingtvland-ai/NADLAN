/* PlanWatch consumer webapp - login + search + share.
 *
 * Talks to the existing PlanWatch API (dashboard.py) over its USER_ROUTES
 * allow-list, authenticated with a bearer token from /api/user/login - a
 * separate, narrower auth path from the operator's admin Basic Auth. See
 * dashboard.py's PUBLIC_ROUTES/USER_ROUTES and accounts.py. */

const CFG = window.PLANWATCH_CONFIG;
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const TOKEN_KEY = "pw-user-token";

function esc(s){
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function api(path, opts = {}){
  const token = localStorage.getItem(TOKEN_KEY);
  const headers = Object.assign({}, opts.headers || {},
    token ? {Authorization: `Bearer ${token}`} : {});
  const r = await fetch(CFG.API_BASE + path, Object.assign({}, opts, {headers}));
  const d = await r.json().catch(() => ({error: "bad response from server"}));
  if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

// Absent key = enabled, mirrors accounts.py's _permissions_out default -
// a user predating this feature (or the {} an admin never touched) keeps
// full access rather than losing every tab.
function modeAllowed(permissions, mode){
  return !permissions || permissions[mode] !== false;
}

function applyPermissions(permissions){
  let firstVisible = null;
  $$("#modes button").forEach(b => {
    const allowed = modeAllowed(permissions, b.dataset.mode);
    b.classList.toggle("hidden", !allowed);
    if (allowed && !firstVisible) firstVisible = b;
  });
  const active = $('#modes button[aria-selected="true"]');
  if (active && active.classList.contains("hidden") && firstVisible) firstVisible.click();
}

function showLoggedIn(username, permissions){
  $("#view-login").classList.add("hidden");
  $("#view-search").classList.remove("hidden");
  $("#whoami").textContent = username ? `שלום, ${username}` : "";
  $("#whoami").classList.remove("hidden");
  $("#btn-logout").classList.remove("hidden");
  applyPermissions(permissions);
}

function showLoggedOut(){
  $("#view-login").classList.remove("hidden");
  $("#view-search").classList.add("hidden");
  $("#results").innerHTML = "";
  $("#whoami").classList.add("hidden");
  $("#btn-logout").classList.add("hidden");
}

async function tryResume(){
  if (!localStorage.getItem(TOKEN_KEY)) return showLoggedOut();
  try {
    const me = await api("/api/user/me");
    showLoggedIn(me.username, me.permissions);
  } catch {
    localStorage.removeItem(TOKEN_KEY);
    showLoggedOut();
  }
}

$("#btn-login").onclick = async () => {
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  $("#login-error").classList.add("hidden");
  if (!username || !password) return;
  try {
    const d = await api("/api/user/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username, password}),
    });
    localStorage.setItem(TOKEN_KEY, d.token);
    showLoggedIn(d.username || username, d.permissions);
  } catch (e) {
    $("#login-error").textContent = e.message;
    $("#login-error").classList.remove("hidden");
  }
};

$("#btn-logout").onclick = async () => {
  try { await api("/api/user/logout", {method: "POST"}); } catch { /* token already gone server-side */ }
  localStorage.removeItem(TOKEN_KEY);
  showLoggedOut();
};

function rowImage(row){
  if (row.image) return row.image;
  let extra = row.images;
  if (typeof extra === "string") { try { extra = JSON.parse(extra); } catch { extra = null; } }
  return Array.isArray(extra) && extra.length ? extra[0] : null;
}

// Yad2/ONMAP rows carry city/sqm; the AD/Madlan/Komo (legacy) rows carry
// locality/area_m2 - three sources, two different field names for the same
// two facts. Every place that reads them must fall back, or most of the
// 69,000 listings (the Yad2+ONMAP majority) silently show blank city/size.
const rowCity = (row) => row.locality || row.city || "";
const rowArea = (row) => row.area_m2 ?? row.sqm ?? null;

function shareText(row){
  const bits = [row.address || rowCity(row), row.rooms ? `${row.rooms} חדרים` : null,
    row.price ? `₪${Number(row.price).toLocaleString()}` : null, row.url].filter(Boolean);
  return bits.join(" · ");
}

const CONFIDENCE_LABEL = {high: "בטחון גבוה", partial: "בטחון חלקי", low: "בטחון נמוך"};

// The scored feeds (ad/komo/madlan) already compute price_per_m2 vs a
// locality/neighborhood benchmark, mortgage estimates and a comparable-sale
// count server-side (see market_client.py) - the webapp just never showed
// any of it. This turns that into the hover/tap "reveal" panel per card.
function resultReveal(r){
  const stats = [];
  if (r.price_per_m2 && r.benchmark_m2){
    const pct = Math.max(0, Math.min(100, Math.round((r.price_per_m2 / r.benchmark_m2) * 100)));
    stats.push(`<div class="reveal-stat reveal-bar">
      <div class="l">מחיר למ"ר לעומת ${esc(r.benchmark_scope || "השוק")}
        (₪${Number(r.price_per_m2).toLocaleString()} מתוך ₪${Number(r.benchmark_m2).toLocaleString()})</div>
      <div class="track"><div class="fill" style="width:${pct}%"></div></div>
    </div>`);
  }
  if (r.monthly_payment)
    stats.push(`<div class="reveal-stat"><div class="l">משכנתא חודשית משוערת</div>
      <div class="v">₪${Number(r.monthly_payment).toLocaleString()}</div></div>`);
  if (r.down_payment)
    stats.push(`<div class="reveal-stat"><div class="l">הון עצמי נדרש</div>
      <div class="v">₪${Number(r.down_payment).toLocaleString()}</div></div>`);
  if (r.comparable_count)
    stats.push(`<div class="reveal-stat"><div class="l">מבוסס על</div>
      <div class="v">${r.comparable_count} עסקאות דומות</div></div>`);
  if (r.confidence)
    stats.push(`<div class="reveal-stat"><div class="l">איכות הנתונים</div>
      <div class="v"><span class="confidence-chip ${esc(r.confidence)}">${esc(CONFIDENCE_LABEL[r.confidence] || r.confidence)}</span></div></div>`);
  return stats.length ? `<div class="reveal"><div class="reveal-grid">${stats.join("")}</div></div>` : "";
}

/* ---------------- listing detail modal: full gallery, גוש/חלקה, contact --------------
   ONMAP rows already carry gush/helka/phone in the search response; Yad2 rows carry
   lat/lon + a token instead, resolved here on demand via /api/parcel-at and
   /api/listing-contact - the same "never bulk, never invented" posture those
   two backend routes already document (see dashboard.py and yad2_contact.py).
   Legacy (AD/Madlan/Komo) rows carry neither and fall back to the brokerage
   name only. */
let RENDERED_ROWS = [];
const isYad2Row = (row) => /yad2/i.test(row.source || "");

function galleryHtml(images, mainIdx){
  if (!images.length) return '<p class="count">אין תמונות זמינות למודעה זו.</p>';
  const main = images[mainIdx] || images[0];
  const thumbs = images.map((src, i) =>
    `<img src="${esc(src)}" class="${i === mainIdx ? "active" : ""}" referrerpolicy="no-referrer"
      onclick="setLightboxMain('${esc(src)}', this)">`
  ).join("");
  return `<div class="lightbox-main"><img src="${esc(main)}" id="lightbox-main-img" referrerpolicy="no-referrer"></div>
    <div class="lightbox-thumbs">${thumbs}</div>`;
}

function setLightboxMain(src, thumbEl){
  $("#lightbox-main-img").src = src;
  $$(".lightbox-thumbs img").forEach(t => t.classList.toggle("active", t === thumbEl));
}

async function openListingDetail(row){
  const modal = $("#listing-modal"), body = $("#listing-modal-body");
  modal.classList.remove("hidden");
  $("#listing-modal-title").textContent = row.address || row.neighborhood || rowCity(row) || "פרטי נכס";
  body.innerHTML = '<div class="spinner"></div>';

  const area = rowArea(row);
  const images = (() => {
    const set = new Set();
    const first = rowImage(row);
    if (first) set.add(first);
    let extra = row.images;
    if (typeof extra === "string") { try { extra = JSON.parse(extra); } catch { extra = null; } }
    if (Array.isArray(extra)) extra.forEach(u => u && set.add(u));
    return [...set];
  })();

  // Fire all three lookups in parallel - each degrades independently rather
  // than blocking the others on failure.
  const galleryP = row.url
    ? api(`/api/listing-gallery?url=${encodeURIComponent(row.url)}&source=${encodeURIComponent(row.source || "")}`).catch(() => null)
    : Promise.resolve(null);
  const parcelP = (row.gush && row.helka)
    ? Promise.resolve({gush: row.gush, helka: row.helka})
    : (row.lat && row.lon
        ? api(`/api/parcel-at?lat=${row.lat}&lon=${row.lon}`).catch(() => null)
        : Promise.resolve(null));
  const contactP = row.phone
    ? Promise.resolve({available: true, phone: row.phone})
    : (isYad2Row(row) && row.token
        ? api(`/api/listing-contact?token=${encodeURIComponent(row.token)}`).catch(() => null)
        : Promise.resolve(null));

  const [galleryRes, parcelRes, contactRes] = await Promise.all([galleryP, parcelP, contactP]);
  const allImages = [...images, ...((galleryRes && galleryRes.images) || [])].filter((v, i, a) => a.indexOf(v) === i);

  const infoStats = [
    ["מחיר", row.price ? "₪" + Number(row.price).toLocaleString() : null],
    ["חדרים", row.rooms],
    ['שטח (מ"ר)', area],
    ["קומה", row.floor],
    ["סוג נכס", row.property_type],
    ['מחיר למ"ר', row.price_per_m2 ? "₪" + Number(row.price_per_m2).toLocaleString() : null],
    ["ימים בשוק", row.dom_days],
    ["משכנתא חודשית משוערת", row.monthly_payment ? "₪" + Number(row.monthly_payment).toLocaleString() : null],
    ["הון עצמי נדרש", row.down_payment ? "₪" + Number(row.down_payment).toLocaleString() : null],
    ["תחנת רכבת קרובה", row.rail_station ? `${row.rail_station} (${Math.round(row.rail_distance_m || 0)} מ')` : null],
    ["בתי ספר ברדיוס ק\"מ", row.schools_1km],
    ["תחנות אוטובוס ב-500 מ'", row.bus_stops_500m],
    ["מקור", row.source],
    ["נראתה לראשונה", (row.first_seen_at || row.first_seen || "").slice(0, 10) || null],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");

  // Second section: the size-adjusted valuation model's own reasoning - see
  // market_client.py/listing_analytics.py, which score every Yad2/ONMAP row
  // against comparable peers. Shown only when the row actually carries them,
  // since legacy (AD/Madlan/Komo) rows are scored by an older, narrower path.
  const insightStats = [
    ["ציון קנייה", row.buy_score != null ? Math.round(row.buy_score) : null],
    ["ציון מכירה", row.sell_score != null ? Math.round(row.sell_score) : null],
    ["פער מול נכסים דומים", row.peer_gap_pct != null ? `${row.peer_gap_pct > 0 ? "+" : ""}${Math.round(row.peer_gap_pct)}%` : null],
    ["פער מול נכסים דומים (₪)", row.peer_gap_ils ? "₪" + Number(row.peer_gap_ils).toLocaleString() : null],
    ["נכסים דומים שנבדקו", row.peer_count],
    ["תוכניות בסביבה", row.plans_count],
    ["מתחמי התחדשות עירונית בסביבה", row.renewal_complexes],
    ["נכסים נוספים בבעלות המוכר", row.owner_property_count],
    ["מודעות נוספות באותה כתובת", row.address_listings],
    ["סימני מוטיבציה למכירה", Array.isArray(row.motivation_flags) ? row.motivation_flags.join(", ")
      : (row.motivation_flags || null)],
  ].filter(([, v]) => v !== null && v !== undefined && v !== "");

  const gush = parcelRes && parcelRes.gush;
  const helka = parcelRes && parcelRes.helka;
  const phone = contactRes && contactRes.available && contactRes.phone;
  const contactMsg = (contactRes && contactRes.reason)
    || (row.brokerage ? `מתווך: ${row.brokerage}` : "אין פרטי יצירת קשר זמינים למקור זה");

  body.innerHTML = `
    <div class="detail-section">${galleryHtml(allImages, 0)}</div>
    ${gush && helka ? `<div class="parcel-ref" style="margin-bottom:16px">גוש ${esc(gush)} חלקה ${esc(helka)}</div>` : ""}
    <div class="detail-section">
      <h4>פרטי הנכס</h4>
      ${infoStats.map(([k, v]) => planDetailRow(k, v)).join("")}
    </div>
    ${insightStats.length ? `<div class="detail-section">
      <h4>ניתוח וניקוד</h4>
      ${insightStats.map(([k, v]) => planDetailRow(k, v)).join("")}
    </div>` : ""}
    <div class="detail-section">
      <h4>יצירת קשר</h4>
      ${phone
        ? `<div class="contact-box"><span class="phone">${esc(phone)}</span>
             <a class="btn primary" href="tel:${esc(phone)}">התקשר</a></div>`
        : `<div class="contact-box"><span class="count">${esc(contactMsg)}</span>
          ${row.url ? `<a class="btn" href="${esc(row.url)}" target="_blank" rel="noopener">למודעה המקורית ↗</a>` : ""}</div>`}
    </div>`;
}
function closeListingModal(){ $("#listing-modal").classList.add("hidden"); }
$("#listing-modal-close").onclick = closeListingModal;
$("#listing-modal-close-2").onclick = closeListingModal;
$("#listing-modal").onclick = (ev) => { if (ev.target.id === "listing-modal") closeListingModal(); };

document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  closeListingModal();
  closePlanModal();
});

function renderResults(rows){
  RENDERED_ROWS = rows;
  if (!rows.length){
    $("#results").innerHTML = '<p class="count">אין תוצאות. נסה לשנות את הסינון.</p>';
    return;
  }
  $("#results").innerHTML = rows.map((r, i) => {
    const img = rowImage(r);
    const text = encodeURIComponent(shareText(r));
    const area = rowArea(r);
    const discount = r.discount_pct && r.discount_pct > 0
      ? `<span class="badge-pop badge-discount" title="${Math.round(r.discount_pct)}% מתחת למחיר השוק">🔥 ${Math.round(r.discount_pct)}%</span>` : "";
    const score = r.rank_score != null
      ? `<span class="badge-pop badge-score" title="ציון התאמה">⭐ ${Math.round(r.rank_score)}</span>` : "";
    return `<div class="result" style="animation-delay:${Math.min(i, 12) * 35}ms" onclick="this.classList.toggle('expanded')">
      <div class="result-media">
        ${discount}${score}
        ${img ? `<img src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
      </div>
      <div class="body">
        <h4>${esc(r.address || r.neighborhood || rowCity(r) || "נכס")}</h4>
        <div class="price">${r.price ? "₪" + Number(r.price).toLocaleString() : "מחיר לא צוין"}</div>
        <div class="meta">${[rowCity(r), r.rooms ? r.rooms + " חדרים" : null,
          area ? area + ' מ"ר' : null].filter(Boolean).map(esc).join(" · ")}</div>
        ${resultReveal(r)}
        <div class="actions" onclick="event.stopPropagation()">
          <button class="btn primary" onclick="openListingDetail(RENDERED_ROWS[${i}])">📷 תמונות, גוש/חלקה וטלפון</button>
          ${r.url ? `<a class="btn" href="${esc(r.url)}" target="_blank" rel="noopener">למודעה ↗</a>` : ""}
          ${CFG.WHATSAPP_BOT_NUMBER
            ? `<a class="btn wa" href="https://wa.me/${CFG.WHATSAPP_BOT_NUMBER}?text=${text}" target="_blank" rel="noopener">שתף ב-WhatsApp</a>`
            : ""}
          ${CFG.TELEGRAM_BOT_USERNAME
            ? `<a class="btn tg" href="https://t.me/${CFG.TELEGRAM_BOT_USERNAME}?text=${text}" target="_blank" rel="noopener">שתף ב-Telegram</a>`
            : ""}
        </div>
      </div>
    </div>`;
  }).join("");
}

/* ---------------- listings: quick filters, sort, grid/list ----------------
   Rows are fetched once per search and re-sorted/re-rendered client-side on
   sort/view changes - no refetch, since the sort key needs to agree across
   three sources that don't share one server-side ranking. */
let LAST_ROWS = [];
let SELECTED_MIN_ROOMS = "";
let SELECTED_MAX_ROOMS = "";
let CURRENT_VIEW = "list";
let CURRENT_SORT = "score";

const SORTERS = {
  score: (r) => -(r.rank_score ?? -1),
  price_asc: (r) => r.price ?? Infinity,
  price_desc: (r) => -(r.price ?? -1),
  rooms_desc: (r) => -(r.rooms ?? -1),
  area_desc: (r) => -(rowArea(r) ?? -1),
};

/* -------- map view (Leaflet, OpenStreetMap tiles) --------
   Only Yad2/ONMAP rows carry lat/lon (see rowCity/rowArea's comment on the
   three-source field split) - the AD/Madlan/Komo feed has none, so the map
   silently shows a subset of what the list/grid views show. MAP is created
   once and reused; Leaflet needs invalidateSize() the first time its
   container becomes visible, or it renders at 0 height. */
let MAP = null;
let MAP_LAYER = null;

function ensureMap(){
  if (MAP || typeof L === "undefined") return MAP;
  MAP = L.map("results-map", {scrollWheelZoom: true}).setView([31.5, 34.9], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap", maxZoom: 19,
  }).addTo(MAP);
  MAP_LAYER = L.layerGroup().addTo(MAP);
  return MAP;
}

function pinIcon(){
  return L.divIcon({className: "", html: '<div class="map-pin"></div>',
    iconSize: [14, 14], iconAnchor: [7, 12], popupAnchor: [0, -12]});
}

function renderMap(rows){
  const map = ensureMap();
  if (!map){
    $("#results-map").innerHTML = '<p class="count" style="padding:20px">המפה לא זמינה כרגע (לא ניתן היה לטעון את ספריית המפות).</p>';
    return;
  }
  MAP_LAYER.clearLayers();
  const pts = rows.filter(r => r.lat && r.lon);
  const bounds = [];
  for (const r of pts){
    const area = rowArea(r);
    // A DOM node (not an HTML string) so the details button can bind a real
    // closure over `r` - Leaflet popups accept either.
    const el = document.createElement("div");
    el.className = "map-popup";
    el.innerHTML = `<div class="p">${r.price ? "₪" + Number(r.price).toLocaleString() : "מחיר לא צוין"}</div>
      <div class="a">${esc(r.address || r.neighborhood || rowCity(r) || "נכס")}</div>
      <div class="m">${[rowCity(r), r.rooms ? r.rooms + " חדרים" : null,
        area ? area + ' מ"ר' : null].filter(Boolean).map(esc).join(" · ")}</div>
      <div class="row" style="margin-top:6px;gap:6px">
        <button class="btn primary" style="font-size:12px;padding:5px 9px" type="button">📷 פרטים</button>
        ${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">למודעה ↗</a>` : ""}
      </div>`;
    el.querySelector("button").addEventListener("click", () => openListingDetail(r));
    L.marker([r.lat, r.lon], {icon: pinIcon()}).bindPopup(el).addTo(MAP_LAYER);
    bounds.push([r.lat, r.lon]);
  }
  setTimeout(() => {
    map.invalidateSize();
    if (bounds.length) map.fitBounds(bounds, {padding: [30, 30], maxZoom: 15});
  }, 50);
  $("#map-count-note").textContent = pts.length
    ? `${pts.length} מתוך ${rows.length} תוצאות מוצגות על המפה (יש להן מיקום ידוע).`
    : "אף אחת מהתוצאות הנוכחיות לא כוללת מיקום ידוע להצגה על המפה.";
}

function applySortAndRender(){
  const sorted = [...LAST_ROWS].sort((a, b) => SORTERS[CURRENT_SORT](a) - SORTERS[CURRENT_SORT](b));
  if (CURRENT_VIEW === "map"){
    $("#results").classList.add("hidden");
    $("#results-map").classList.remove("hidden");
    $("#map-note-wrap").classList.remove("hidden");
    renderMap(sorted);
  } else {
    $("#results-map").classList.add("hidden");
    $("#map-note-wrap").classList.add("hidden");
    $("#results").classList.remove("hidden");
    $("#results").classList.toggle("grid", CURRENT_VIEW === "grid");
    renderResults(sorted);
  }
}

$$("#city-chips .chip").forEach(b => b.onclick = () => {
  $$("#city-chips .chip").forEach(x => x.setAttribute("aria-selected", String(x === b)));
  $("#q-locality").value = b.dataset.city;
  runSearch();
});

$$("#room-chips .chip").forEach(b => b.onclick = () => {
  $$("#room-chips .chip").forEach(x => x.setAttribute("aria-selected", String(x === b)));
  SELECTED_MIN_ROOMS = b.dataset.minRooms;
  SELECTED_MAX_ROOMS = b.dataset.maxRooms;
  runSearch();
});

$("#sort-select").onchange = () => { CURRENT_SORT = $("#sort-select").value; applySortAndRender(); };
$$("#view-toggle button").forEach(b => b.onclick = () => {
  $$("#view-toggle button").forEach(x => x.setAttribute("aria-selected", String(x === b)));
  CURRENT_VIEW = b.dataset.view;
  applySortAndRender();
});

$("#adv-toggle").onclick = () => {
  const open = $("#adv-panel").classList.toggle("open");
  $("#adv-toggle").setAttribute("aria-expanded", String(open));
};

function animateCount(el, to, suffix){
  const from = 0;
  const duration = 700;
  const start = performance.now();
  function step(now){
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = `${Math.round(from + (to - from) * eased).toLocaleString()} ${suffix}`;
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

/* -------- simulated progress bar --------
   The server doesn't stream real progress, but the two cases are known and
   very different (dashboard.py's _annotated_cached comment: first search
   after a restart scores the whole ~69k-row stock, further searches are
   cached) - so the animation targets a slow ~90s creep the first time and a
   quick ~1.5s creep after that, always finishing with a snap to 100% only
   once the response actually arrives. Never claims a percentage it can't
   back up as "done". */
let SEARCHED_ONCE = false;
let PROGRESS_TIMER = null;

function startProgress(){
  $("#search-count").classList.add("hidden");
  $("#search-progress").classList.remove("hidden");
  const tau = SEARCHED_ONCE ? 900 : 26000;
  const start = performance.now();
  clearInterval(PROGRESS_TIMER);
  PROGRESS_TIMER = setInterval(() => {
    const elapsed = performance.now() - start;
    const pct = Math.round(90 * (1 - Math.exp(-elapsed / tau)));
    $("#progress-fill").style.width = pct + "%";
    $("#progress-label").textContent = pct + "%";
  }, 120);
}

function finishProgress(){
  clearInterval(PROGRESS_TIMER);
  $("#progress-fill").style.width = "100%";
  $("#progress-label").textContent = "100%";
  SEARCHED_ONCE = true;
  setTimeout(() => {
    $("#search-progress").classList.add("hidden");
    $("#search-count").classList.remove("hidden");
    $("#progress-fill").style.width = "0%";
  }, 350);
}

async function runSearch(){
  $("#search-error").classList.add("hidden");
  const params = new URLSearchParams();
  const locality = $("#q-locality").value.trim();
  if (locality) params.set("locality", locality);
  if ($("#q-min-price").value) params.set("min_price", $("#q-min-price").value);
  if ($("#q-max-price").value) params.set("max_price", $("#q-max-price").value);
  if (SELECTED_MIN_ROOMS) params.set("min_rooms", SELECTED_MIN_ROOMS);
  if (SELECTED_MAX_ROOMS) params.set("max_rooms", SELECTED_MAX_ROOMS);
  if ($("#q-min-area").value) params.set("min_area", $("#q-min-area").value);
  if ($("#q-max-area").value) params.set("max_area", $("#q-max-area").value);
  if ($("#q-min-ppm").value) params.set("min_ppm", $("#q-min-ppm").value);
  if ($("#q-max-ppm").value) params.set("max_ppm", $("#q-max-ppm").value);
  if ($("#q-min-discount").value) params.set("min_discount", $("#q-min-discount").value);
  if ($("#q-max-dom").value) params.set("max_dom", $("#q-max-dom").value);
  if ($("#q-has-url").checked) params.set("has_url", "1");
  if ($("#q-source").value) params.set("source", $("#q-source").value);
  params.set("limit", "60");
  // The first search after the server (re)starts scores the full listing
  // stock before filtering/paging and can take a minute or two; every
  // search after that is served from cache and comes back in seconds - see
  // dashboard.py's _annotated_cached. The progress bar's pace (not just this
  // label) is honest about that difference too - see startProgress.
  $("#search-count").textContent = SEARCHED_ONCE ? "מחפש…" : "מחפש… החיפוש הראשון עשוי לקחת עד כמה דקות";
  $("#results").innerHTML = "";
  startProgress();
  try {
    const d = await api(`/api/combined-sale-listings?${params}`);
    LAST_ROWS = [...(d.legacy_rows || []), ...(d.live_rows || []), ...(d.onmap_rows || []), ...(d.facebook_rows || [])];
    finishProgress();
    animateCount($("#search-count"), d.total ?? LAST_ROWS.length, "תוצאות");
    $("#listings-toolbar").style.display = LAST_ROWS.length ? "flex" : "none";
    applySortAndRender();
  } catch (e) {
    finishProgress();
    $("#search-count").textContent = "";
    $("#search-error").textContent = e.message;
    $("#search-error").classList.remove("hidden");
  }
}
$("#btn-search").onclick = runSearch;

/* ---------------- mode switching (listings / smart / renewal / market) ---------------- */
const MODES = ["listings", "smart", "renewal", "market"];
$$("#modes button").forEach(b => b.onclick = () => {
  $$("#modes button").forEach(x => x.setAttribute("aria-selected", String(x === b)));
  MODES.forEach(m => $(`#mode-${m}`).classList.toggle("hidden", m !== b.dataset.mode));
  $("#results").innerHTML = "";
  $("#results").classList.remove("grid", "hidden");
  $("#results-map").classList.add("hidden");
  $("#map-note-wrap").classList.add("hidden");
  $("#search-error").classList.add("hidden");
});

/* ---------------- plan detail (in-app, replaces navigating to the gov site) ---------------- */
function parcelRef(row){
  return row.gush && row.helka ? `גוש ${esc(row.gush)} חלקה ${esc(row.helka)}` : null;
}

function planDetailRow(k, v){
  if (v === null || v === undefined || v === "") return "";
  return `<div class="detail-row"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;
}

async function openPlanDetail(objectId){
  const modal = $("#plan-modal"), body = $("#plan-modal-body");
  modal.classList.remove("hidden");
  body.innerHTML = '<p class="count">טוען…</p>';
  try {
    const d = await api(`/api/plan-summary/${objectId}`);
    const p = d.plan || {};
    $("#plan-modal-title").textContent = p.pl_name ? `${p.pl_number} · ${p.pl_name}` : (p.pl_number || "תוכנית");
    const ref = parcelRef(p);
    const renewalUnits = (d.renewal || []).reduce((s, r) => s + (Number(r.units_planned) || 0), 0);
    body.innerHTML = [
      ref ? `<div class="parcel-ref">${ref}</div>` : "",
      planDetailRow("רשות מקומית", p.jurisdiction_name),
      planDetailRow("אזור תכנון", p.plan_area_name),
      planDetailRow("שלב", p.short_status || p.station),
      planDetailRow("ייעוד עיקרי", p.landuse),
      planDetailRow('שטח (דונם)', p.area_dunam),
      planDetailRow('יח"ד מאושרות', p.housing_units),
      planDetailRow("מטרות התוכנית", p.objectives),
      planDetailRow("עדכון אחרון", (p.last_update || "").slice(0, 10)),
      planDetailRow("הופקדה", (p.depositing_date || "").slice(0, 10)),
      planDetailRow("נפתחה", (p.open_date || "").slice(0, 10)),
      d.renewal && d.renewal.length
        ? planDetailRow("מתחמי התחדשות עירונית קשורים",
            `${d.renewal.length}${renewalUnits ? ` · ${renewalUnits} יח"ד מתוכננות` : ""}`)
        : "",
      p.pl_url ? `<div class="row" style="margin-top:12px">
        <a class="btn" href="${esc(p.pl_url)}" target="_blank" rel="noopener">מסמכי התוכנית באתר הממשלתי ↗</a>
      </div>` : "",
    ].filter(Boolean).join("") || '<p class="count">אין פרטים זמינים לתוכנית זו.</p>';
  } catch (e) {
    body.innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
}
function closePlanModal(){ $("#plan-modal").classList.add("hidden"); }
$("#plan-modal-close").onclick = closePlanModal;
$("#plan-modal-close-2").onclick = closePlanModal;
$("#plan-modal").onclick = (ev) => { if (ev.target.id === "plan-modal") closePlanModal(); };

/* ---------------- smart search (parcels / plans / localities / places) ---------------- */
function smartRowTitle(kind, row){
  if (kind === "plans") return row.pl_number + (row.pl_name ? ` · ${row.pl_name}` : "");
  return row.name || row.pl_name || row.formatted_address || row.address
    || Object.values(row).find(v => typeof v === "string") || "תוצאה";
}

function smartRowSubtitle(kind, row){
  if (kind === "plans")
    return [row.jurisdiction_name, row.short_status || row.station,
      row.housing_units ? `${row.housing_units} יח"ד` : null].filter(Boolean).join(" · ");
  return [row.locality || row.city, row.type, row.phone].filter(Boolean).join(" · ");
}

function renderSmartResults(groups){
  if (!groups.length){
    $("#results").innerHTML = '<p class="count">אין תוצאות. נסה חיפוש אחר.</p>';
    return;
  }
  $("#results").innerHTML = groups.map(g => {
    if (g.kind === "parcel")
      return `<div class="group"><h4>${esc(g.title)}</h4>
        <div class="simple-row"><div class="t">גוש ${esc(g.gush)} חלקה ${esc(g.helka)}</div></div></div>`;
    if (g.kind === "locality")
      return `<div class="group"><h4>${esc(g.title)}</h4>
        <div class="simple-row"><div class="t">${esc(g.locality?.name || "")}</div>
        <div class="s">${g.plans ?? 0} תוכניות · ${(g.units ?? 0).toLocaleString()} יח"ד מתוכננות</div></div></div>`;
    const rows = g.rows || [];
    return `<div class="group"><h4>${esc(g.title)}</h4>` +
      rows.map(r => {
        if (g.kind !== "plans")
          return `<div class="simple-row">
            <div class="t">${esc(smartRowTitle(g.kind, r))}</div>
            <div class="s">${esc(smartRowSubtitle(g.kind, r))}</div>
          </div>`;
        const ref = parcelRef(r);
        return `<div class="simple-row">
          <div class="t">${esc(smartRowTitle(g.kind, r))}</div>
          <div class="s">${esc(smartRowSubtitle(g.kind, r))}</div>
          ${ref ? `<div class="parcel-ref">${ref}</div>` : ""}
          <div class="row" style="margin-top:6px">
            <button class="btn" onclick="openPlanDetail(${r.object_id})">פרטים מלאים באתר</button>
          </div>
        </div>`;
      }).join("") + `</div>`;
  }).join("");
}

async function runSmartSearch(){
  $("#search-error").classList.add("hidden");
  const q = $("#q-smart").value.trim();
  if (!q) return;
  $("#results").innerHTML = '<p class="count">מחפש…</p>';
  try {
    const d = await api(`/api/search?q=${encodeURIComponent(q)}`);
    renderSmartResults(d.groups || []);
  } catch (e) {
    $("#results").innerHTML = "";
    $("#search-error").textContent = e.message;
    $("#search-error").classList.remove("hidden");
  }
}
$("#btn-smart-search").onclick = runSmartSearch;

/* ---------------- urban-renewal opportunities ---------------- */
function renderRenewalResults(rows){
  if (!rows.length){
    $("#results").innerHTML = '<p class="count">אין תוצאות. נסה לשנות את הסינון.</p>';
    return;
  }
  $("#results").innerHTML = rows.map(r => {
    const text = encodeURIComponent([r.locality, r.plan_number, r.pl_url].filter(Boolean).join(" · "));
    return `<div class="result">
      <div class="body">
        <h4>${esc(r.locality || "מתחם התחדשות")}
          ${r.rank_score != null ? `<span class="score">${r.rank_score}</span>` : ""}</h4>
        <div class="meta">${[r.track, r.status || r.short_status,
          r.units_existing_n ? `${r.units_existing_n} יח"ד קיימות` : null,
          r.units_planned_n ? `→ ${r.units_planned_n} מתוכננות` : null,
          r.permits_n ? `${r.permits_n} היתרים` : "אין היתרים עדיין"]
          .filter(Boolean).map(esc).join(" · ")}</div>
        <div class="actions">
          ${r.object_id ? `<button class="btn" onclick="openPlanDetail(${r.object_id})">פרטים מלאים באתר</button>`
            : (r.pl_url ? `<a class="btn" href="${esc(r.pl_url)}" target="_blank" rel="noopener">לתוכנית ↗</a>` : "")}
          ${CFG.WHATSAPP_BOT_NUMBER
            ? `<a class="btn wa" href="https://wa.me/${CFG.WHATSAPP_BOT_NUMBER}?text=${text}" target="_blank" rel="noopener">שתף ב-WhatsApp</a>`
            : ""}
          ${CFG.TELEGRAM_BOT_USERNAME
            ? `<a class="btn tg" href="https://t.me/${CFG.TELEGRAM_BOT_USERNAME}?text=${text}" target="_blank" rel="noopener">שתף ב-Telegram</a>`
            : ""}
        </div>
      </div>
    </div>`;
  }).join("");
}

async function runRenewalSearch(){
  $("#search-error").classList.add("hidden");
  const params = new URLSearchParams();
  const locality = $("#q-ren-locality").value.trim();
  if (locality) params.set("locality", locality);
  if ($("#q-ren-open").checked) params.set("max_permits", "0");
  params.set("limit", "40");
  $("#renewal-count").textContent = "מחפש…";
  $("#results").innerHTML = "";
  try {
    const d = await api(`/api/opportunities?${params}`);
    $("#renewal-count").textContent = d.note ? d.note : `${d.total ?? (d.rows || []).length} תוצאות`;
    renderRenewalResults(d.rows || []);
  } catch (e) {
    $("#renewal-count").textContent = "";
    $("#search-error").textContent = e.message;
    $("#search-error").classList.remove("hidden");
  }
}
$("#btn-renewal-search").onclick = runRenewalSearch;

/* ---------------- market & indices ---------------- */
async function loadMarket(){
  $("#market-content").innerHTML = '<p class="count">טוען…</p>';
  try {
    const d = await api("/api/market");
    const n = d.national;
    const renewalTotal = (d.renewal_stages || []).reduce((sum, s) => sum + (s.complexes || 0), 0);
    const localityRow = (r) => `<div class="simple-row">
        <div class="t">${esc(r.locality)}</div>
        <div class="s">₪${Number(r.avg_m2 || 0).toLocaleString()} למ"ר · ${r.projects} פרויקטים</div>
      </div>`;
    $("#market-content").innerHTML = `
      <div class="tiles">
        ${n ? `<div class="tile"><div class="n">${n.pct_year > 0 ? "+" : ""}${n.pct_year ?? "—"}%</div>
          <div class="l">מדד מחירי דירות, שינוי שנתי (למ"ס)</div></div>` : ""}
        <div class="tile"><div class="n">${(d.counts?.rami_inventory ?? 0).toLocaleString()}</div>
          <div class="l">מכרזי רמ"י במאגר</div></div>
        <div class="tile"><div class="n">${renewalTotal.toLocaleString()}</div>
          <div class="l">מתחמי התחדשות עירונית</div></div>
      </div>
      <div class="row" style="align-items:flex-start;gap:24px">
        <div class="group" style="flex:1;min-width:240px">
          <h4>הישובים היקרים ביותר (מחיר למשתכן, ₪/מ"ר)</h4>
          ${(d.most_expensive || []).slice(0, 8).map(localityRow).join("") || '<p class="count">אין נתונים</p>'}
        </div>
        <div class="group" style="flex:1;min-width:240px">
          <h4>הישובים הזולים ביותר (מחיר למשתכן, ₪/מ"ר)</h4>
          ${(d.cheapest || []).slice(0, 8).map(localityRow).join("") || '<p class="count">אין נתונים</p>'}
        </div>
      </div>
      ${(d.caveats || []).length
        ? `<p class="count" style="margin-top:10px">${esc(d.caveats[0])}</p>` : ""}
    `;
  } catch (e) {
    $("#market-content").innerHTML = `<p class="error">${esc(e.message)}</p>`;
  }
}
$("#btn-market-load").onclick = loadMarket;

function setupBotLinks(){
  if (CFG.WHATSAPP_BOT_NUMBER)
    $("#link-whatsapp").href = `https://wa.me/${CFG.WHATSAPP_BOT_NUMBER}`;
  else
    $("#link-whatsapp").classList.add("hidden");
  if (CFG.TELEGRAM_BOT_USERNAME)
    $("#link-telegram").href = `https://t.me/${CFG.TELEGRAM_BOT_USERNAME}`;
  else
    $("#link-telegram").classList.add("hidden");
}

// Values entered in the dashboard's "הגדרות מנהל" tab (stored in the
// `settings` table, GET /api/public/bot-links - no login needed) override
// the static config.js placeholders, so an operator who sets them there
// never has to hand-edit and redeploy this file for just those two fields.
async function loadBotLinksFromServer(){
  try {
    const d = await api("/api/public/bot-links");
    if (d.whatsapp_bot_number) CFG.WHATSAPP_BOT_NUMBER = d.whatsapp_bot_number;
    if (d.telegram_bot_username) CFG.TELEGRAM_BOT_USERNAME = d.telegram_bot_username;
  } catch { /* fall back to config.js's values */ }
  setupBotLinks();
}

loadBotLinksFromServer();
tryResume();
