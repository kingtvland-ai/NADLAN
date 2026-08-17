import { fetchListingsPage } from './backend/src/scraper.js';

const urls = [
  'https://gw.yad2.co.il/realestate-feed/forsale/feed?page=1',
  'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=0&page=1',
  'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=1&page=1',
  'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=2&page=1',
  'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=3&page=1',
  'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=6&page=1',
  'https://gw.yad2.co.il/realestate-feed/forsale/feed?region=7&page=1',
];

(async () => {
  for (const url of urls) {
    console.log('---', url, '---');
    try {
      const data = await fetchListingsPage(url);
      const pagination = data?.data?.pagination;
      console.log('keys', Object.keys(data || {}).slice(0, 20));
      console.log('private', data?.data?.private?.length, 'agency', data?.data?.agency?.length, 'yad1', data?.data?.yad1?.length);
      console.log('pagination', pagination);
    } catch (err) {
      console.error('ERROR', err);
    }
  }
})();
