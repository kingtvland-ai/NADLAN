// PlanWatch - WhatsApp bot (Baileys)
// =====================================
// Unofficial WhatsApp client library (@whiskeysockets/baileys), linked to
// the operator's own phone via a one-time QR scan - not the official Meta
// Business API. This was an explicit, informed choice by the operator, who
// was told it risks a ban under WhatsApp's ToS. Auth state persists under
// WHATSAPP_AUTH_DIR (the same mounted disk as the SQLite DB in production,
// see render.yaml) so a container restart does not force a re-scan.
//
// Same trust model as bots/telegram/bot.js: a server-side process on the
// same box, calling dashboard.py's API over loopback with the operator's
// PLANWATCH_BASIC_AUTH - see dashboard.py's _require_user_auth, which
// accepts Basic Auth on the USER_ROUTES search endpoints for exactly this.
//
// The question flow (city -> source -> rooms -> page size -> results) lives
// in bots/shared/conversation.js, identical to the Telegram bot. This bot
// renders every step as plain numbered text ("1. ... 2. ...") rather than
// WhatsApp's "list"/"buttons" messages - Baileys supports both, but they
// render inconsistently (or not at all) on a personal number outside the
// official Business API, and a menu that silently fails to render is worse
// than one that was never attempted. A numeric reply is mapped back to the
// option it named - see resolveIncomingToken - so the same flow Telegram
// drives with real buttons still works here with a typed digit.
//
// QR handling: dashboard.py exposes GET /api/admin/whatsapp-qr (Basic-Auth
// gated, same as the rest of the admin dashboard) which serves whatever PNG
// this process last wrote to WHATSAPP_QR_PATH. The operator opens that URL
// once in a browser and scans with their phone.

import makeWASocket, { useMultiFileAuthState, DisconnectReason } from '@whiskeysockets/baileys';
import { pino } from 'pino';
import QRCode from 'qrcode';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import { writeSetting } from '../shared/api.js';
import { getSession, resetSession } from '../shared/session.js';
import { cityStep, handleInput } from '../shared/conversation.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Must default to the same path dashboard.py falls back to
// (HERE / "data" / "whatsapp-auth") when WHATSAPP_AUTH_DIR is unset, or the
// two processes silently talk about two different sessions: the admin
// dashboard's "linked?" check and QR viewer read Python's default, this
// process would write to a directory two levels up from here instead. In
// production (render.yaml) both always get the same explicit env var, so
// this only bites a local run started without it.
const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, '..', '..', 'data', 'whatsapp-auth');
const QR_PATH = process.env.WHATSAPP_QR_PATH || path.join(AUTH_DIR, 'qr.png');

// File-based remote control from dashboard.py's "הגדרות מנהל" tab - this
// process has no HTTP server of its own, so the admin's "התנתק"/"בדיקת
// חיבור" buttons drop a flag file here instead. See dashboard.py's
// WHATSAPP_DISCONNECT_FLAG/WHATSAPP_TEST_FLAG/WHATSAPP_TEST_RESULT_PATH,
// which must name the same files.
const DISCONNECT_FLAG_PATH = path.join(AUTH_DIR, '.disconnect_requested');
const TEST_FLAG_PATH = path.join(AUTH_DIR, '.test_requested');
const TEST_RESULT_PATH = path.join(AUTH_DIR, 'last_test.json');
const CONTROL_POLL_MS = 4000;

fs.mkdirSync(AUTH_DIR, { recursive: true });

const WELCOME = '👋 שלום, ברוכים הבאים ל-NADLAN4U!\n\n'
  + 'אני בוט לחיפוש נדל"ן בישראל: נכסים למכירה מכל האתרים, הזדמנויות התחדשות עירונית, ובדיקת גוש/חלקה - הכול בשיחה אחת.\n\n'
  + 'בכל שלב אפשר להקליד "עזרה" או "תפריט" כדי לחזור להתחלה.';
const GREETING = /^(שלום|היי|hi|hello|start|תפריט|התחל|menu|עזרה|help)\b/i;

// Deliberately plain text, no WhatsApp "list"/"buttons" messages - Baileys
// supports both, but they render inconsistently (or not at all) on a
// personal number outside the official Business API, and the operator asked
// for the numbered-text flow specifically instead of chasing that
// reliability problem. Sends the same {prompt, options} pack both bots share
// as a numbered list in plain text; remembers it on the session so a bare
// "2" reply can be mapped back to the picked option - see
// resolveIncomingToken below.
async function sendStep(sock, jid, session, pack) {
  const options = pack.options || [];
  session.lastOptions = options;
  const numbered = options.map((o, i) => `${i + 1}. ${o.label}`).join('\n');
  const body = numbered ? `${pack.prompt}\n\n${numbered}\n\n_אפשר להקליד את המספר או את הטקסט המבוקש._` : pack.prompt;
  await sock.sendMessage(jid, { text: body });
}

// Resolves an incoming message to the token conversation.js#handleInput
// expects: a numeric reply mapped against the options from the *previous*
// step, or raw free text.
function resolveIncomingToken(session, text) {
  const trimmed = (text || '').trim();
  if (/^\d+$/.test(trimmed) && session.lastOptions) {
    const picked = session.lastOptions[Number(trimmed) - 1];
    if (picked) return picked.id;
  }
  return trimmed;
}

async function handleIncomingText(sock, jid, rawText) {
  const trimmed = (rawText || '').trim();
  if (!trimmed) return;
  const session = getSession(jid);
  if (GREETING.test(trimmed)) {
    const fresh = resetSession(jid);
    await sock.sendMessage(jid, { text: WELCOME });
    await sendStep(sock, jid, fresh, cityStep());
    return;
  }
  try {
    const token = resolveIncomingToken(session, trimmed);
    const pack = await handleInput(session, token, { onInterim: (m) => sock.sendMessage(jid, { text: m }) });
    await sendStep(sock, jid, session, pack);
  } catch (err) {
    console.error('whatsapp bot search failed:', err.message);
    await sock.sendMessage(jid, { text: 'החיפוש נכשל כרגע. נסו שוב בעוד רגע.' });
  }
}

// Polled every CONTROL_POLL_MS while a socket is live - see the flag-path
// constants' comment above for why this is a file, not a real RPC channel.
async function checkControlFlags(sock) {
  if (fs.existsSync(DISCONNECT_FLAG_PATH)) {
    fs.rmSync(DISCONNECT_FLAG_PATH, { force: true });
    console.log('whatsapp bot: disconnect requested from admin panel - logging out');
    try {
      await sock.logout();
    } catch (err) {
      console.error('whatsapp bot: logout() failed (continuing to wipe local state):', err.message);
    }
    // logout() invalidates the session server-side; wiping the local auth
    // files too means the next start always asks for a fresh QR rather than
    // possibly reusing stale creds.
    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    console.log('whatsapp bot: disconnected - restart this process to link a new number');
    process.exit(0); // not a fatal error for the container - see docker-entrypoint.sh
  }
  if (fs.existsSync(TEST_FLAG_PATH)) {
    fs.rmSync(TEST_FLAG_PATH, { force: true });
    console.log('whatsapp bot: connection test requested from admin panel');
    const result = { at: new Date().toISOString() };
    try {
      const selfJid = sock.user && sock.user.id;
      if (!selfJid) throw new Error('sock.user not set yet (still connecting?)');
      await sock.sendMessage(selfJid, {
        text: `🔔 בדיקת חיבור NADLAN4U — ${new Date().toLocaleString('he-IL')}`,
      });
      result.ok = true;
    } catch (err) {
      result.ok = false;
      result.error = err.message;
    }
    fs.writeFileSync(TEST_RESULT_PATH, JSON.stringify(result));
  }
}

// Writes the just-connected number into the "קישורי שיתוף" settings
// (accounts.SETTINGS_KEYS.whatsapp_bot_number) so the webapp's "המשך שיחה
// ב-WhatsApp" button appears without the operator having to type the
// number in by hand and keep it in sync by memory.
async function publishBotNumber(sock) {
  const jid = sock.user && sock.user.id;
  const number = jid ? jid.split(/[:@]/)[0] : null;
  if (!number) return;
  const ok = await writeSetting('whatsapp_bot_number', number);
  if (ok) console.log(`whatsapp bot: published bot number ${number} to settings`);
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const sock = makeWASocket({
    auth: state,
    logger: pino({ level: 'warn' }),
    printQRInTerminal: false,
  });

  sock.ev.on('creds.update', saveCreds);

  const controlTimer = setInterval(() => {
    checkControlFlags(sock).catch((err) => console.error('whatsapp bot: control check failed:', err.message));
  }, CONTROL_POLL_MS);

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        await QRCode.toFile(QR_PATH, qr, { width: 320 });
        console.log(`whatsapp bot: new QR written to ${QR_PATH} - open /api/admin/whatsapp-qr to scan it`);
      } catch (err) {
        console.error('whatsapp bot: failed to write QR PNG:', err.message);
      }
    }

    if (connection === 'open') {
      console.log('whatsapp bot: connected');
      fs.rm(QR_PATH, { force: true }, () => {});
      publishBotNumber(sock);
    }

    if (connection === 'close') {
      clearInterval(controlTimer);
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      console.warn(`whatsapp bot: connection closed (code ${statusCode}) - ${loggedOut ? 'logged out, re-scan required' : 'reconnecting'}`);
      if (!loggedOut) start().catch((err) => console.error('whatsapp bot: reconnect failed:', err.message));
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (msg.key.fromMe || !msg.message) continue;
      const jid = msg.key.remoteJid;
      const text = msg.message.conversation || msg.message.extendedTextMessage?.text;
      await handleIncomingText(sock, jid, text);
    }
  });
}

start().catch((err) => {
  console.error('whatsapp bot: failed to start -', err.message);
  process.exit(0); // not a fatal error for the container - see docker-entrypoint.sh
});
