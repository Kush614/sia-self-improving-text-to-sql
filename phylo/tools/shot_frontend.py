#!/usr/bin/env python
"""Screenshot the CopilotKit/Three.js frontend and capture console errors."""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3001/"
OUT = Path(r"E:\sia\phylo\docs"); OUT.mkdir(parents=True, exist_ok=True)
errs = []

with sync_playwright() as p:
    b = p.chromium.launch(args=["--use-gl=swiftshader", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"])
    pg = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    pg.goto(URL, wait_until="domcontentloaded", timeout=60000)  # SSE keeps net active; don't wait for idle
    time.sleep(14)  # let lineage load + grow animation + live events stream in
    pg.screenshot(path=str(OUT / "phylo-frontend.png"))
    # open the CopilotKit sidebar (its launcher button)
    try:
        pg.get_by_role("button").last.click()
        time.sleep(2)
        pg.screenshot(path=str(OUT / "phylo-copilot.png"))
    except Exception as e:
        print("sidebar click failed:", e)
    b.close()

print("=== console errors/warnings ===")
for e in errs[:40]:
    print("  ", e[:200])
print(f"({len(errs)} total)  screenshots -> {OUT}")
