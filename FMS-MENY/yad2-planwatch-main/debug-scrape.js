import { buildFeedUrl } from './backend/src/urlBuilder.js';
import { fetchListingsPage } from './backend/src/scraper.js';

(async () => {
  const url = buildFeedUrl('forsale', 'tel-aviv-area', { page: 1 });
  console.log('URL:', url);
  try {
    const data = await fetchListingsPage(url);
    console.log('GOT data keys:', Object.keys(data || {}).slice(0, 20));
    console.log('private length:', data?.data?.private?.length);
    console.log('agency length:', data?.data?.agency?.length);
    console.log('yad1 length:', data?.data?.yad1?.length);
    console.log('pagination:', JSON.stringify(data?.data?.pagination, null, 2));
  } catch (err) {
    console.error('ERROR:', err);
    process.exit(1);
  }
})();
