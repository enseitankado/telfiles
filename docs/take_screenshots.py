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
    ("files",    "02-files",    None),
    ("hunter",   "03-hunter",   None),
    ("links",    "04-links",    None),
    ("settings", "05-settings", None),
    ("status",   "06-status",   "#status-panel .st-cards"),
]


def login(page):
    page.goto(BASE_URL, wait_until="networkidle")
    try:
        page.wait_for_selector("#ug-pass", timeout=8000)
        page.fill("#ug-pass", PASSWORD)
        page.click("#ui-greeter button")
        page.wait_for_selector("#ui-greeter", state="hidden", timeout=8000)
    except PWTimeout:
        pass


def dismiss_notifications(page):
    """Close any toast/update notifications that may overlap content."""
    try:
        page.evaluate("""
            document.querySelectorAll(
                '.toast, .update-banner, [id*="update"], [class*="update-notice"]'
            ).forEach(el => el.remove());
        """)
    except Exception:
        pass


def set_language(page, lang_code):
    page.evaluate(f"localStorage.setItem('lang', '{lang_code}')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector(".tab-btn", timeout=8000)


def switch_tab(page, tab, ready_selector=None):
    page.evaluate(f"switchTab('{tab}')")
    if ready_selector:
        try:
            page.wait_for_selector(ready_selector, timeout=12000)
        except PWTimeout:
            pass
        page.wait_for_timeout(600)
    else:
        page.wait_for_timeout(900)
    dismiss_notifications(page)


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

        login(page)

        for lang_code, lang_name in LANGS:
            print(f"\n[{lang_code}] {lang_name}")
            lang_dir = SCREENSHOTS_DIR / lang_code
            lang_dir.mkdir(parents=True, exist_ok=True)

            set_language(page, lang_code)

            for tab_id, filename, ready_sel in TABS:
                switch_tab(page, tab_id, ready_sel)
                screenshot(page, lang_dir / f"{filename}.png")

        browser.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
