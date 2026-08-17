// Per-conversation state for the button-driven search flow (mode -> city ->
// source -> rooms -> results, or mode -> gush/helka -> dossier), shared by
// both bots. Plain in-memory Map: both bots are single-process, and a lost
// session on restart just means the next message starts a fresh search - no
// different from today's behaviour where there was no session at all.
import { RESULTS_PER_PAGE } from './api.js';

const SESSIONS = new Map();
const SESSION_TTL_MS = 30 * 60 * 1000; // idle chat forgets its filters after 30 min

export function getSession(id) {
  const existing = SESSIONS.get(id);
  if (existing && Date.now() - existing.touchedAt < SESSION_TTL_MS) {
    existing.touchedAt = Date.now();
    return existing;
  }
  const fresh = {
    step: 'mode', mode: '', locality: '', source: '', minRooms: '', maxRooms: '',
    gush: '', helka: '', pageSize: RESULTS_PER_PAGE, offset: 0, touchedAt: Date.now(),
  };
  SESSIONS.set(id, fresh);
  return fresh;
}

export function resetSession(id) {
  SESSIONS.delete(id);
  return getSession(id);
}

// Periodic sweep so a long-running process does not accumulate one entry per
// chat forever.
setInterval(() => {
  const now = Date.now();
  for (const [id, session] of SESSIONS) {
    if (now - session.touchedAt >= SESSION_TTL_MS) SESSIONS.delete(id);
  }
}, 10 * 60 * 1000).unref();
