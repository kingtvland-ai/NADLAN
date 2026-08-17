# Integration Plan for YAD2 + PlanWatch

> **Historical.** This plan predates the current architecture: `frontend/`
> was retired to `archive/frontend/`, and `dashboard.html` (served directly
> by `dashboard.py`) is the UI now - it talks to Python only, not through
> `backend/server.js`. `backend/` still runs, but only as a server-to-server
> dependency of `yad2_feed.start_feed()`. See [DEPLOY.md](DEPLOY.md) for the
> architecture as it stands today. Kept below for reference.

## Purpose
This document describes how to integrate the existing `backend/` and `frontend/` folders with the remaining Python-based PlanWatch system in this repository.

## Architecture

### Current services
- `backend/`: Node/Express service offering `/api/listings` for Yad2 partner listings.
- `frontend/`: React/Vite application consuming `http://localhost:4000/api/listings`.
- root Python files: PlanWatch data, database, dashboard API, sync jobs, and listing feed integration.

### Recommended architecture
- Keep `backend/` unchanged as the Yad2 listings proxy.
- Run the Python PlanWatch service separately via `dashboard.py`.
- Use a shared SQLite database at `data/planwatch.sqlite3`.
- Optionally, proxy PlanWatch endpoints through `backend/src/server.js` so the frontend talks to one API host.

## Required files
- `requirements.txt`: Python dependencies for PlanWatch.
- `INTEGRATION.md`: this integration plan.

## Startup steps
1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the PlanWatch dashboard API:
   ```bash
   python dashboard.py
   ```
3. Optionally run the initial sync:
   ```bash
   python sync.py --full
   ```
4. Run Node backend:
   ```bash
   cd backend
   npm install
   npm run start
   ```
5. Run frontend:
   ```bash
   cd frontend
   npm install
   npm run dev
   ```

### Or start all services together
Use the provided `run-all.ps1` script to open the Python service, the Node backend, and the frontend in separate PowerShell windows:
```powershell
.\run-all.ps1
```

### Optional environment overrides
Copy `.env.example` to `.env` and update any overridable keys before starting the services.

### Proxy configuration
- The backend forwards all requests from `/api/planwatch/*` to the Python service.
- If the Python service uses a custom host or port, set `PYTHON_API_BASE` before starting the backend.

## Environment variables
- `PLANWATCH_DB`: optional path for the PlanWatch SQLite database.
- `PLANWATCH_LISTING_FEED_SECRET`: required by `listing_feed_api.py` if you run that service.
- `PLANWATCH_GOVMAP_KEY`: optional GovMap API key.
- `PLANWATCH_TABU_KEY`, `PLANWATCH_TABU_URL`: optional land registry key.
- `PLANWATCH_PRIVATE_API_TOKEN`, `PLANWATCH_PRIVATE_REVEAL_TOKEN`: optional private people API.

## Notes
- The frontend currently only calls `/api/listings` on the Node backend.
- `dashboard.py` already exposes a working PlanWatch JSON API on its port.
- CORS support was added to `dashboard.py` to allow cross-origin access from the React app.
