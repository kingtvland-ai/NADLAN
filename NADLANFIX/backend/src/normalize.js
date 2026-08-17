// Normalizes the gw.yad2.co.il feed API JSON response into a clean listings array.
// The response shape is:
//   data.private       -> array of private listing cards
//   data.agency        -> array of agency listing cards
//   data.yad1          -> array of new-project listing cards
//   data.pagination    -> { total, totalPages }
// Each item has fields like: token, price, address.city.text,
// address.neighborhood.text, additionalDetails.roomsCount,
// additionalDetails.squareMeter, metaData.coverImage, etc.

function get(obj, path, fallback = undefined) {
  return path.reduce((acc, key) => (acc == null ? acc : acc[key]), obj) ?? fallback;
}

// Inline SVG fallback so every listing card renders an image even when the
// feed carries none. Kept as a data URI so no external request is needed.
function placeholderImage(city = '') {
  const place = (city || 'נכס').slice(0, 16);
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="400">` +
    `<rect width="600" height="400" fill="#e5e7eb"/>` +
    `<rect x="20" y="20" width="560" height="360" fill="none" stroke="#d1d5db" stroke-width="2"/>` +
    `<text x="300" y="180" text-anchor="middle" font-size="72">🏠</text>` +
    `<text x="300" y="260" text-anchor="middle" font-size="26" fill="#6b7280" font-family="Arial, sans-serif">${place}</text>` +
    `</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

export function normalizeListingsResponse(raw) {
  const data = get(raw, ['data'], {});

  // Collect all listing arrays (private, agency, yad1, platinum, etc.)
  const listingArrays = [
    'private',
    'agency',
    'yad1',
    'platinum',
    'kingOfTheHar',
    'trio',
    'booster',
    'leadingBroker',
  ];

  const items = [];
  for (const key of listingArrays) {
    const arr = data[key];
    if (Array.isArray(arr)) items.push(...arr);
  }

  const listings = items.map(normalizeListing).filter((l) => l && l.id && l.postedAt && isInDesiredYears(l.postedAt));

  return {
    listings,
    totalCount: get(data, ['pagination', 'total'], listings.length),
    totalPages: get(data, ['pagination', 'totalPages'], 1),
    page: 1,
  };
}

function normalizeListing(item) {
  if (!item) return null;

  const id = item.token || item.id || item.adId;
  const title = item.title || get(item, ['address', 'city', 'text'], '') || '';
  const subtitle = item.subTitle || get(item, ['additionalDetails', 'property', 'text'], '') || '';
  const price = item.price ?? null;
  const city = get(item, ['address', 'city', 'text'], '') || item.city || '';
  const neighborhood = get(item, ['address', 'neighborhood', 'text'], '') || '';
  const street = get(item, ['address', 'street', 'text'], '') || '';
  const houseNumber = get(item, ['address', 'house', 'number'], null);
  const floor = get(item, ['address', 'house', 'floor'], null);
  const rooms = get(item, ['additionalDetails', 'roomsCount'], item.rooms ?? null);
  const squareMeters = get(item, ['additionalDetails', 'squareMeter'], item.squaremeter ?? null);
  const rawImages = get(item, ['metaData', 'images'], []);
  const image = get(item, ['metaData', 'coverImage'], null) || item.image || item.img_url || placeholderImage(city);
  const imageList = (Array.isArray(rawImages) && rawImages.length ? rawImages : [image]).filter(Boolean);
  const url = id ? `https://www.yad2.co.il/realestate/item/${id}` : null;
  const postedAt = extractPostingDate(item);
  // Yad2's own classification of who is selling: `private` / `agency` etc.
  // "בלי תיווך" (no brokerage) means the seller is the owner, so the
  // brokerage filter can be derived from a published fact rather than from
  // the absence of an agency name.
  const adType = item.adType || item.ad_type || null;
  const agency = get(item, ['customer', 'agencyName'], null)
    || get(item, ['customer', 'name'], null) || null;
  const isPrivate = adType === 'private' || (!agency && adType !== 'agency');

  return {
    id,
    title,
    subtitle,
    price,
    city,
    neighborhood,
    street,
    houseNumber,
    floor,
    rooms,
    squareMeters,
    image,
    images: imageList,
    url,
    postedAt,
    adType,
    agency,
    isPrivate,
  };
}

function extractPostingDate(item) {
  // Try a bunch of likely fields that may contain a posting/published date
  const candidates = [
    get(item, ['metaData', 'publishedAt']),
    get(item, ['metaData', 'publishedDate']),
    get(item, ['metaData', 'postedAt']),
    get(item, ['createdAt']),
    get(item, ['created_at']),
    get(item, ['publicationDate']),
    get(item, ['publication', 'date']),
    get(item, ['date']),
    get(item, ['postDate']),
  ];

  for (const cand of candidates) {
    if (!cand) continue;
    const d = new Date(cand);
    if (!Number.isNaN(d.getTime())) return d.toISOString();
  }

  // Fallback: try to extract a YYYYMMDD or YYYYMM pattern from image URL or any string fields
  const text = JSON.stringify(item);
  const mFull = text.match(/(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])/);
  if (mFull) {
    const iso = `${mFull[1]}-${mFull[2]}-${mFull[3]}T00:00:00.000Z`;
    return new Date(iso).toISOString();
  }

  const mYearMonth = text.match(/(20\d{2})(0[1-9]|1[0-2])/);
  if (mYearMonth) {
    const iso = `${mYearMonth[1]}-${mYearMonth[2]}-01T00:00:00.000Z`;
    return new Date(iso).toISOString();
  }

  return null;
}

function isInDesiredYears(isoDateStr) {
  if (!isoDateStr) return false;
  const d = new Date(isoDateStr);
  if (Number.isNaN(d.getTime())) return false;
  const year = d.getUTCFullYear();
  return year === 2025 || year === 2026;
}