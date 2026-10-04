"""Binny's stock at a few chosen stores.

Binny's search is off-limits to bots (robots.txt), but product pages are
allowed. Each bottle in config.yaml's binnys.pages is loaded once per store,
with Binny's own "current-store" cookie set the way its store picker does,
and the page's "IN STOCK @ <store>" line and price are read.

Writes binnys.json for the web page. Emails only for bottles in config.yaml's
email_below, when a store's price first drops under that amount.
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
    info = product_info(page)
    return {
        "status": status.capitalize(),
        "in_stock": in_stock,
        "shelf": shelf.title() if in_stock else "",
        "price": info.get("price") or text_price(text),
        "name": info.get("name", ""),
        "image": info.get("image") or og_image(page),
    }


def og_image(page):
    """The page's share image (Binny's product data has no photo)."""
    el = page.query_selector('meta[property="og:image"]')
    url = (el.get_attribute("content") or "") if el else ""
    return url if url.startswith("https://") else ""


def product_info(page):
    """Price, name and photo from the page's structured product data."""
    for raw in page.eval_on_selector_all('script[type="application/ld+json"]',
                                         "els => els.map(e => e.textContent)"):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if d.get("@type") != "Product":
                continue
            offer = d.get("offers")
            offer = offer[0] if isinstance(offer, list) else offer
            price = (offer or {}).get("price") or (offer or {}).get("lowPrice")
            image = d.get("image")
            image = image[0] if isinstance(image, list) and image else image
            return {"price": f"${float(price):,.2f}" if price else "",
                    "name": d.get("name", ""),
                    "image": image if isinstance(image, str) else ""}
    return {}


def text_price(text):
    p = PRICE.search(text)
    return (p.group(1) or p.group(2)) if p else ""


def main():
    full = yaml.safe_load(Path("config.yaml").read_text())
    cfg = full.get("binnys") or {}
    email_below = full.get("email_below") or {}
    stores = cfg.get("stores") or {}
    pages = cfg.get("pages") or {}
    try:
        old = json.loads(OUT_FILE.read_text()).get("bottles", {})
    except (OSError, ValueError):
        old = {}

    results = {b: {} for b in pages}
    blocked = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # One browsing session for everything, switching the store the way
        # Binny's store picker does, with a pause between pages.
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        for store, store_id in stores.items():
            ctx.add_cookies([{"name": "current-store", "value": str(store_id),
                              "domain": "www.binnys.com", "path": "/"}])
            for bottle, url in pages.items():
                r = None
                if not blocked:
                    try:
                        r = read_page(page, url, store)
                        print(f"[Binny's {store}] {bottle}: {r['status']} {r['price']} {r['shelf']}")
                    except Exception as e:
                        print(f"[Binny's {store}] {bottle}: error: {e}")
                        # Binny's security check: stop for this run, don't retry
                        blocked = "Just a moment" in str(e)
                    page.wait_for_timeout(5000)
                if not r:
                    # keep this store's last result for this bottle
                    r = ((old.get(bottle) or {}).get("stores") or {}).get(store)
                if r:
                    results[bottle][store] = r
        if blocked:
            print("[Binny's] security check shown; stopped for this run")
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
                    "shelf": per[s]["shelf"], "name": per[s].get("name", ""),
                    "image": per[s].get("image", "")}
        bottles[bottle] = {"url": url, "best": best, "stores": per}

    # email_below: email once when a store's in-stock price drops under the amount
    alerts = []
    for bottle, below in email_below.items():
        for s, r in (bottles.get(bottle) or {}).get("stores", {}).items():
            def under(x):
                v = price_value((x or {}).get("price"))
                return bool(x and x.get("in_stock") and v is not None and v < float(below))
            if under(r) and not under(((old.get(bottle) or {}).get("stores") or {}).get(s)):
                alerts.append({"bottle": f"{bottle} (under ${float(below):,.2f})",
                               "store": f"Binny's {s}" + (f" ({r['shelf']})" if r.get("shelf") else ""),
                               "url": bottles[bottle]["url"], "price": r["price"], "size": "750 ml"})
    if alerts:
        from checker import send_email
        send_email(alerts)

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_FILE.write_text(json.dumps(
        {"updated": now, "stores": list(stores), "bottles": bottles}, indent=2))


if __name__ == "__main__":
    main()
