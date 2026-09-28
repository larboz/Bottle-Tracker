"""Binny's stock at a few chosen stores.

Binny's search is off-limits to bots (robots.txt), but product pages are
allowed. Each bottle in config.yaml's binnys.pages is loaded once per store,
with Binny's own "current-store" cookie set the way its store picker does,
and the page's "IN STOCK @ <store>" line and price are read.

Writes binnys.json for the web page. Never sends email.
"""
import datetime
import json
import re
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

UA = "Mozilla/5.0 (compatible; BottleWatch/1.0)"
OUT_FILE = Path("binnys.json")
# e.g. "IN STOCK @ Joliet ISLAND STACKINGS > SECTION 16 Curbside or Store Pick Up"
STOCK = re.compile(
    r"(IN STOCK|OUT OF STOCK|ONLY \d+ LEFT|LOW STOCK|SOLD OUT|NOT AVAILABLE)\s*@\s*(.+?)\s+Curbside",
    re.I)
PRICE = re.compile(r"750 ML BOTTLE CURRENT (?:SALE )?PRICE IS (\$[\d,]+\.\d\d)"
                   r"|CURRENT (?:SALE )?PRICE IS (\$[\d,]+\.\d\d)", re.I)


def price_value(price):
    digits = re.sub(r"[^\d.]", "", price or "")
    return float(digits) if digits else None


def read_page(page, url, store):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    text = " ".join(page.inner_text("body").split())
    m = STOCK.search(text)
    if not m:
        raise RuntimeError(f"no stock line on page (title: {page.title()!r}, "
                           f"text: {text[:160]!r})")
    status, where = m.group(1).upper(), m.group(2)
    # where = "<store name> <shelf>"; drop the store name
    shelf = where[len(store):].strip() if where.lower().startswith(store.lower()) else where
    in_stock = status.startswith(("IN STOCK", "ONLY", "LOW"))
    return {
        "status": status.capitalize(),
        "in_stock": in_stock,
        "shelf": shelf.title() if in_stock else "",
        "price": product_price(page) or text_price(text),
    }


def product_price(page):
    """Price from the page's structured product data (most reliable)."""
    for raw in page.eval_on_selector_all('script[type="application/ld+json"]',
                                         "els => els.map(e => e.textContent)"):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            offer = d.get("offers") if d.get("@type") == "Product" else None
            offer = offer[0] if isinstance(offer, list) else offer
            price = (offer or {}).get("price") or (offer or {}).get("lowPrice")
            if price:
                return f"${float(price):,.2f}"
    return ""


def text_price(text):
    p = PRICE.search(text)
    return (p.group(1) or p.group(2)) if p else ""


def main():
    cfg = (yaml.safe_load(Path("config.yaml").read_text()).get("binnys") or {})
    stores = cfg.get("stores") or {}
    pages = cfg.get("pages") or {}
    try:
        old = json.loads(OUT_FILE.read_text()).get("bottles", {})
    except (OSError, ValueError):
        old = {}

    results = {b: {} for b in pages}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for store, store_id in stores.items():
            ctx = browser.new_context(user_agent=UA)
            ctx.add_cookies([{"name": "current-store", "value": str(store_id),
                              "domain": "www.binnys.com", "path": "/"}])
            page = ctx.new_page()
            for bottle, url in pages.items():
                try:
                    r = read_page(page, url, store)
                    print(f"[Binny's {store}] {bottle}: {r['status']} {r['price']} {r['shelf']}")
                except Exception as e:
                    print(f"[Binny's {store}] {bottle}: error: {e}")
                    # keep this store's last result for this bottle
                    r = ((old.get(bottle) or {}).get("stores") or {}).get(store)
                if r:
                    results[bottle][store] = r
            ctx.close()
        browser.close()

    bottles = {}
    for bottle, url in pages.items():
        per = results[bottle]
        order = list(stores)
        # cheapest in-stock store; on a tie, the store listed first in config.yaml
        in_stock = [(price_value(r["price"]), order.index(s), s) for s, r in per.items()
                    if r["in_stock"] and price_value(r["price"]) is not None and s in order]
        best = None
        if in_stock:
            val, _, s = min(in_stock)
            best = {"store": s, "price": per[s]["price"], "status": per[s]["status"],
                    "shelf": per[s]["shelf"]}
        bottles[bottle] = {"url": url, "best": best, "stores": per}

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_FILE.write_text(json.dumps(
        {"updated": now, "stores": list(stores), "bottles": bottles}, indent=2))


if __name__ == "__main__":
    main()
