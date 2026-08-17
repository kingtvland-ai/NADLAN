import { chromium } from 'playwright';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';

// Yad2 sits behind Radware bot protection (stormcasterv2.js). A fresh
// headless context always gets the "Radware Page" challenge and never
// passes it. Solution: use a PERSISTENT browser profile. Run `npm run
// login` once to open a visible browser and pass the challenge manually;
// the profile keeps the valid cookies/tokens. Then the scraper reuses
// that profile, navigates to the page, and fetches the gw.yad2.co.il
// feed API from inside the browser (so all Radware cookies/headers are
// attached automatically.)

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BASE_PROFILE_DIR = path.join(__dirname, '..', '.browser-profile');
const ACTIVE_PROFILE_ROOT = path.join(__dirname, '..', '.browser-profile-active');
const ACTIVE_PROFILE_DIR = path.join(ACTIVE_PROFILE_ROOT, String(process.pid));

function ensureDirectory(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function copyDirectory(src, dst) {
  if (!fs.existsSync(src)) return;
  if (fs.existsSync(dst)) {
    fs.rmSync(dst, { recursive: true, force: true });
  }
  fs.cpSync(src, dst, { recursive: true, force: true });
}

async function copyDirectoryWithRetry(src, dst) {
  if (!fs.existsSync(src)) return;
  const maxAttempts = 10;
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function copyLoose(currentSrc, currentDst) {
    let entries;
    try {
      entries = await fs.promises.readdir(currentSrc, { withFileTypes: true });
    } catch (err) {
      if (err.code === 'ENOENT') return;
      throw err;
    }

    await fs.promises.mkdir(currentDst, { recursive: true });

    for (const entry of entries) {
      const srcPath = path.join(currentSrc, entry.name);
      const dstPath = path.join(currentDst, entry.name);

      if (entry.isDirectory()) {
        await copyLoose(srcPath, dstPath);
      } else if (entry.isFile()) {
        try {
          await fs.promises.copyFile(srcPath, dstPath);
        } catch (err) {
          if (err.code === 'ENOENT') continue;
          throw err;
        }
      } else if (entry.isSymbolicLink()) {
        try {
          const linkTarget = await fs.promises.readlink(srcPath);
          await fs.promises.symlink(linkTarget, dstPath);
        } catch (err) {
          if (err.code === 'ENOENT' || err.code === 'EEXIST') continue;
          throw err;
        }
      }
    }
  }

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      if (fs.existsSync(dst)) {
        await fs.promises.rm(dst, { recursive: true, force: true });
      }
      await copyLoose(src, dst);
      return;
    } catch (err) {
      if ((err.code === 'EBUSY' || err.code === 'ENOENT') && attempt < maxAttempts) {
        await delay(200);
        continue;
      }
      throw err;
    }
  }
}

function removeProfileLockFiles(dir) {
  const lockCandidates = ['SingletonLock', 'lockfile', 'LOCK', 'SingletonSocket-0', 'SingletonSocket-1', 'SingletonSocket-2'];
  for (const file of lockCandidates) {
    const pathToFile = path.join(dir, file);
    if (fs.existsSync(pathToFile)) {
      try {
        fs.rmSync(pathToFile, { recursive: true, force: true });
      } catch {
        // ignore stale lock cleanup failures
      }
    }
  }
}

function prepareProfileDir(ignoreBaseProfile = false) {
  ensureDirectory(ACTIVE_PROFILE_ROOT);
  if (fs.existsSync(ACTIVE_PROFILE_DIR)) {
    fs.rmSync(ACTIVE_PROFILE_DIR, { recursive: true, force: true });
  }

  if (!ignoreBaseProfile && fs.existsSync(BASE_PROFILE_DIR)) {
    copyDirectory(BASE_PROFILE_DIR, ACTIVE_PROFILE_DIR);
  } else {
    ensureDirectory(ACTIVE_PROFILE_DIR);
  }

  removeProfileLockFiles(ACTIVE_PROFILE_DIR);
  return ACTIVE_PROFILE_DIR;
}

let persistentContextPromise = null;
let fetchQueue = Promise.resolve();

async function createPersistentContext(headless = true, allowMissingProfile = false, ignoreBaseProfile = false) {
  if (!fs.existsSync(BASE_PROFILE_DIR) && !allowMissingProfile) {
    throw new Error(
      'Persistent Yad2 browser profile not found. Run `npm run login` once and complete the challenge to create backend/.browser-profile.'
    );
  }

  const profileDir = prepareProfileDir(ignoreBaseProfile);
  return chromium.launchPersistentContext(profileDir, {
    headless,
    channel: 'chrome',
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
    locale: 'he-IL',
    viewport: { width: 1366, height: 768 },
    args: ['--disable-blink-features=AutomationControlled'],
  });
}

export async function getPersistentContext(headless = true, allowMissingProfile = false, ignoreBaseProfile = false) {
  if (!persistentContextPromise) {
    persistentContextPromise = createPersistentContext(headless, allowMissingProfile, ignoreBaseProfile);
  }

  try {
    return await persistentContextPromise;
  } catch (err) {
    persistentContextPromise = null;
    throw err;
  }
}

async function resetBrowser() {
  if (!persistentContextPromise) return;

  try {
    const context = await persistentContextPromise;
    await context.close();
  } catch {
    // ignore
  }

  persistentContextPromise = null;
  if (fs.existsSync(ACTIVE_PROFILE_DIR)) {
    try {
      fs.rmSync(ACTIVE_PROFILE_DIR, { recursive: true, force: true });
    } catch {
      // ignore cleanup failure
    }
  }
}

export async function saveProfile() {
  if (!fs.existsSync(ACTIVE_PROFILE_DIR)) return;
  await copyDirectoryWithRetry(ACTIVE_PROFILE_DIR, BASE_PROFILE_DIR);
}

function enqueueFetch(fn) {
  const next = fetchQueue.then(() => fn());
  fetchQueue = next.catch(() => {});
  return next;
}

function isRecoverablePlaywrightError(err) {
  if (!err || typeof err.message !== 'string') return false;
  return [
    'Target page, context or browser has been closed',
    'browserType.launchPersistentContext: Opening in existing browser session',
    'Context closed',
    'Page closed',
    'Cannot find context with specified id',
    'The profile is already in use',
  ].some((token) => err.message.includes(token));
}

/**
 * Fetch the gw.yad2.co.il feed API from inside the browser context so all
 * Radware cookies/headers are attached automatically.
 *
 * @param {string} feedUrl - full gw.yad2.co.il feed API URL
 */
export async function fetchListingsPage(feedUrl) {
  return enqueueFetch(async () => {
    let lastError = null;

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const context = await getPersistentContext(false);
      const page = await context.newPage();

      try {
        await page.goto('https://www.yad2.co.il/realestate/forsale/tel-aviv-area', {
          waitUntil: 'domcontentloaded',
          timeout: 30000,
        });
        await page.waitForTimeout(3000);

        const json = await page.evaluate(async (url) => {
          const res = await fetch(url, {
            headers: {
              Accept: 'application/json, text/plain, */*',
              Origin: 'https://www.yad2.co.il',
              Referer: 'https://www.yad2.co.il/',
            },
            credentials: 'include',
          });
          if (!res.ok) throw new Error(`Feed fetch failed: ${res.status}`);
          return res.json();
        }, feedUrl);

        await page.close();
        return json;
      } catch (err) {
        lastError = err;
        try {
          await page.close();
        } catch {
          // ignore
        }

        if (isRecoverablePlaywrightError(err) && attempt === 0) {
          await resetBrowser();
          continue;
        }

        await resetBrowser();
        throw err;
      }
    }

    throw lastError;
  });
}

export async function closeBrowser() {
  await resetBrowser();
}
