import { chromium } from 'playwright';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SESSION_STATE_PATH = path.join(__dirname, '..', '..', 'data', 'facebook_storage_state.json');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto('https://www.facebook.com/marketplace/', { waitUntil: 'domcontentloaded', timeout: 60000 });

  console.log('\n========================================');
  console.log('Browser opened to Facebook Marketplace.');
  console.log('Log in to Facebook if prompted.');
  console.log('Once you see the Marketplace page, CLOSE the browser window');
  console.log('to save the session state and exit this script.');
  console.log('========================================\n');

  await new Promise((resolve) => {
    context.on('close', resolve);
  });

  try {
    await context.close();
  } catch {
    // already closed
  }

  const state = await context.storageState();
  await fs.writeFile(SESSION_STATE_PATH, JSON.stringify(state, null, 2));
  console.log(`Session state saved to ${SESSION_STATE_PATH}`);
  await browser.close();
})().catch(err => {
  console.error('Facebook login failed:', err);
  process.exit(1);
});
