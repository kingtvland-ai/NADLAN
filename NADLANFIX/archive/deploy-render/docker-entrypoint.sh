#!/bin/sh
# Starts both processes this image needs, in the order that matters.
#
# Node goes first and in the background: `yad2_feed.start_feed()` (Python)
# calls it at 127.0.0.1:4000 (PLANWATCH_YAD2_FEED) to drive the persistent
# Chromium profile that reaches the private-listing feed - see the Dockerfile
# for why that logic was not ported to Python. Python is `exec`'d last so it
# replaces this shell as PID 1: Render sends SIGTERM to PID 1 on a deploy or
# a restart, and only a process actually listening for it shuts down
# cleanly instead of being SIGKILLed after a timeout with a write in flight.
#
# A crashed Node does not take Python down with it. Everything driven by the
# stored data - the whole sale-listings screen, parcel dossiers, every
# scored endpoint - works without it; only a *new* private-listing harvest
# would fail, and it already fails with a clear message ("Needs the Node
# service up") rather than a silent gap. That degradation is intentional:
# the alternative is one Node crash taking down the entire dashboard.
set -eu

# The Yad2 feed scraper (backend/src/feed.js) deliberately launches Chrome
# headed, not headless - scraper.js's own comment says a headless context
# always gets Radware's bot-check page and never the real feed. A headed
# browser still needs *some* display to render into, and a bare container
# has none: confirmed live ("Missing X server or $DISPLAY", Chrome exiting
# immediately) once a real feed harvest was attempted against this exact
# image. Xvfb is a virtual framebuffer - a display that exists only in
# memory, satisfying Chrome without an actual screen. DISPLAY must be
# exported before Node starts, not just for this shell, so the child
# process inherits it.
Xvfb :99 -screen 0 1366x768x24 -nolisten tcp &
export DISPLAY=:99

# Render injects PORT for whichever process it expects to be the public web
# service - it does not know that is meant to be Python here, not Node. Node
# reads process.env.PORT itself (backend/src/server.js's own fallback chain
# is `process.env.PORT || process.argv[2] || 4000`), so left alone it grabs
# Render's PORT, opens first (Python has migrations to run before it can
# bind), and Render locks onto Node as "the service" - every request then
# hits the internal scraper's Express app instead of dashboard.py, which
# looks like a working deploy (both expose a matching-shaped /api/health)
# right up until any real endpoint 404s. Pinning Node to its own fixed
# internal port, independent of whatever Render assigned, is what keeps it
# off the port Python needs.
PORT=4000 node /app/backend/src/server.js &
NODE_PID=$!

# Node needs a moment to bind :4000 before anything asks it for a harvest;
# this is a courtesy, not a dependency - dashboard.py's own retry/timeout
# handling in yad2_feed.py is what actually protects a request made before
# this returns.
sleep 1

if ! kill -0 "$NODE_PID" 2>/dev/null; then
    echo "docker-entrypoint: Node backend failed to start - continuing without it." >&2
    echo "docker-entrypoint: private-listing harvest (yad2_feed.start_feed) will be unavailable." >&2
fi

# The Telegram/WhatsApp bots (bots/telegram, bots/whatsapp) are optional and
# independent of everything above: each is backgrounded the same way, and
# each bails out on its own (exit 0) when its required env var is unset -
# PLANWATCH_TELEGRAM_TOKEN, or nothing for WhatsApp since it only needs a
# one-time QR scan via GET /api/admin/whatsapp-qr. A crashed or unconfigured
# bot must not take the dashboard down, same reasoning as Node above.
node /app/bots/telegram/bot.js &
node /app/bots/whatsapp/bot.js &

# Python is the public service and must bind whatever port Render actually
# assigned - defaulting to 8000 for docker-compose/local runs where PORT is
# unset. dashboard.py has no idea Render exists; this is the only place that
# translates Render's convention into its --port flag.
PYTHON_CMD="python dashboard.py --host 0.0.0.0 --port ${PORT:-8000} --no-browser"

# Litestream is entirely opt-in, gated on LITESTREAM_GCS_BUCKET: a plan with
# a real persistent disk (or a local run) never sets it and this block does
# nothing. When it is set, `restore` pulls the last replicated snapshot down
# before Python ever opens the database - `-if-replica-exists` makes that a
# no-op on the very first deploy, when the bucket is still empty - and
# `replicate -exec` then runs Python as its child, streaming every WAL
# commit to the bucket for as long as it's up. This is Litestream's own
# documented pattern for wrapping an application; it is not something
# invented here.
if [ -n "${LITESTREAM_GCS_BUCKET:-}" ]; then
    litestream restore -if-replica-exists -config /app/litestream.yml "${PLANWATCH_DB}"
    exec litestream replicate -config /app/litestream.yml -exec "$PYTHON_CMD"
fi

exec $PYTHON_CMD
