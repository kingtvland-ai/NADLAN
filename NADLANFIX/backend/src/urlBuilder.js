const HOST = 'https://gw.yad2.co.il';

// Map region slugs to yad2 region IDs
const REGION_IDS = {
  'tel-aviv-area': 3,
  'center-and-sharon': 1,
  'south': 2,
  'north-and-valleys': 7,
  'jerusalem-area': 6,
  'sharon': 1,
  'haifa-and-creatures': 5,
  'all': [1, 2, 3, 5, 6, 7],
};

function getRegionIds(regionSlug) {
  const regions = REGION_IDS[regionSlug];
  if (regions == null) {
    return [3];
  }
  return Array.isArray(regions) ? regions : [regions];
}

function buildFeedUrl(dealType, regionId, query = {}) {
  const params = new URLSearchParams();
  if (regionId != null) params.set('region', String(regionId));
  if (query.area) params.set('area', String(query.area));
  if (query.city) params.set('city', String(query.city));
  if (query.page != null) params.set('page', String(query.page));
  return `${HOST}/realestate-feed/${dealType}/feed?${params.toString()}`;
}

export { buildFeedUrl, getRegionIds, HOST };