#!/usr/bin/env python3
"""Take screenshots of TelFiles UI in each supported language."""

import os
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = "http://localhost:8765"
PASSWORD = "admin"
DOCS_DIR = Path(__file__).parent
SCREENSHOTS_DIR = DOCS_DIR / "screenshots"

LANGS = [
    ("tr", "Türkçe"),
    ("en", "English"),
    ("de", "Deutsch"),
    ("ru", "Русский"),
    ("zh", "中文"),
]

TABS = [
    ("files",    "02-files"),
    ("hunter",   "03-hunter"),
    ("links",    "04-links"),
    ("settings", "05-settings"),
    ("status",   "06-status"),
]

def login(page):
    page.goto(BASE_URL, wait_until="networkidle")
    # greeter may already be shown or need a moment
    try:
        page.wait_for_selector("#ug-pass", timeout=8000)
        page.fill("#ug-pass", PASSWORD)
        page.click("#ui-greeter button")
        page.wait_for_selector("#ui-greeter", state="hidden", timeout=8000)
    except PWTimeout:
        pass  # already logged in (session cookie)


def set_language(page, lang_code):
    page.evaluate(f"localStorage.setItem('lang', '{lang_code}')")
    page.reload(wait_until="networkidle")
    # wait for a known translated element to appear
    page.wait_for_selector(".tab-btn", timeout=8000)


def switch_tab(page, tab):
    page.evaluate(f"switchTab('{tab}')")
    page.wait_for_timeout(900)  # let data load / animations settle


def screenshot(page, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=False)
    print(f"  saved {path.relative_to(DOCS_DIR.parent)}")


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
        )
        ctx = browser.new_context(viewport={"width": 1440, "height": 860})
        page = ctx.new_page()

        # --- initial login (establishes session cookie) ---
        login(page)

        for lang_code, lang_name in LANGS:
            print(f"\n[{lang_code}] {lang_name}")
            lang_dir = SCREENSHOTS_DIR / lang_code
            lang_dir.mkdir(parents=True, exist_ok=True)

            set_language(page, lang_code)

            for tab_id, filename in TABS:
                switch_tab(page, tab_id)
                screenshot(page, lang_dir / f"{filename}.png")

        browser.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
