# Command reference

Run `python skills/yad2-data-bridge/scripts/yad2_bridge.py [options]` from the repository root.

Required: `--region-slug`, for example `tel-aviv-area`.

Filters: `--deal-type` (defaults to `forsale`), `--area`, `--city`, `--page`, and `--all`.

Destinations:

- No destination: print normalized JSON to stdout.
- `--output PATH`: write `.json` or `.csv`, inferred from extension. Use `--format` to override.
- `--webhook URL`: POST `{source, fetchedAt, filters, totalCount, listings}` as JSON.
- `--dry-run`: fetch and describe the webhook payload without sending it.

Authentication:

- `--header-env NAME`: send the environment variable as a Bearer token.
- If omitted, `YAD2_WEBHOOK_TOKEN` is used when present.
- `--header 'X-Api-Key: value'` is supported, but environment variables are preferred.

Runtime: `--api-base URL`, `--project-root PATH`, `--no-auto-start`, and `--timeout SECONDS`.

```powershell
python skills/yad2-data-bridge/scripts/yad2_bridge.py --region-slug tel-aviv-area --all --output yad2.csv
$env:YAD2_WEBHOOK_TOKEN = '<token>'
python skills/yad2-data-bridge/scripts/yad2_bridge.py --region-slug tel-aviv-area --city 5000 --webhook https://crm.example/api/yad2
```
