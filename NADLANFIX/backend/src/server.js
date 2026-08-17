import express from 'express';
import cors from 'cors';
import path from 'path';
import { fileURLToPath } from 'url';
import { buildFeedUrl, getRegionIds } from './urlBuilder.js';
import { fetchListingsPage } from './scraper.js';
import { normalizeListingsResponse } from './normalize.js';
import { getCached, setCached, cacheKeyFromFilters } from './cache.js';
import { fetchLocalListings, filterLocalListings, normalizeLocalListing } from './localListings.js';
import { fetchFeedPage, REGIONS } from './feed.js';
import { loadOnmapRows } from './onmapDb.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, '../../public-site')));

const PYTHON_API_BASE = process.env.PYTHON_API_BASE || 'http://127.0.0.1:8000';

//: מפת מקורות הפנימיים (8099) — כל מקור הוא endpoint עצמאי בעל מעטפת
//: { data, record_count, drift_status, ... }. "onmap" הוא מקור הנכסים
//: החדש של onmap.co.il (phoenix.onmap.co.il/v1/properties).
const LOCAL_LISTINGS_SOURCES = {
  yad2: process.env.YAD2_LOCAL_LISTINGS_URL || 'http://127.0.0.1:8099/api/yad2',
  onmap: 'http://127.0.0.1:8099/api/onmap',
};

//: מקור "על המפה" מוגש מהמאגר המקומי של PlanWatch (כל הרשומות שגורף
//: onmap_feed.py), לא מה-API החיצוני 8099 שמחזיר רק ~38.
const DB_LISTING_SOURCE_URL = 'planwatch.sqlite3 → onmap_listings';

async function proxyToPython(req, res) {
  const pythonPath = req.originalUrl
    .replace(/^\/api\/planwatch/, '/api')
    .replace(/^\/api\/dashboard/, '/api');
  const url = `${PYTHON_API_BASE}${pythonPath}`;
  const init = {
    method: req.method,
    headers: {
      accept: req.headers.accept || 'application/json',
      'content-type': req.headers['content-type'] || 'application/json',
    },
    body: ['GET', 'HEAD'].includes(req.method) ? undefined : JSON.stringify(req.body),
  };

  try {
    const response = await fetch(url, init);
    for (const [name, value] of response.headers) {
      if (name.toLowerCase() === 'transfer-encoding') continue;
      res.setHeader(name, value);
    }
    res.status(response.status);
    const buffer = Buffer.from(await response.arrayBuffer());
    res.send(buffer);
  } catch (err) {
    console.error('Python proxy error:', err);
    res.status(502).json({ error: 'Failed to connect to PlanWatch service' });
  }
}

app.all('/api/planwatch', proxyToPython);
app.all('/api/planwatch/*', proxyToPython);
app.all('/api/dashboard', proxyToPython);
app.all('/api/dashboard/*', proxyToPython);

const PROXY_ROUTES = [
  '/api/bootstrap',
  '/api/integrations',
  '/api/market',
  '/api/opportunities',
  '/api/live-sources',
  '/api/listings',
  '/api/local-listings',
];
for (const route of PROXY_ROUTES) {
  app.all(route, proxyToPython);
  app.all(`${route}/*`, proxyToPython);
}

app.get('/api/local-listings', async (req, res) => {
  try {
    const sourceKey = req.query.source || 'yad2';

    // ONMAP: מגישים ישירות מהמאגר של PlanWatch (כל הרשומות שגורף
    // onmap_feed.py), במקום מהמקור החיצוני 8099 שמחזיר רק ~38 בגלל
    // ש-pagination מושבת בהגדרת ה-scraper.
    let result;
    if (sourceKey === 'onmap') {
      const { data } = loadOnmapRows();
      const listings = data
        .map((row) => normalizeLocalListing(row, 'onmap'))
        .filter(Boolean);
      result = {
        listings,
        totalCount: listings.length,
        totalPages: 1,
        page: 1,
        source: 'onmap',
        sourceName: 'על המפה (מאגר PlanWatch)',
        sourceUrl: DB_LISTING_SOURCE_URL,
      };
    } else {
      const sourceUrl = LOCAL_LISTINGS_SOURCES[sourceKey]
        || req.query.url
        || LOCAL_LISTINGS_SOURCES.yad2;
      result = await fetchLocalListings({ url: sourceUrl, targetCount: req.query.limit || 0, source: sourceKey });
    }

    const filtered = filterLocalListings(result, {
      city: req.query.city,
      area: req.query.area,
      minPrice: req.query.minPrice == null ? null : Number(req.query.minPrice),
      maxPrice: req.query.maxPrice == null ? null : Number(req.query.maxPrice),
      minRooms: req.query.minRooms == null ? null : Number(req.query.minRooms),
      maxRooms: req.query.maxRooms == null ? null : Number(req.query.maxRooms),
    });
    res.json({ ...filtered, fromCache: false });
  } catch (error) {
    console.error('Local listings API error:', error.message);
    res.status(502).json({ error: error.message || 'Failed to fetch local listings API' });
  }
});

async function fetchAllListings(dealType, regionSlug, query = {}) {
  const regionIds = getRegionIds(regionSlug);
  const allListings = [];
  let totalPages = 0;

  for (const regionId of regionIds) {
    const firstPage = await fetchListingsPage(buildFeedUrl(dealType, regionId, { ...query, page: 1 }));
    const firstNormalized = normalizeListingsResponse(firstPage);
    allListings.push(...firstNormalized.listings);
    const regionTotalPages = firstNormalized.totalPages || 1;
    totalPages = Math.max(totalPages, regionTotalPages);

    for (let page = 2; page <= regionTotalPages; page += 1) {
      const pageData = await fetchListingsPage(buildFeedUrl(dealType, regionId, { ...query, page }));
      const pageNormalized = normalizeListingsResponse(pageData);
      allListings.push(...pageNormalized.listings);
    }
  }

  return {
    listings: allListings,
    totalCount: allListings.length,
    totalPages: regionIds.length > 1 ? null : totalPages,
    page: 1,
    allRegions: regionIds.length > 1,
    allPages: true,
  };
}

// GET /api/listings?dealType=forsale&regionSlug=tel-aviv-area&area=1&city=5000&page=1
app.get('/api/listings', async (req, res) => {
  const dealType = req.query.dealType || 'forsale';
  const regionSlug = req.query.regionSlug;
  const page = req.query.page ? Number(req.query.page) : 1;

  if (!regionSlug) {
    return res.status(400).json({ error: 'regionSlug is required' });
  }

  // Build query params for the gw.yad2.co.il feed API
  const query = {};
  if (req.query.area) query.area = req.query.area;
  if (req.query.city) query.city = req.query.city;
  if (page > 1) query.page = String(page);

  const allPages = req.query.all === '1' || req.query.all === 'true';
  const filters = {
    dealType,
    regionSlug,
    area: req.query.area ? Number(req.query.area) : undefined,
    city: req.query.city ? Number(req.query.city) : undefined,
    page,
    all: allPages,
  };

  const cacheKey = cacheKeyFromFilters(filters);
  const cached = getCached(cacheKey);
  if (cached) {
    return res.json({ ...cached, fromCache: true });
  }

  try {
    let result;
    const regionIds = getRegionIds(regionSlug);

    if (allPages) {
      result = await fetchAllListings(dealType, regionSlug, query);
    } else if (regionIds.length === 1) {
      const feedUrl = buildFeedUrl(dealType, regionIds[0], query);
      const raw = await fetchListingsPage(feedUrl);
      result = normalizeListingsResponse(raw);
    } else {
      const listings = [];
      for (const regionId of regionIds) {
        const raw = await fetchListingsPage(buildFeedUrl(dealType, regionId, query));
        const normalized = normalizeListingsResponse(raw);
        listings.push(...normalized.listings);
      }
      result = {
        listings,
        totalCount: listings.length,
        totalPages: null,
        page,
        allRegions: true,
      };
    }

    setCached(cacheKey, result);
    res.json({ ...result, fromCache: false });
  } catch (err) {
    console.error('Failed to fetch listings:', err.message);
    res.status(502).json({ error: err.message || 'Failed to fetch listings from source' });
  }
});

// Raw access to Yad2's real search feed, for the PlanWatch harvester.
//
// Deliberately unfiltered: normalizeListingsResponse() drops any row without a
// posting date and anything outside 2025-2026, which is right for the React UI
// and wrong for a store that wants every listing and derives dates itself.
// This returns what the feed said, with `ad_type` intact so private sellers
// stay identifiable.
app.get('/api/yad2-feed', async (req, res) => {
  const region = Number(req.query.region || 3);
  const page = Number(req.query.page || 1);
  if (!REGIONS.includes(region)) {
    return res.status(400).json({ error: `region must be one of ${REGIONS}` });
  }
  try {
    const result = await fetchFeedPage(region, page);
    res.json({ region, page, ...result });
  } catch (err) {
    console.error('feed error:', err.message);
    res.status(502).json({ error: err.message || 'feed fetch failed' });
  }
});

app.get('/api/yad2-feed/regions', (_req, res) => res.json({ regions: REGIONS }));

app.get('/api/health', (_req, res) => res.json({ ok: true }));

const PORT = process.env.PORT || process.argv[2] || 4000;
app.listen(PORT, () => {
  console.log(`API listening on http://localhost:${PORT}`);
});
