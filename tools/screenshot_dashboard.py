#!/usr/bin/env python
"""Capture dashboard screenshots for the README via Playwright/Chromium.

Requires the static server running:  python -m http.server 8700 --directory dashboard
Usage:  python tools/screenshot_dashboard.py
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(r"E:\sia\docs\screenshots")
OUT.mkdir(parents=True, exist_ok=True)
URL = "http://127.0.0.1:8700/"


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)

        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_selector("#chart svg")
        time.sleep(2.6)  # let the chart draw-in + count-up settle

        pg.screenshot(path=str(OUT / "00-hero.png"))                       # viewport crop (README header)
        pg.screenshot(path=str(OUT / "01-overview-light.png"), full_page=True)

        # generation explorer (improvement.md panel)
        pg.locator(".gen-grid").screenshot(path=str(OUT / "05-improvement.png"))

        # run the before/after playground, then capture it
        pg.click("#runBtn")
        time.sleep(2.8)
        pg.locator("#baSection").screenshot(path=str(OUT / "03-run-query.png"))

        # dark theme full page
        pg.click("#themeBtn")
        time.sleep(1.0)
        pg.screenshot(path=str(OUT / "02-overview-dark.png"), full_page=True)

        # deep-dive page (reset to light)
        pg.evaluate("localStorage.setItem('sia-theme','light')")
        pg.goto(URL + "explain.html", wait_until="networkidle")
        time.sleep(1.2)
        pg.screenshot(path=str(OUT / "04-explain-light.png"), full_page=True)

        b.close()
    for f in sorted(OUT.glob("*.png")):
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)")
    print("screenshots written to", OUT)


if __name__ == "__main__":
    main()
