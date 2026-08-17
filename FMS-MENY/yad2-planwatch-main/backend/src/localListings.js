const DEFAULT_LOCAL_URL = 'http://127.0.0.1:8099/api/yad2';

function asNumber(value) {
  if (value == null || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

/**
 * נרמול רשומת נכס ממקור מקומי (8099) לצורה אחידה.
 *
 * שני המקורות השונים (yad2, onmap) עשויים לחזור עם שמות שדות שונים,
 * לכן המיפוי כאן גמיש ומכסה את הווריאציות הנפוצות. המקור עצמו נשמר על כל
 * רשומה כדי שה-UI יוכל להציג מהיכן הגיע הנכס.
 */
// ONMAP (על המפה) מחזיר מבנה מקונן שונה מ-Yad2:
//   address.he.{city_name,neighborhood,street_name}
//   additional_info.{rooms,area.base,floor.on_the}
//   address.location.{lat,lon}
//   images: [{ thumbnail, gallery, full }]  (או מערך URLים לפרויקטים)
const PROPERTY_TYPE_HE = {
  apartment: 'דירה',
  penthouse: 'פנטהאוז',
  mini_penthouse: 'מיני פנטהאוז',
  cottage: "קוטג'",
  garden_apartment: 'דירת גן',
  duplex: 'דופלקס',
  studio: 'סטודיו',
  room: 'חדר',
  project: 'פרויקט',
};

// ONMAP has no per-listing page: the ad is a panel over the map search, and it
// only opens when the map is already looking at the listing. So the viewport
// goes in the path and the slug selects the ad within it. The older form used
// here, `onmap.co.il/property/<slug>`, is not a route the site serves - it
// answered ONMAP's own "page not found" screen. Mirrors onmap_feed.listing_url.
function onmapListingUrl(slug, lat, lon) {
  if (!slug) return null;
  const base = 'https://www.onmap.co.il/search/homes/buy';
  const y = Number(lat);
  const x = Number(lon);
  if (!Number.isFinite(y) || !Number.isFinite(x)) return `${base}?property=${slug}`;
  return `${base}/c_${y.toFixed(6)},${x.toFixed(6)}`
    + `/t_${(y + 0.02).toFixed(6)},${(x + 0.02).toFixed(6)}/z_16?property=${slug}`;
}

function toImageUrl(img) {
  if (typeof img === 'string') return img;
  return img?.full || img?.gallery || img?.thumbnail || img?.url || null;
}

export function normalizeLocalListing(item, source = 'yad2-local-api') {
  const id = item?.token || item?.id || item?.adId || item?.listingId || item?.propertyId;
  if (!id) return null;

  const isOnmap = source === 'onmap' || item?.entityType === 'property' || item?.entityType === 'project'
    || Boolean(item?.address?.he) || Boolean(item?.additional_info);
  const address = item.address?.he || item.address || {};
  const additional = item.additional_info || {};
  const location = item.address?.location || {};

  const city = isOnmap ? address.city_name || '' : item.city || '';
  const typeHe = isOnmap ? PROPERTY_TYPE_HE[item.property_type] || item.property_type || '' : '';
  const title = isOnmap
    ? `${typeHe ? `${typeHe} ` : ''}${city}`.trim() || item.name?.he || 'נכס'
    : item.title || item.property_type || item.propertyType || item.city || 'נכס מיד2';

  const agency = item.agency || item.customer?.agencyName || item.customer?.name
    || item.agencyName || item.brokerage || item.officeName
    || (isOnmap ? item.team?.owner?.title || null : null);
  const adType = isOnmap
    ? (item.entityType === 'project' ? 'project' : item.search_option === 'rent' ? 'rent' : 'sale')
    : item.ad_type || item.adType || item.sellerType || null;
  // Private = owner selling directly; agency = brokered. Uses the source's own
  // published classification where present, else an absent agency name is a
  // private-seller hint but never treated as certain.
  const isPrivate = adType === 'private' || (!agency && adType !== 'agency' && adType != null);

  const rooms = isOnmap ? asNumber(additional.rooms) : asNumber(item.rooms);
  const squareMeters = isOnmap
    ? asNumber(additional.area?.base ?? additional.area?.total)
    : asNumber(item.sqm ?? item.squareMeters ?? item.area_sqm);
  const floor = isOnmap ? asNumber(additional.floor?.on_the) : asNumber(item.floor);

  // בניית גלריית תמונות: ONMAP מחזיר אובייקטים {thumbnail,gallery,full},
  // מקורות אחרים מחזירים URLים ישירים — שתי הצורות נתמכות.
  const gallery = [
    ...(Array.isArray(item.images) ? item.images.map(toImageUrl) : []),
    ...(Array.isArray(item.photos) ? item.photos.map(toImageUrl) : []),
  ].filter(Boolean);
  const coverImage = item.image
    ? (typeof item.image === 'string' ? item.image : item.image?.full || item.image?.url)
    : gallery[0] || item.logo
      || `https://placehold.co/600x400?text=${encodeURIComponent((city || 'נכס').slice(0, 12))}`;
  if (!gallery.length) gallery.push(coverImage);

  return {
    id,
    title,
    subtitle: agency || '',
    price: asNumber(item.price ?? item.exact_property_price ?? item.min_property_price),
    city,
    area: item.area || '',
    neighborhood: isOnmap ? address.neighborhood || '' : item.neighborhood || '',
    street: isOnmap ? address.street_name || '' : item.street || '',
    floor,
    rooms,
    squareMeters,
    // Every listing gets a cover image: the real image when the source
    // provides one, otherwise a generated placeholder so no card renders
    // with an empty picture slot.
    image: coverImage,
    // Full image gallery for the lightbox: the feed's image array when
    // present, otherwise just the cover so the popup still has something.
    images: gallery,
    latitude: asNumber(location.lat ?? item.lat ?? item.latitude),
    longitude: asNumber(location.lon ?? item.lon ?? item.longitude),
    sourceType: adType,
    agency,
    isPrivate,
    source,
    url: item.url || item.listingUrl || item.link
      || (isOnmap && item.slug ? onmapListingUrl(item.slug, location.lat, location.lon) : null)
      || `https://www.yad2.co.il/realestate/item/${encodeURIComponent(id)}`,
  };
}

async function fetchLocalBatch({ url, timeoutMs = 30000, page, source = 'yad2-local-api' } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const requestUrl = new URL(url);
    if (page != null) requestUrl.searchParams.set('page', String(page));
    const response = await fetch(requestUrl, { headers: { accept: 'application/json' }, signal: controller.signal });
    if (!response.ok) throw new Error(`Local listings API returned HTTP ${response.status}`);
    const payload = await response.json();
    const rows = Array.isArray(payload) ? payload : payload?.data;
    if (!Array.isArray(rows)) throw new Error('Local listings API returned no data array');
    return {
      listings: rows.map((row) => normalizeLocalListing(row, source)).filter(Boolean),
      totalCount: Number(payload?.record_count) || rows.length,
      totalPages: 1,
      page: 1,
      source,
      sourceUrl: payload?.url || requestUrl.toString(),
      sourceName: payload?.source_name || payload?.source || source,
      //: דגל "drift" מועבר ל-UI כך שגם כשהמקור מחזיר תשובה ריקה אפשר
      //: להציג אזהרה ברורה ("המקור השתנה") במקום מסך ריק.
      driftStatus: payload?.drift_status || null,
      driftNote: payload?.drift_note || null,
    };
  } finally {
    clearTimeout(timeout);
  }
}

export async function fetchLocalListings({ url = DEFAULT_LOCAL_URL, timeoutMs = 30000, targetCount = 0, source = 'yad2-local-api' } = {}) {
  const desired = Math.max(0, Number(targetCount) || 0);
  if (!desired) return fetchLocalBatch({ url, timeoutMs, source });

  const unique = new Map();
  let latest = null;
  const maxBatches = Math.max(1, Math.ceil(desired / 20) * 3);
  for (let page = 1; page <= maxBatches && unique.size < desired; page += 1) {
    latest = await fetchLocalBatch({ url, timeoutMs, page, source });
    for (const listing of latest.listings) unique.set(listing.id, listing);
  }
  if (!latest) return { listings: [], totalCount: 0, totalPages: 0, page: 1, source };
  return {
    ...latest,
    listings: [...unique.values()].slice(0, desired),
    totalCount: Math.min(unique.size, desired),
    batchesFetched: Math.min(maxBatches, Math.ceil(unique.size / 20)),
  };
}

export function filterLocalListings(result, { city, area, minPrice, maxPrice, minRooms, maxRooms } = {}) {
  const listings = result.listings.filter((item) => {
    if (city && !item.city.includes(String(city))) return false;
    if (area && !item.area.includes(String(area))) return false;
    if (minPrice != null && (item.price == null || item.price < minPrice)) return false;
    if (maxPrice != null && (item.price == null || item.price > maxPrice)) return false;
    if (minRooms != null && (item.rooms == null || item.rooms < minRooms)) return false;
    if (maxRooms != null && (item.rooms == null || item.rooms > maxRooms)) return false;
    return true;
  });
  return { ...result, listings, totalCount: listings.length };
}