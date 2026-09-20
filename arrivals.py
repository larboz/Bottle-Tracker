"""Tracks when Gold Eagle's New Arrivals page changes, and logs the times.
Run on a schedule. Learns when the store loads new inventory."""
import datetime
import json
import re
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from checker import TILE_JS, UA

URL = ("https://goldeaglewine.com/shop/"
       "?category=spirits_new_arrivals&title=Spirits%20New%20Arrivals")
MARKER = "/product"
STATE = Path("arrivals_state.json")
LOG = Path("arrivals_log.txt")


def name_from_href(href):
    parts = urllib.parse.urlparse(href).path.rstrip("/").split("/")
    slug = parts[-2] if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{24}", parts[-1]) else parts[-1]
    return slug.replace("-", " ").title()


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=UA["User-Agent"])
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)
        tiles = page.eval_on_selector_all("a", TILE_JS, MARKER)
        browser.close()

    if not tiles:
        raise SystemExit("No product tiles found; page not loaded. Keeping old state.")

    current = {}
    for t in tiles:
        href = t["href"].split("?")[0]
        current.setdefault(href, name_from_href(href))

    now = datetime.datetime.now(ZoneInfo("America/Chicago"))
    stamp = now.strftime("%Y-%m-%d %H:%M %Z")

    if not STATE.exists():
        LOG.write_text(f"{stamp} | baseline: {len(current)} products\n")
        print(f"Baseline saved: {len(current)} products")
    else:
        prev = json.loads(STATE.read_text())
        new = [current[h] for h in current if h not in prev]
        gone = [prev[h] for h in prev if h not in current]
        if new or gone:
            line = f"{stamp} | +{len(new)} new"
            if new:
                line += ": " + "; ".join(new)
            line += f" | -{len(gone)} gone"
            with LOG.open("a") as f:
                f.write(line + "\n")
            print(line)
        else:
            print(f"{stamp} | no change ({len(current)} products)")

    STATE.write_text(json.dumps(current, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
