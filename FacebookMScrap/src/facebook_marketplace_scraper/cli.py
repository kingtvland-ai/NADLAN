# src/facebook_marketplace_scraper/cli.py
from __future__ import annotations

import asyncio
from pathlib import Path

import typer
import uvicorn

from .browser import MarketplaceBrowser
from .city_resolver import resolve_city_id
from .daemon import run_daemon
from .dashboard import FIXED_QUERY_BASE, create_dashboard_app
from .fixtures import capture_search_fixture
from .llm_classifier import LlamaClassifierSettings, LocalLlamaClassifier
from .logging_config import configure_logging
from .models import SearchSpec, Watchlist
from .notifications import NotificationManager, NotificationSettings
from .retention import retention_policy_from_env
from .service import MarketplaceCollector
from .storage import MarketplaceStore

app = typer.Typer(no_args_is_help=True, help="Facebook Marketplace research collector.")
watch_app = typer.Typer(no_args_is_help=True, help="Manage recurring Marketplace watchlists.")
session_app = typer.Typer(no_args_is_help=True, help="Manage the local browser session state.")
fixture_app = typer.Typer(no_args_is_help=True, help="Capture sanitized extraction fixtures.")
maintenance_app = typer.Typer(no_args_is_help=True, help="Database maintenance and retention.")
llm_app = typer.Typer(no_args_is_help=True, help="Inspect the optional local llama.cpp classifier.")
app.add_typer(watch_app, name="watch")
app.add_typer(session_app, name="session")
app.add_typer(fixture_app, name="fixture")
app.add_typer(maintenance_app, name="maintenance")
app.add_typer(llm_app, name="llm")

DEFAULT_DB = Path("data/marketplace.sqlite3")
DEFAULT_SESSION = Path("data/facebook_storage_state.json")


@app.callback()
def configure(
    log_level: str = typer.Option("INFO", help="Logging level."),
    log_format: str = typer.Option("text", help="text or json."),
) -> None:
    """Configure non-blocking queue-backed application logging."""
    configure_logging(level=log_level, fmt=log_format)


@app.command("init-db")
def init_db(db: Path = typer.Option(DEFAULT_DB, help="SQLite database path.")) -> None:
    """Create or upgrade the local SQLite schema."""
    asyncio.run(MarketplaceStore(db).initialize())
    typer.echo(f"Initialized {db}")


@session_app.command("login")
def session_login(
    session: Path = typer.Option(DEFAULT_SESSION, help="Playwright storage-state output path."),
) -> None:
    """Open a headed browser for manual sign-in and save its session state."""

    async def _run() -> None:
        async with MarketplaceBrowser(headless=False, storage_state_path=session) as browser:
            await browser.open_marketplace()
            typer.echo("Sign in manually in the browser window. No password is read by this tool.")
            await asyncio.to_thread(input, "Press Enter here after Marketplace is ready: ")
            target = await browser.save_storage_state(session)
            typer.echo(f"Saved browser session to {target}")

    asyncio.run(_run())


@fixture_app.command("capture")
def fixture_capture(
    query: str = typer.Argument(..., help="Marketplace query used to capture result-card structure."),
    city_id: str = typer.Option(..., help="Facebook Marketplace numeric city id (found manually per city)."),
    output: Path = typer.Option(Path("tests/fixtures/marketplace_search_cards.json")),
    max_items: int = typer.Option(30, min=1, max=500),
    session: Path = typer.Option(DEFAULT_SESSION),
) -> None:
    """Capture a real browser result snapshot and sanitize unstable IDs/image URLs."""
    target = asyncio.run(
        capture_search_fixture(
            query=query,
            city_id=city_id,
            output=output,
            storage_state_path=session,
            max_items=max_items,
        )
    )
    typer.echo(f"Saved sanitized Marketplace fixture to {target}")


@app.command()
def search(
<<<<<<< HEAD
    city: str = typer.Option(..., "--city", help="Israeli city name (e.g. נתניה). See israel_cities.py for the full list."),
    radius_km: float = typer.Option(5, min=0, help="Search radius in kilometers around the city."),
    category_id: str = typer.Option(
        None,
        help=(
            "Facebook Marketplace category_id query param, e.g. 1270772586445798 for "
            "'Property for sale'. Find it by clicking the category on facebook.com and "
            "copying the category_id from the URL."
        ),
    ),
=======
    query: str = typer.Argument(..., help="Marketplace search query."),
    city_id: str = typer.Option(..., help="Facebook Marketplace numeric city id (found manually per city)."),
    radius_km: float = typer.Option(5, min=0, help="Search radius in kilometers around the city."),
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
    max_items: int = typer.Option(20, min=1, max=500, help="Maximum listings to collect."),
    min_price: float | None = typer.Option(None, min=0, help="Local minimum price filter."),
    max_price: float | None = typer.Option(None, min=0, help="Local maximum price filter."),
    currency: str = typer.Option("ILS", help="Currency used for ambiguous '$' prices."),
    db: Path = typer.Option(DEFAULT_DB, help="SQLite database path."),
    session: Path = typer.Option(DEFAULT_SESSION, help="Playwright storage-state path."),
    headed: bool = typer.Option(False, help="Show the browser window."),
) -> None:
    """Collect, enrich, persist, deduplicate, score, and notify on one search."""

    async def _run() -> None:
        store = MarketplaceStore(db)
        await store.initialize()
        city_id = await resolve_city_id(store, city)
        if not city_id:
            typer.echo(f"'{city}' is not a supported city. See israel_cities.py.")
            raise typer.Exit(code=1)
        llm_settings = LlamaClassifierSettings.from_env()
        classifier = LocalLlamaClassifier(llm_settings) if llm_settings.enabled else None
        notifier = NotificationManager(store, NotificationSettings.from_env())
        try:
            collector = MarketplaceCollector(
                store=store,
                storage_state_path=session,
                headless=not headed,
                classifier=classifier,
                notifier=notifier,
            )
            result = await collector.collect(
                SearchSpec(
<<<<<<< HEAD
                    query=f"{FIXED_QUERY_BASE} {city}",
                    city_id=city_id,
                    radius_km=radius_km,
                    category_id=category_id,
=======
                    query=query,
                    city_id=city_id,
                    radius_km=radius_km,
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
                    max_items=max_items,
                    min_price=min_price,
                    max_price=max_price,
                    default_currency=currency,
                )
            )
            typer.echo(
                f"run={result.run_id} extracted={result.extracted} normalized={result.normalized} "
                f"inserted={result.inserted} updated={result.updated} "
                f"price_changes={result.price_changes} notifications={result.notifications}"
            )
            for item in result.listings[:20]:
                listing = item.listing
                typer.echo(
                    f"{item.deal_score:5.1f}  {listing.price_text or '—':>12}  "
                    f"[{listing.category}/{listing.condition}] {listing.title[:60]}  {listing.url}"
                )
        finally:
            await notifier.close()
            if classifier is not None:
                await classifier.close()

    asyncio.run(_run())


@watch_app.command("add")
def watch_add(
    name: str = typer.Option(..., help="Unique watchlist name."),
<<<<<<< HEAD
    city: str = typer.Option(..., "--city", help="Israeli city name (e.g. נתניה). See israel_cities.py for the full list."),
    radius_km: float = typer.Option(5, min=0, help="Search radius in kilometers around the city."),
    category_id: str = typer.Option(
        None,
        help="Facebook Marketplace category_id query param, e.g. 1270772586445798 for 'Property for sale'.",
    ),
=======
    query: str = typer.Option(..., help="Marketplace query."),
    city_id: str = typer.Option(..., help="Facebook Marketplace numeric city id (found manually per city)."),
    radius_km: float = typer.Option(5, min=0, help="Search radius in kilometers around the city."),
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
    min_price: float | None = typer.Option(None, min=0),
    max_price: float | None = typer.Option(None, min=0),
    target_price: float | None = typer.Option(None, min=0),
    max_items: int = typer.Option(50, min=1, max=500),
    interval_minutes: int = typer.Option(30, min=1),
    currency: str = typer.Option("ILS"),
    db: Path = typer.Option(DEFAULT_DB),
) -> None:
    """Create a recurring watchlist."""

    async def _run() -> None:
        store = MarketplaceStore(db)
        await store.initialize()
        city_id = await resolve_city_id(store, city)
        if not city_id:
            typer.echo(f"'{city}' is not a supported city. See israel_cities.py.")
            raise typer.Exit(code=1)
        watchlist_id = await store.create_watchlist(
            Watchlist(
                name=name,
<<<<<<< HEAD
                query=f"{FIXED_QUERY_BASE} {city}",
                city_id=city_id,
                radius_km=radius_km,
                category_id=category_id,
=======
                query=query,
                city_id=city_id,
                radius_km=radius_km,
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
                min_price=min_price,
                max_price=max_price,
                target_price=target_price,
                max_items=max_items,
                default_currency=currency,
                interval_seconds=interval_minutes * 60,
            )
        )
        typer.echo(f"Created watchlist {watchlist_id}: {name}")

    asyncio.run(_run())


@watch_app.command("list")
def watch_list(db: Path = typer.Option(DEFAULT_DB)) -> None:
    """List configured watchlists."""

    async def _run() -> None:
        store = MarketplaceStore(db)
        await store.initialize()
        for item in await store.list_watchlists():
            state = "on" if item.enabled else "off"
            typer.echo(
                f"{item.id:>3} [{state}] {item.name}: {item.query!r} city_id={item.city_id} "
<<<<<<< HEAD
                f"category_id={item.category_id} radius={item.radius_km}km "
                f"every {item.interval_seconds // 60}m target={item.target_price}"
=======
                f"radius={item.radius_km}km every {item.interval_seconds // 60}m target={item.target_price}"
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
            )

    asyncio.run(_run())


@watch_app.command("remove")
def watch_remove(
    watchlist_id: int = typer.Argument(...),
    db: Path = typer.Option(DEFAULT_DB),
) -> None:
    """Delete a watchlist and its match records."""

    async def _run() -> None:
        store = MarketplaceStore(db)
        await store.initialize()
        removed = await store.delete_watchlist(watchlist_id)
        if not removed:
            raise typer.Exit(code=1)
        typer.echo(f"Removed watchlist {watchlist_id}")

    asyncio.run(_run())


@maintenance_app.command("prune")
def maintenance_prune(db: Path = typer.Option(DEFAULT_DB)) -> None:
    """Apply the configured retention policy immediately."""

    async def _run() -> None:
        store = MarketplaceStore(db)
        await store.initialize()
        result = await store.prune(retention_policy_from_env())
        typer.echo(f"Retention: {result}")

    asyncio.run(_run())


@llm_app.command("status")
def llm_status() -> None:
    """Check the configured local llama.cpp server."""

    async def _run() -> None:
        settings = LlamaClassifierSettings.from_env()
        if not settings.enabled:
            typer.echo("Local LLM classification is disabled (set FBMS_LLM_ENABLED=1 to enable).")
            return
        classifier = LocalLlamaClassifier(settings)
        try:
            healthy = await classifier.health()
        finally:
            await classifier.close()
        typer.echo(f"llama.cpp health={'ok' if healthy else 'unavailable'} url={settings.base_url}")

    asyncio.run(_run())


@app.command("daemon")
def daemon_command(
    db: Path = typer.Option(DEFAULT_DB),
    session: Path = typer.Option(DEFAULT_SESSION),
    poll_seconds: int = typer.Option(30, min=5),
    headed: bool = typer.Option(False),
    once: bool = typer.Option(False, help="Run all enabled watchlists once and exit."),
) -> None:
    """Run due watchlists continuously using one reusable browser session."""
    asyncio.run(
        run_daemon(
            db_path=db,
            storage_state_path=session,
            poll_seconds=poll_seconds,
            headless=not headed,
            once=once,
        )
    )


@app.command("dashboard")
def dashboard_command(
    db: Path = typer.Option(DEFAULT_DB),
    session: Path = typer.Option(DEFAULT_SESSION),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8787, min=1, max=65535),
    open_browser: bool = typer.Option(True, help="Open the dashboard in a browser tab on startup."),
) -> None:
    """Serve the dashboard and JSON API."""
    # 0.0.0.0 / :: are bind addresses, not browsable URLs.
    display_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    dashboard_url = f"http://{display_host}:{port}/"
    uvicorn.run(
        create_dashboard_app(db, session, open_browser=open_browser, dashboard_url=dashboard_url),
        host=host,
        port=port,
        log_level="info",
    )

if __name__ == "__main__":
    app()
