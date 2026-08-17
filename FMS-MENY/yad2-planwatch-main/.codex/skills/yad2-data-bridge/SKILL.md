---
name: yad2-data-bridge
description: Fetch normalized Israeli real-estate listings from the local Yad2 integration and deliver them to JSON, CSV, stdout, or an external webhook/API. Use when the user asks to pull, export, sync, import, or send Yad2 listing data to another app, website, CRM, spreadsheet automation, or custom interface in one command.
---

# Yad2 Data Bridge

Use `scripts/yad2_bridge.py` as the single entry point. It calls the existing local Yad2 backend; do not scrape Yad2 independently or invent endpoints.

## Workflow

1. Collect or infer `region-slug`; default `deal-type` to `forsale`.
2. Read [references/usage.md](references/usage.md) when advanced options are needed.
3. Run the bridge from the repository root. It starts the backend automatically unless `--no-auto-start` is set.
4. Return the listing count and destination. Never echo authentication header values.
5. If Yad2 presents its browser challenge, run `npm --prefix backend run login` once, let the user complete it, then retry.

## Guardrails

- Fetch only public listing data through the repository's existing integration.
- Respect the source site's terms, rate limits, and access controls. Do not bypass CAPTCHA, authentication, or anti-bot controls.
- Require explicit authorization before POSTing to an external destination.
- Prefer `YAD2_WEBHOOK_TOKEN` or `--header-env` for secrets; never put secrets in files or logs.
- Use `--dry-run` before sending to a new webhook when practical.

## Quick commands

```powershell
python skills/yad2-data-bridge/scripts/yad2_bridge.py --region-slug tel-aviv-area --all --output listings.json
python skills/yad2-data-bridge/scripts/yad2_bridge.py --region-slug tel-aviv-area --all --webhook https://example.com/api/import --header-env YAD2_WEBHOOK_TOKEN
```
