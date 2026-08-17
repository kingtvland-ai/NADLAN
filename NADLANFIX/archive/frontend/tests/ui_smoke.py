from pathlib import Path
import os
from playwright.sync_api import sync_playwright


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.goto(os.getenv("UI_BASE", "http://127.0.0.1:5173"), wait_until="networkidle")

    assert page.locator("h1").inner_text() == "כל המידע. בלי הרעש."
    assert page.locator(".category-card").count() == 4
    assert page.locator(".advanced-filters").count() == 0
    page.get_by_role("button", name="מסננים נוספים").click()
    assert page.locator(".advanced-filters").is_visible()

    page.get_by_role("button", name="מקורות מידע").click()
    page.get_by_role("button", name="בדוק חיבורים").click()
    page.wait_for_selector(".source-card")
    assert page.locator(".source-card").count() >= 8
    assert page.get_by_text("ONMAP", exact=True).is_visible()
    assert "׳" not in page.locator("body").inner_text()
    assert not errors, errors

    artifact = Path("frontend/test-artifacts/home.png")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(artifact), full_page=True)
    browser.close()
