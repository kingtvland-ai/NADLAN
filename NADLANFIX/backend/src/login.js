import { getPersistentContext, saveProfile } from './scraper.js';

// One-time manual approval helper for Radware bot protection.
//
// Run: npm run login
// A visible browser opens to yad2. If you see a "Radware Page" / challenge,
// complete it. Once you see the real listings page, CLOSE the browser
// window to save the profile. The scraper will then reuse this profile.

const TARGET = 'https://www.yad2.co.il/realestate/forsale/tel-aviv-area';

(async () => {
  const context = await getPersistentContext(false, true, true);
  const page = await context.newPage();

  await page.goto(TARGET, { waitUntil: 'domcontentloaded', timeout: 60000 });

  console.log('\n========================================');
  console.log('Browser opened to yad2.');
  console.log('If you see a "Radware Page" / challenge, complete it.');
  console.log('Once you see the real listings page, CLOSE the browser window');
  console.log('to save the profile and exit this script.');
  console.log('========================================\n');

  // Wait until the user closes the browser.
  await new Promise((resolve) => {
    context.on('close', resolve);
  });

  try {
    await context.close();
  } catch {
    // already closed
  }

  await new Promise((resolve) => setTimeout(resolve, 300));
  await saveProfile();
  console.log('Profile saved. You can now run `npm run dev`.');
})();