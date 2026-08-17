# Run both services together in separate PowerShell windows.
# Usage: .\run-all.ps1
#
# Node is only needed for a fresh harvest of the real Yad2 feed (private
# listings) via backend/src/scraper.js's persistent browser profile -
# dashboard.html talks to Python alone. Start Python by itself for everything
# else. The production frontend is public-site/ (served by Netlify or locally
# via the Node backend). See DEPLOYMENT_ARCHITECTURE.md for the full topology.

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendPath = Join-Path $repo 'backend'

Write-Host 'Starting PlanWatch Python service...' -ForegroundColor Cyan
Start-Process pwsh -ArgumentList "-NoExit", "-Command`, `"Set-Location '$repo'; python dashboard.py`""

Start-Sleep -Seconds 1
Write-Host 'Starting Node backend (Yad2 scraper)...' -ForegroundColor Cyan
Start-Process pwsh -ArgumentList "-NoExit", "-Command`, `"Set-Location '$backendPath'; npm install; npm run start`""
