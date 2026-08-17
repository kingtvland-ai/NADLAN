// Verify ONMAP normalization with real API response
import { fetchLocalListings } from './backend/src/localListings.js';

const result = await fetchLocalListings({
  url: 'http://127.0.0.1:8099/api/onmap',
  source: 'onmap',
  targetCount: 0,
});

console.log('Total:', result.totalCount);
console.log('Normalized listings:', result.listings.length);
console.log('Drift:', result.driftStatus, '-', result.driftNote || '');

if (result.listings.length > 0) {
  const sample = result.listings[0];
  console.log('--- Sample listing fields ---');
  console.log('id:', sample.id);
  console.log('title:', sample.title);
  console.log('city:', sample.city);
  console.log('price:', sample.price);
  console.log('rooms:', sample.rooms);
  console.log('squareMeters:', sample.squareMeters);
  console.log('floor:', sample.floor);
  console.log('street:', sample.street);
  console.log('neighborhood:', sample.neighborhood);
  console.log('image:', sample.image?.slice(0, 80));
  console.log('images count:', sample.images?.length);
  console.log('lat:', sample.latitude, 'lon:', sample.longitude);
  console.log('url:', sample.url);

  const withCity = result.listings.filter((l) => l.city);
  const withRooms = result.listings.filter((l) => l.rooms != null);
  const withPrice = result.listings.filter((l) => l.price != null);
  const withImage = result.listings.filter((l) => l.image);
  console.log('--- Coverage ---');
  console.log('with city:', withCity.length);
  console.log('with rooms:', withRooms.length);
  console.log('with price:', withPrice.length);
  console.log('with image:', withImage.length);

  const missing = result.listings.filter((l) => !l.city || l.price == null);
  if (missing.length) {
    console.log('--- Missing city/price samples ---');
    for (const m of missing.slice(0, 3)) {
      console.log(JSON.stringify({ id: m.id, title: m.title, city: m.city, price: m.price }));
    }
  }
}

if (result.listings.length === 0) {
  console.error('FAIL: no listings normalized!');
  process.exit(1);
}

const critical = result.listings.filter((l) => !l.city && l.price == null);
if (critical.length > result.listings.length * 0.5) {
  console.error('FAIL: ' + critical.length + '/' + result.listings.length + ' listings missing city+price');
  process.exit(1);
}

console.log('PASS: ONMAP normalization works');