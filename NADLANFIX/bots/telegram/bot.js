// PlanWatch - Telegram bot
// ==========================
// Button-driven listing search over Telegram, talking to the existing
// dashboard.py API on the same box (loopback, PYTHON_API_BASE) using the
// operator's PLANWATCH_BASIC_AUTH - the same trust model backend/src/server.js
// already uses. This is a *server-side trusted process*, not a public
// browser, so it does not need the end-user bearer-token layer the webapp
// uses (see accounts.py / dashboard.py's USER_ROUTES).
//
// The question flow (city -> source -> rooms -> results) lives in
// bots/shared/conversation.js so it stays identical to the WhatsApp bot;
// this file only turns that flow's {prompt, options} into Telegram's
// inline_keyboard and wires up Telegram-specific plumbing (polling, token
// resolution, publishing the bot's own @username).
//
// Long polling, not a webhook: simplest to run alongside the other
// processes in the same container with no public callback URL to manage.
// A crashed/misconfigured bot must not take the dashboard down - see
// docker-entrypoint.sh, which starts this backgrounded and only logs if it
// exits early.

import TelegramBot from 'node-telegram-bot-api';
import { readSetting, writeSetting } from '../shared/api.js';
import { getSession } from '../shared/session.js';
import { cityStep, handleInput } from '../shared/conversation.js';

const BASIC_AUTH_SET = !!process.env.PLANWATCH_BASIC_AUTH;

// The env var wins when set (that's what render.yaml/DEPLOY.md document),
// but the operator can also paste the token into the "הגדרות מנהל" tab in
// dashboard.html, which writes it to the settings table - handy since this
// bot is a long-running process the operator would otherwise have to
// redeploy just to rotate a token. See accounts.SECRET_SETTINGS_KEYS.
async function resolveToken() {
  if (process.env.PLANWATCH_TELEGRAM_TOKEN) return process.env.PLANWATCH_TELEGRAM_TOKEN;
  return readSetting('telegram_bot_token');
}

const TOKEN = await resolveToken();
if (!TOKEN) {
  console.error('telegram bot: no token - set PLANWATCH_TELEGRAM_TOKEN or paste one into '
    + 'הגדרות מנהל in dashboard.html. Bot will not start.');
  process.exit(0); // not a fatal error for the container - see docker-entrypoint.sh
}
if (!BASIC_AUTH_SET) {
  console.warn('telegram bot: PLANWATCH_BASIC_AUTH not set - search calls to the API will be unauthenticated '
    + 'and will fail once the API requires auth (e.g. PLANWATCH_DEPLOY=render).');
}

// Telegram inline_keyboard rows of 2 read comfortably on a phone without
// forcing a long single column for the 8-city quick list.
function toKeyboard(options) {
  const rows = [];
  for (let i = 0; i < options.length; i += 2) {
    rows.push(options.slice(i, i + 2).map((o) => ({ text: o.label, callback_data: o.id })));
  }
  return { reply_markup: { inline_keyboard: rows } };
}

// Telegram caps a single message at 4096 chars. Now that the user can pick
// up to 50 results per page (see bots/shared/api.js RESULTS_PAGE_SIZES),
// that cap is routinely hit, not a defensive edge case - so this splits on
// the blank line between listing rows (runSearch in conversation.js joins
// rows with "\n\n") rather than an arbitrary character count, which used to
// risk slicing a row's URL in half and leaving an unclickable half-link in
// one of the two messages.
function chunkPrompt(prompt, max) {
  const paragraphs = prompt.split('\n\n');
  const chunks = [];
  let current = '';
  for (const para of paragraphs) {
    const candidate = current ? `${current}\n\n${para}` : para;
    if (candidate.length > max && current) {
      chunks.push(current);
      current = para;
    } else {
      current = candidate;
    }
    // A single paragraph longer than `max` on its own (should not happen
    // for a listing row, but a "did you mean" prompt with many suggestions
    // could) still needs a hard split so sendMessage never gets a >4096
    // string and errors out silently.
    while (current.length > max) {
      chunks.push(current.slice(0, max));
      current = current.slice(max);
    }
  }
  if (current) chunks.push(current);
  return chunks.length ? chunks : [prompt];
}

async function sendStep(chatId, pack) {
  const opts = pack.options && pack.options.length ? toKeyboard(pack.options) : {};
  const MAX = 3900;
  if (pack.prompt.length <= MAX) {
    return bot.sendMessage(chatId, pack.prompt, opts);
  }
  const chunks = chunkPrompt(pack.prompt, MAX);
  for (let i = 0; i < chunks.length - 1; i++) await bot.sendMessage(chatId, chunks[i]);
  return bot.sendMessage(chatId, chunks[chunks.length - 1], opts);
}

const bot = new TelegramBot(TOKEN, { polling: true });

bot.on('polling_error', (err) => console.error('telegram bot polling error:', err.message));

// Writes this bot's @username into the "קישורי שיתוף" settings
// (accounts.SETTINGS_KEYS.telegram_bot_username) so the webapp's "המשך שיחה
// ב-Telegram" button appears without the operator having to look up and
// type the username in by hand.
bot.getMe().then(async (me) => {
  if (!me.username) return;
  const ok = await writeSetting('telegram_bot_username', me.username);
  if (ok) console.log(`telegram bot: published bot username @${me.username} to settings`);
}).catch((err) => console.error('telegram bot: getMe failed:', err.message));

bot.onText(/^\/(start|menu|reset|help)/, async (msg) => {
  const session = getSession(msg.chat.id);
  Object.assign(session, { step: 'mode', mode: '', locality: '', source: '', minRooms: '', maxRooms: '', offset: 0 });
  await bot.sendMessage(
    msg.chat.id,
    '👋 שלום, ברוכים הבאים ל-NADLAN4U!\n\n'
    + 'אני בוט לחיפוש נדל"ן בישראל: נכסים למכירה מכל האתרים, הזדמנויות התחדשות עירונית, ובדיקת גוש/חלקה - הכול בשיחה אחת.\n\n'
    + 'בכל שלב אפשר לשלוח /start כדי לחזור להתחלה.',
  );
  await sendStep(msg.chat.id, cityStep());
});

bot.on('callback_query', async (query) => {
  bot.answerCallbackQuery(query.id).catch(() => {});
  const chatId = query.message.chat.id;
  const session = getSession(chatId);
  await bot.sendChatAction(chatId, 'typing');
  const pack = await handleInput(session, query.data || '', { onInterim: (m) => bot.sendMessage(chatId, m) });
  await sendStep(chatId, pack);
});

bot.on('message', async (msg) => {
  if (!msg.text || msg.text.startsWith('/')) return;
  const chatId = msg.chat.id;
  const session = getSession(chatId);
  await bot.sendChatAction(chatId, 'typing');
  const pack = await handleInput(session, msg.text.trim(), { onInterim: (m) => bot.sendMessage(chatId, m) });
  await sendStep(chatId, pack);
});

console.log('telegram bot: polling started');
