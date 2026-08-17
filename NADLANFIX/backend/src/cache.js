import NodeCache from 'node-cache';

// Cache search results for 15 minutes. Real-estate listings don't change
// second-to-second, and caching is the single biggest thing that keeps you
// off yad2's radar and keeps your app fast.
const cache = new NodeCache({ stdTTL: 15 * 60, checkperiod: 60 });

export function getCached(key) {
  return cache.get(key);
}

export function setCached(key, value) {
  cache.set(key, value);
}

export function cacheKeyFromFilters(filters) {
  return JSON.stringify(filters, Object.keys(filters).sort());
}
