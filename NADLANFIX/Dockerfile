# PlanWatch — one container, two processes.
#
# Why one image instead of two Render services
# ----------------------------------------------
# Node exists for exactly one reason: `backend/src/scraper.js` drives a real,
# persistent Chromium profile through Yad2's Radware bot check to reach
# gw.yad2.co.il/realestate-feed - the feed that carries private-seller
# listings (87,672 rows, against 14,445 from the promoted-ads carousel a
# plain HTTP client can already see). That mechanism was not ported to
# Python: a persistent authenticated browser profile is exactly the kind of
# thing that is cheap to keep working and expensive to get wrong a second
# time, and the risk of a rewrite buys nothing this container needs.
#
# Render disks attach to one service only, so two services could not share
# `data/planwatch.sqlite3` between a Python service and a Node service the
# way they already share it as two local processes on one machine today.
# One container keeps that same topology - Python on 8000, Node on 4000,
# `PYTHON_API_BASE` wiring them together - just started together instead of
# by hand. See docker-entrypoint.sh for the process supervision.
#
# `npx playwright install --with-deps` needs root and apt (it installs the
# OS libraries Chromium needs, not just the browser binary), which is why
# Node is installed here rather than copied in from a separate stage - a
# copied `node` binary would not carry that install step with it.
FROM python:3.14-slim

# shapely needs GEOS - without it the planning layer and parcel_at_point
# fail import silently and every listing loses its planning/parcel fields.
# curl+gnupg+ca-certificates are only here to fetch and trust NodeSource's
# setup script; nothing at runtime depends on them. xvfb is for the Yad2
# feed scraper specifically - see docker-entrypoint.sh for why a headed
# Chrome needs it in a container with no real display.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgeos-dev gcc curl ca-certificates gnupg xvfb \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Litestream continuously replicates PLANWATCH_DB to a GCS/Firebase Storage
# bucket - see litestream.yml and docker-entrypoint.sh. Fetches whatever the
# latest release actually is rather than a hardcoded version, same reasoning
# as the NodeSource setup script above: this line should not need touching
# every time upstream cuts a release.
#
# The release's plain x86_64 build is named "-linux-x86_64.tar.gz", not
# "-linux-amd64" - that string only appears on a separate, experimental
# "litestream-vfs-*" package, which a looser grep matched instead and which
# does not contain a binary named "litestream" at all (confirmed against a
# real build: `tar: litestream: Not found in archive`). Anchoring on
# "litestream-<version>-linux-x86_64.tar.gz" rules that out. `find` (not a
# fixed extraction path) covers either a flat or subdirectory archive layout
# without needing to know which one upstream ships.
RUN mkdir -p /tmp/litestream-dl \
    && curl -fsSL "$(curl -fsSL https://api.github.com/repos/benbjohnson/litestream/releases/latest \
        | grep -oE '"browser_download_url": *"[^"]*litestream-[0-9][^"]*-linux-x86_64\.tar\.gz"' \
        | head -1 | cut -d '"' -f4)" \
        -o /tmp/litestream.tar.gz \
    && tar -xzf /tmp/litestream.tar.gz -C /tmp/litestream-dl \
    && find /tmp/litestream-dl -type f -name litestream -exec mv {} /usr/local/bin/litestream \; \
    && chmod +x /usr/local/bin/litestream \
    && rm -rf /tmp/litestream-dl /tmp/litestream.tar.gz

WORKDIR /app

# Python dependencies first, so `pip install` is cached across rebuilds that
# only touch application code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Node dependencies, same caching reasoning. `npm ci` needs the lockfile
# present, not just package.json, or it fails rather than resolving loosely -
# which is the point: a reproducible build over a fast one.
COPY backend/package.json backend/package-lock.json backend/
# scraper.js launches with `channel: 'chrome'` deliberately, not the
# bundled Chromium - real Chrome is what gets past Radware's bot check (see
# the file's own comments). `playwright install chromium` alone leaves
# /opt/google/chrome/chrome missing, which surfaced only once a real deploy
# tried to run a feed harvest: every page failed with "Chromium
# distribution 'chrome' is not found", never during local dev where a
# system Chrome install already existed on PATH.
RUN cd backend && npm ci --omit=dev \
    && npx playwright install --with-deps chromium chrome

# The Telegram/WhatsApp bots (bots/telegram, bots/whatsapp) are separate
# npm packages, each its own process - see docker-entrypoint.sh. Neither
# needs Playwright; installing them here rather than folding into
# backend/package.json keeps a crashed or misconfigured bot's dependencies
# from ever being able to break the Yad2 scraper's.
COPY bots/telegram/package.json bots/telegram/package-lock.json bots/telegram/
RUN cd bots/telegram && npm ci --omit=dev
COPY bots/whatsapp/package.json bots/whatsapp/package-lock.json bots/whatsapp/
RUN cd bots/whatsapp && npm ci --omit=dev

COPY . .

# Windows checkouts routinely lose the executable bit git tracks for this
# file, and a non-executable ENTRYPOINT fails the container at boot with
# "permission denied" rather than at build time - set it unconditionally
# instead of trusting whatever bit came out of the build context.
RUN chmod +x /app/docker-entrypoint.sh

# The persistent disk mounts here - see render.yaml. Both processes read
# this same path: Python via PLANWATCH_DB, Node indirectly through Python's
# API for anything it needs from the store.
ENV PLANWATCH_DB=/data/planwatch.sqlite3
ENV PLANWATCH_DEPLOY=render
ENV PYTHON_API_BASE=http://127.0.0.1:8000

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
