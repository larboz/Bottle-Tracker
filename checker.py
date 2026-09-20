import html
import json
import os
import re
import smtplib
import urllib.parse
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

UA = {"User-Agent": "Mozilla/5.0 (compatible; BottleWatch/1.0)"}
STATE_FILE = Path("state.json")
SOLD_OUT = re.compile(r"sold out|out of stock|unavailable", re.I)


def tokens(s):
    s = s.lower().replace("'", "").replace("\u2019", "")
    return re.findall(r"[a-z0-9]+", s)


def matches(bottle, title):
    have = set(tokens(title))
    return all(w in have for w in tokens(bottle))


def base_url(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def shopify_products(base):
    out = []
    for page in range(1, 21):
        r = requests.get(f"{base}/products.json",
                         params={"limit": 250, "page": page},
                         headers=UA, timeout=30)
        if r.status_code != 200:
            return None if page == 1 else out
        try:
            prods = r.json().get("products", [])
        except ValueError:
            return None
        if not prods:
            break
        out += prods
    return out


def check_shopify(base, bottles, products):
    hits = []
    for bottle in bottles:
        for p in products:
            if matches(bottle, p["title"]):
                variants = p.get("variants", [])
                in_stock = any(v.get("available") for v in variants)
                prices = [float(v["price"]) for v in variants if v.get("price")]
                hits.append({
                    "bottle": bottle,
                    "title": p["title"],
                    "url": f"{base}/products/{p['handle']}",
                    "in_stock": in_stock,
                    "price": f"${min(prices):.2f}" if prices else "",
                })
    return hits


def check_search(store, base, bottles):
    hits = []
    for bottle in bottles:
        url = store["search_url"].format(q=urllib.parse.quote_plus(bottle))
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        for m in re.finditer(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>',
                             r.text, re.S | re.I):
            href, inner = m.groups()
            text = html.unescape(re.sub(r"<[^>]+>", " ", inner))
            text = " ".join(text.split())
            if text and matches(bottle, text):
                after = re.sub(r"<[^>]+>", " ", r.text[m.end():m.end() + 500])
                hits.append({
                    "bottle": bottle,
                    "title": text[:100],
                    "url": urllib.parse.urljoin(base, href),
                    "in_stock": not SOLD_OUT.search(text + " " + after),
                    "price": "",
                })
    return hits


def check_browser(store, base, bottles):
    """For JS-rendered stores (e.g. City Hive). Uses a headless browser."""
    from playwright.sync_api import sync_playwright

    marker = store.get("product_href", "/product")
    hits = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=UA["User-Agent"])
        for bottle in bottles:
            url = store["search_url"].format(q=urllib.parse.quote_plus(bottle))
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000)
            links = page.eval_on_selector_all(
                "a", "els => els.map(e => ({href: e.href, text: e.innerText}))")
            # The site always returns close matches, so a good page has
            # product links. None = page didn't load right; skip this run.
            if not any(marker in l["href"] for l in links):
                sample = [l["href"] for l in links][:25]
                browser.close()
                raise RuntimeError(
                    f"no product links for '{bottle}' (page not loaded, or "
                    f"product_href wrong). Sample links: {sample}")
            seen = set()
            n_products = sum(1 for l in links if marker in l["href"])
            print(f"[{store['name']}] '{bottle}': {n_products} product links on page")
            for l in links:
                text = " ".join((l["text"] or "").split())
                if l["href"] in seen or marker not in l["href"]:
                    continue
                if text and matches(bottle, text):
                    seen.add(l["href"])
                    hits.append({
                        "bottle": bottle,
                        "title": text[:100],
                        "url": l["href"],
                        "in_stock": not SOLD_OUT.search(text),
                        "price": "",
                    })
            if not seen:
                shown = [" ".join((l["text"] or "").split())[:60]
                         for l in links if marker in l["href"]][:5]
                print(f"[{store['name']}] '{bottle}': NOT LISTED. Closest shown: {shown}")
        browser.close()
    return hits


def send_email(lines):
    body = "In stock now:\n\n" + "\n\n".join(lines)
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("ALERT_TO", user)
    if not (user and pw):
        print("No SMTP creds; would send:\n" + body)
        return
    msg = EmailMessage()
    msg["Subject"] = f"Bottle alert: {len(lines)} in stock"
    msg["From"], msg["To"] = user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def main():
    cfg = yaml.safe_load(Path("config.yaml").read_text())
    old = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    new = dict(old)
    alerts = []

    for store in cfg["stores"]:
        name, base = store["name"], base_url(store["url"])
        try:
            if store.get("render"):
                hits = check_browser(store, base, store["bottles"])
                products = None
            else:
                products = None if store.get("search_url") else shopify_products(base)
            if store.get("render"):
                pass
            elif products is not None:
                hits = check_shopify(base, store["bottles"], products)
            elif store.get("search_url"):
                hits = check_search(store, base, store["bottles"])
            else:
                print(f"[{name}] Not Shopify; add a search_url. Skipping.")
                continue
        except Exception as e:
            print(f"[{name}] error: {e}")
            continue  # keep old state for this store

        seen = set()
        for h in hits:
            key = f"{name}|{h['url']}"
            seen.add(key)
            was = old.get(key, False)
            new[key] = h["in_stock"]
            print(f"[{name}] {h['title']}: {'IN' if h['in_stock'] else 'out'}")
            if h["in_stock"] and not was:
                alerts.append(f"{h['title']} {h['price']}\n{name}\n{h['url']}")
        # anything previously tracked for this store but no longer listed -> out
        for key in list(new):
            if key.startswith(f"{name}|") and key not in seen:
                new[key] = False

    if alerts:
        send_email(alerts)
    STATE_FILE.write_text(json.dumps(new, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
