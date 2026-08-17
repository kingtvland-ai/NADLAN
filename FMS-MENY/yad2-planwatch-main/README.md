# YAD2 + PlanWatch Integration

This repository includes:
- `backend/`: Node/Express service driving a persistent Playwright profile
  to reach Yad2's private-listing feed. Called server-to-server by
  `dashboard.py` (`yad2_feed.start_feed`), not by a browser - the UI does not
  talk to it directly.
- Python PlanWatch files in the repository root for data sync, dashboard API,
  and analytics. `dashboard.html` is the UI - served by `dashboard.py`
  itself, no build step.
- `archive/frontend/`: an earlier React/Vite search UI, retired once
  `dashboard.html` covered everything it did and more. Kept for reference,
  not run.

See [DEPLOY.md](DEPLOY.md) for how to run and deploy this.

## Setup

### 1. Python dependencies
```bash
pip install -r requirements.txt
```

### 2. Run PlanWatch dashboard service
```bash
python dashboard.py
```

### 3. Optionally run initial PlanWatch sync
```bash
python sync.py --full
```

### 4. Run Node backend
```bash
cd backend
npm install
npm run start
```

### 5. Run frontend
```bash
cd frontend
npm install
npm run dev
```

### 6. Start everything together
```powershell
.
un-all.ps1
```

### 7. Optional environment overrides
Copy `.env.example` to `.env` and update any keys before starting services.

## What was integrated

- `backend/src/server.js` now proxies `/api/planwatch/*` requests to the Python PlanWatch dashboard API on `http://127.0.0.1:8000`.
- `frontend/src/SearchResults.jsx` now supports a `PlanWatch summary` mode in addition to the existing Yad2 listings search.
- `dashboard.py` now returns CORS headers on all JSON responses so the React app can access the PlanWatch API.
- `frontend/src/App.css` now includes dedicated PlanWatch summary styling.
- Root docs and env examples were added to make the combined stack easier to run.

## Yad2 stock and the leads screen

The Yad2 for-sale stock is harvested into PlanWatch's own SQLite store by
`yad2_feed.py` and read from there. Rendering the listings table never touches
the network — refreshing is the "רענן יד 2" button, or the daily `yad2`
scheduler job.

```bash
python yad2_feed.py --status                    # last run's terminal state
python yad2_feed.py --target 18000              # one full pass (~25-40 min)
python yad2_feed.py --target 500 --max-pages 25 # short pass, for a smoke test
```

This needs the local curl2api connector running; it defaults to
`http://127.0.0.1:8100/api/yad2_forsale` and is overridable with
`PLANWATCH_YAD2_API` / `PLANWATCH_YAD2_PATH`.

**Run it daily.** Days-on-market and price movement are not readable off the
board — they exist only because we recorded what the board said yesterday. Each
pass appends to `yad2_price_history`, which is what the לידים tab
(`lead_intel.py`, `/api/leads`) scores sellers on. Skip the daily run and the
lead ranking has nothing but today's asking price to work with.

Advertiser phone numbers (`yad2_contact.py`, `/api/listing-contact`) are looked
up **per listing, on explicit request** — there is no bulk sweep. As of
2026-08-05 the ad page itself is behind Radware, so the route reports why it
could not answer; setting `GOOGLE_MAPS_API_KEY` enables the fallback that finds
the *agency's* published business number.

## Environment variables
- `PLANWATCH_DB` - optional SQLite path for PlanWatch data.
- `PLANWATCH_YAD2_API` - base URL of the local Yad2 connector (default `http://127.0.0.1:8100`).
- `PLANWATCH_YAD2_PATH` - connector path for the for-sale board (default `/api/yad2_forsale`).
- `GOOGLE_MAPS_API_KEY` - enables agency phone lookup via the Places API.
- `PLANWATCH_LISTING_FEED_SECRET` - required to run `listing_feed_api.py`.
- `PLANWATCH_GOVMAP_KEY` - optional GovMap API key.
- `PLANWATCH_TABU_KEY` - optional land registry API key.
- `PLANWATCH_TABU_URL` - optional land registry API endpoint.
- `PLANWATCH_PRIVATE_API_TOKEN` - optional private people API token.
- `PLANWATCH_PRIVATE_REVEAL_TOKEN` - optional private people reveal token.
- `PYTHON_API_BASE` - optional override for the backend proxy base URL if the Python service runs on a different host or port.

## PlanWatch proxy path
- The Node backend exposes Python-hosted PlanWatch APIs under `/api/planwatch/*`.
- Example: `http://localhost:4000/api/planwatch/bootstrap` forwards to `http://127.0.0.1:8000/api/bootstrap`.

## Notes

The frontend still uses the existing Yad2 listing endpoint at `/api/listings`.
PlanWatch data is accessed through `/api/planwatch/bootstrap` via the Node backend proxy.

If you want to call additional PlanWatch endpoints, use `/api/planwatch/<endpoint>` from the frontend.
