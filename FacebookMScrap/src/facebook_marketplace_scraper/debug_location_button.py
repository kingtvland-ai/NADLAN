"""חד-פעמי: מדפיס את כל הכפתורים בדף המרקטפלייס עם השם הנגיש שלהם,
כדי למצוא איך צריך לתקן את ה-regex ב-search_location_candidates.

הרצה: python debug_location_button.py <path-to-storage-state.json>
"""
import asyncio
import sys

from playwright.async_api import async_playwright


async def main(storage_state_path: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="he-IL",
            viewport={"width": 1440, "height": 1000},
            storage_state=storage_state_path,
        )
        page = await context.new_page()
        await page.goto("https://www.facebook.com/marketplace/", wait_until="networkidle", timeout=30_000)

        buttons = page.get_by_role("button")
        count = await buttons.count()
        print(f"נמצאו {count} כפתורים בעמוד:\n")
        for i in range(count):
            btn = buttons.nth(i)
            try:
                name = await btn.get_attribute("aria-label")
                text = (await btn.inner_text(timeout=1000)).strip()
            except Exception:
                text = ""
            if name or text:
                print(f"[{i}] aria-label={name!r} text={text!r}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
