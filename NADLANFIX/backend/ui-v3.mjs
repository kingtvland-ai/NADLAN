// Verify: private listings, images on the for-sale tab, and the leads tab.
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8000';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1700, height: 1150 } });
const errors = [];
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

try {
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(3000);

  // --- for-sale tab: images ---
  await page.click('#tabs button[data-tab="deals"]');
  await page.waitForTimeout(1500);
  await page.click('button[data-dl="listings"]');
  await page.waitForTimeout(25000);
  console.log('FOR-SALE rows:', await page.locator('#dl-result tbody tr').count());
  console.log('FOR-SALE images:', await page.locator('#dl-result tbody img').count());
  console.log('note:', (await page.locator('#dl-note').innerText()).replace(/\s+/g, ' ').slice(0, 240));
  await page.screenshot({ path: 'ui-v3-forsale.png' });

  // --- leads tab: private filter ---
  await page.click('#tabs button[data-tab="leads"]');
  await page.waitForTimeout(22000);
  console.log('\nLEADS rows:', await page.locator('#ld-result tbody tr').count());
  console.log('LEADS images:', await page.locator('#ld-result tbody img').count());
  console.log('brokerage note:', (await page.locator('#ld-result p.sub').last().innerText()).replace(/\s+/g, ' '));
  await page.check('#ld-private');
  await page.waitForTimeout(9000);
  console.log('private-only:', await page.locator('#ld-count').innerText());
  const row = await page.locator('#ld-result tbody tr').first().locator('td').allInnerTexts();
  console.log('first private row:', JSON.stringify(row).slice(0, 700));
  await page.screenshot({ path: 'ui-v3-leads.png' });

  console.log('\nJS errors:', errors.length ? errors.slice(0, 5) : 'none');
} catch (e) {
  console.error('FAILED:', e.message, errors.slice(0, 4));
} finally {
  await browser.close();
}
