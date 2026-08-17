# AGENTS.md

## Project rules

- Target Python 3.12 or newer.
- Use semantic versioning for releases and keep package/runtime version declarations aligned.
- Prefer async APIs for browser, network, database, and filesystem work.
- Never put blocking network/disk/log-stream operations directly on the asyncio event loop.
- Preserve thread safety when moving unavoidable blocking work to worker threads or queue listeners.
- Do not implement CAPTCHA bypasses, credential theft, access-control bypasses, or anti-abuse evasion.
- Keep extraction logic behind adapters because Marketplace markup changes frequently.
- Keep serialized card fixtures compatible with the production extraction record contract; sanitize live captures before committing.
- Add tests for parsers, normalization, persistence, migrations, scoring, API behavior, retention, and classification before expanding scraping coverage.
- Every persistent SQLite schema change must be represented by an idempotent versioned migration and tested against an older database fixture.
- Keep daemon defaults resource-conscious; reuse expensive resources, cap local-LLM concurrency, bound comparable candidate sets, and avoid unbounded concurrency.
- Local-LLM classification must remain optional and fall back cleanly when the local server is unavailable.
- Safety-excluded listings must not be promoted by scoring or notification paths.
- Update README.md, CHANGELOG.md, and TODO.md whenever user-visible behavior or roadmap state changes.
- Never commit browser storage-state files, credentials, cookies, databases, or collected private data.
