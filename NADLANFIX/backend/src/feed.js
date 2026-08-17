// Access to Yad2's real search feed - the one that carries PRIVATE listings.
//
// Why this exists alongside scraper.js
// ------------------------------------
// The local curl2api connector reads `props.pageProps.lobbyData.platinumAds`
// off the lobby page. That array is the *paid promotion carousel*, which is
// why every one of the 14,445 rows harvested through it had an agency and not
// one was private. It is also small: 20 promoted ads per page.
//
// The real feed is `gw.yad2.co.il/realestate-feed/forsale/feed`, and measured
// on 2026-08-05 it carries, per page: 20 `private` + 20 `agency` + promoted
// slots, with a per-region total. Across the six regions that is **87,672**
// listings against the carousel's 14,445, and roughly half of them are private
// sellers - the ones an agent most wants to reach.
//
// The catch is that the endpoint is only reachable from inside a browser that
// holds a live Yad2 session. Fetched directly it answers 502/400; fetched with
// `page.evaluate` from a tab that has loaded yad2.co.il it answers 200 - even
// when that tab itself shows the Radware interstitial, because the session
// cookies are attached either way. So this module keeps one page open and
// issues the feed calls from within it.
//
// `adType` on each row is Yad2's own classification, which means brokerage
// here is a stated fact rather than something inferred from a missing field.

import { getPersistentContext, closeBrowser } from './scraper.js';

const HOST = 'https://gw.yad2.co.il';

//: The six regions that together cover the country. Yad2 rejects a feed
//: request with no region (HTTP 400), so there is no "everything" call.
export const REGIONS = [1, 2, 3, 5, 6, 7];

//: Arrays in the response that hold real listings. `yad1` is new-build
//: developer inventory with a different shape and is deliberately excluded.
const LISTING_ARRAYS = ['private', 'agency', 'platinum', 'kingOfTheHar',
                        'trio', 'booster', 'leadingBroker'];

let pagePromise = null;

async function openPage() {
  const context = await getPersistentContext(false, true, false);
  const page = await context.newPage();
  // One navigation establishes the session; every later feed call reuses it.
  await page.goto('https://www.yad2.co.il/realestate/forsale',
    { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(5000);
  return page;
}

async function getPage() {
  if (pagePromise) {
    const existing = await pagePromise.catch(() => null);
    if (existing && !existing.isClosed()) return existing;
    pagePromise = null;
  }
  pagePromise = openPage();
  return pagePromise;
}

//: A full pass is ~3,800 requests over several hours, and the browser does not
//: always survive that: a crashed tab or a closed context turns every later
//: call into a 502, which is exactly how the first long run died at region 2
//: page 268 after 25 consecutive failures. Recreating the session on that class
//: of error costs one navigation and rescues the rest of the pass.
function isDeadSession(err) {
  const msg = String(err?.message || err);
  return ['Target page, context or browser has been closed',
          'Target closed', 'Page closed', 'Context closed',
          'Execution context was destroyed',
          'browser has been closed',
          'Protocol error'].some((t) => msg.includes(t));
}

async function resetPage() {
  const page = await pagePromise?.catch(() => null);
  if (page && !page.isClosed()) await page.close().catch(() => {});
  pagePromise = null;
  await closeBrowser().catch(() => {});
}

function get(obj, path, fallback = null) {
  return path.reduce((acc, k) => (acc == null ? acc : acc[k]), obj) ?? fallback;
}

//: Flatten one feed row into the flat shape the Python store expects. Kept
//: deliberately close to the connector's field names so both sources land in
//: the same table without a second code path.
function normalise(item, arrayName) {
  const token = item?.token || item?.adNumber;
  if (!token) return null;
  const images = get(item, ['metaData', 'images'], []) || [];
  return {
    token: String(token),
    price: item.price ?? null,
    rooms: get(item, ['additionalDetails', 'roomsCount']),
    sqm: get(item, ['additionalDetails', 'squareMeter']),
    property_type: get(item, ['additionalDetails', 'property', 'text']),
    city: get(item, ['address', 'city', 'text']),
    area: get(item, ['address', 'area', 'text']),
    neighborhood: get(item, ['address', 'neighborhood', 'text']),
    street: get(item, ['address', 'street', 'text']),
    house_number: get(item, ['address', 'house', 'number']),
    floor: get(item, ['address', 'house', 'floor']),
    lat: get(item, ['address', 'coords', 'lat']),
    lon: get(item, ['address', 'coords', 'lon']),
    // Yad2's own word for who is selling. `private` here is a published fact,
    // not an inference from an absent agency field.
    ad_type: item.adType || arrayName,
    agency: get(item, ['customer', 'agencyName'])
            || get(item, ['customer', 'name']) || null,
    image: get(item, ['metaData', 'coverImage']) || images[0] || null,
    //: The whole gallery, not a cover. The cap was 10, which clipped a quarter
    //: of the harvested ads to exactly ten photos and left the lightbox showing
    //: less than the ad does. 30 is still a bound - a listing claiming hundreds
    //: of photos is a malformed row, not a generous seller - but it is above
    //: anything the feed has actually published.
    images: images.slice(0, 30),
    condition_id: get(item, ['additionalDetails', 'propertyCondition', 'id']),
  };
}

/**
 * One page of the real feed for one region.
 * @returns {{rows: object[], total: number|null, totalPages: number|null}}
 */
export async function fetchFeedPage(region, page = 1) {
  const url = `${HOST}/realestate-feed/forsale/feed?region=${Number(region)}&page=${Number(page)}`;

  let payload = null;
  let lastError = null;
  // Two attempts: the second runs on a freshly opened session, which is what
  // recovers a pass whose browser died mid-way.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const target = await getPage();
      payload = await target.evaluate(async (u) => {
        const res = await fetch(u, {
          headers: { Accept: 'application/json, text/plain, */*' },
          credentials: 'include',
        });
        if (!res.ok) return { __error: `HTTP ${res.status}` };
        return res.json();
      }, url);
      break;
    } catch (err) {
      lastError = err;
      if (attempt === 0 && isDeadSession(err)) {
        await resetPage();
        continue;
      }
      throw err;
    }
  }
  if (!payload) throw lastError || new Error('feed fetch failed');
  if (payload?.__error) throw new Error(payload.__error);
  const data = payload?.data ?? payload ?? {};
  const rows = [];
  for (const name of LISTING_ARRAYS) {
    const arr = data[name];
    if (!Array.isArray(arr)) continue;
    for (const item of arr) {
      const row = normalise(item, name);
      if (row) rows.push(row);
    }
  }
  return {
    rows,
    total: get(data, ['pagination', 'total']),
    totalPages: get(data, ['pagination', 'totalPages']),
  };
}

export async function closeFeed() {
  const page = await pagePromise?.catch(() => null);
  if (page && !page.isClosed()) await page.close().catch(() => {});
  pagePromise = null;
}
