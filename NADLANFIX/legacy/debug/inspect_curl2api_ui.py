from playwright.sync_api import sync_playwright
import sys

sys.stdout.reconfigure(encoding="utf-8")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto("http://127.0.0.1:8080", wait_until="networkidle", timeout=30000)
    print("title:", page.title())
    print("url:", page.url)
    print("buttons:", page.get_by_role("button").all_text_contents())
    print("links:", page.get_by_role("link").all_text_contents())
    print("inputs:", page.locator("input").count())
    page.screenshot(path="curl2api-ui.png", full_page=True)
    browser.close()
