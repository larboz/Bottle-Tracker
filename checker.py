import datetime
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
DATA_FILE = Path("data.json")
SOLD_OUT = re.compile(r"sold out|out of stock|unavailable", re.I)
NO_RESULTS = re.compile(r"couldn.t find any results", re.I)


def tokens(s):
    s = s.lower().replace("'", "").replace("\u2019", "")
    s = re.sub(r"(?<=[a-z])\.", "", s)
    return re.findall(r"[a-z0-9]+", s)


DEFAULT_EXCLUDE = ["cigar", "cigars", "rum", "rums", "glass", "glasses",
                   "glassware", "taster", "gift", "shirt", "hat", "candle",
                   # tequila/mezcal/etc. aged in whiskey barrels use the brand names
                   "tequila", "reposado", "anejo", "re:ejo", "blanco", "cristalino",
                   "mezcal", "vodka", "gin", "cognac"]


def split_alt(alt):
    """'George Stagg !jr' -> ('George Stagg', {'jr'}). Words starting with !
    rule out a listing for this bottle only."""
    keep, neg = [], set()
    for w in alt.split():
        if w.startswith("!"):
            neg.update(tokens(w[1:]))
        else:
            keep.append(w)
    return " ".join(keep), neg


def display_name(bottle):
    """First spelling of a bottle entry, without any !words."""
    return split_alt(bottle.split("|")[0])[0]


def min_window(words, need):
    """Length of the shortest run of words containing every word in need."""
    best = None
    counts = {}
    have = 0
    left = 0
    for right, w in enumerate(words):
        if w in need:
            counts[w] = counts.get(w, 0) + 1
            if counts[w] == 1:
                have += 1
        while have == len(need):
            span = right - left + 1
            best = span if best is None else min(best, span)
            lw = words[left]
            if lw in need:
                counts[lw] -= 1
                if counts[lw] == 0:
                    have -= 1
            left += 1
    return best


def matches(bottle, title, exclude=None):
    """True if the bottle's words appear close together in the title.
    Words can be in any order but must sit within one word of each other
    (so 'Weller Single Barrel' will not match 'Single Barrel Cigar Co Weller')."""
    words = tokens(title)
    # "Bottle A + Bottle B" listings are bundles
    if exclude is not None and re.search(r"\s\+\s", title):
        return False
    if exclude is not None:
        bad_words, bad_patterns = exclude
        for w in words:
            if w in bad_words or any(p.fullmatch(w) for p in bad_patterns):
                return False
    for alt in bottle.split("|"):
        alt, neg = split_alt(alt)
        if neg & set(words):
            continue
        need = set(tokens(alt))
        if not need:
            continue
        span = min_window(words, need)
        if span is not None and span <= len(need) + 1:
            return True
    return False


def exclude_set(store):
    """Words (and optional 're:' patterns) that disqualify a product.
    Defaults plus anything in the store's exclude_extra list."""
    entries = list(store.get("exclude", DEFAULT_EXCLUDE)) + list(store.get("exclude_extra", []))
    words, patterns = set(), []
    for e in entries:
        if str(e).startswith("re:"):
            patterns.append(re.compile(str(e)[3:]))
        else:
            words.update(tokens(str(e)))
    return words, patterns


def name_from_href(href):
    parts = urllib.parse.urlparse(href).path.rstrip("/").split("/")
    if not parts or not parts[-1]:
        return ""
    slug = parts[-2] if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{24}", parts[-1]) else parts[-1]
    return slug.replace("-", " ").title()


def base_url(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def shopify_products(base):
    out = []
    for page in range(1, 101):
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


def check_shopify(base, bottles, products, store):
    hits = []
    for bottle in bottles:
        for p in products:
            if matches(bottle, p["title"], exclude_set(store)):
                variants = p.get("variants", [])
                in_stock = any(v.get("available") for v in variants)
                prices = [float(v["price"]) for v in variants if v.get("price")]
                imgs = p.get("images") or []
                size = re.search(r"\b\d+(?:\.\d+)?\s?(?:ml|l)\b", p["title"], re.I)
                hits.append({
                    "size": size.group(0) if size else "",
                    "bottle": bottle,
                    "title": p["title"],
                    "name": p["title"],
                    "image": imgs[0].get("src", "") if imgs else "",
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
            if text and matches(bottle, text, exclude_set(store)):
                after = re.sub(r"<[^>]+>", " ", r.text[m.end():m.end() + 500])
                hits.append({
                    "bottle": bottle,
                    "title": text[:100],
                    "url": urllib.parse.urljoin(base, href),
                    "in_stock": not SOLD_OUT.search(text + " " + after),
                    "price": "",
                })
    return hits


TILE_JS = """
(els, m) => {
  const prod = els.filter(e => e.href.includes(m));
  return prod.map(a => {
    let tile = a;
    for (let depth = 0; depth < 6 && tile.parentElement; depth++) {
      const p = tile.parentElement;
      const other = prod.some(e => e.href !== a.href && p.contains(e));
      if (other) break;
      tile = p;
    }
    const alts = [...tile.querySelectorAll('img')].map(i => i.alt).join(' ');
    const label = (a.getAttribute('aria-label') || '') + ' ' + (a.title || '');
    const imgs = [...tile.querySelectorAll('img')]
      .map(i => i.currentSrc || i.src || i.getAttribute('data-src') || '')
      .filter(Boolean);
    const image = imgs.find(u => u.includes('/products/')) || imgs[0] || '';
    return {href: a.href, image: image,
            text: (tile.innerText || '') + ' ' + alts + ' ' + label};
  });
}
"""


def check_browser(store, base, bottles):
    """For JS-rendered stores (e.g. City Hive). Uses a headless browser."""
    from playwright.sync_api import sync_playwright

    marker = store.get("product_href", "/product")
    hits = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(user_agent=UA["User-Agent"])
        queries = [(display_name(b), a.strip())
                   for b in bottles for a in b.split("|") if a.strip()]
        for display, bottle in queries:
            query = split_alt(bottle)[0]
            url = store["search_url"].format(q=urllib.parse.quote_plus(query))
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000)
            tiles = page.eval_on_selector_all("a", TILE_JS, marker)
            # Big stores always return close matches, so a good page has
            # product tiles. Smaller ones may say "couldn't find any results".
            # No tiles and no such message = page didn't load right; skip this run.
            if not tiles and NO_RESULTS.search(page.inner_text("body")):
                print(f"[{store['name']}] '{bottle}': no search results")
                continue
            if not tiles:
                browser.close()
                raise RuntimeError(f"no product tiles for '{bottle}' (page not loaded, or product_href wrong)")
            print(f"[{store['name']}] '{bottle}': {len(tiles)} product tiles on page")
            seen = set()
            for t in tiles:
                text = " ".join((t["text"] or "").split())
                if t["href"] in seen:
                    continue
                if matches(bottle, text, exclude_set(store)):
                    seen.add(t["href"])
                    price = re.search(r"\$\d[\d,]*\.\d\d", text)
                    size = re.search(r"\b\d+(?:\.\d+)?\s?(?:mL|L|oz)\b", text)
                    hits.append({
                        "bottle": display,
                        "title": text[:100],
                        "name": name_from_href(t["href"]) or display,
                        "image": t.get("image", ""),
                        "url": t["href"],
                        "in_stock": not SOLD_OUT.search(text),
                        "price": price.group(0) if price else "",
                        "size": size.group(0) if size else "",
                    })
            if not seen:
                shown = [" ".join((t["text"] or "").split())[:70] for t in tiles][:6]
                print(f"[{store['name']}] '{bottle}': NOT LISTED. Tiles shown: {shown}")
        browser.close()
    return hits


def over_msrp(h, cfg):
    """True if the price is more than max_over_msrp above the bottle's MSRP.
    Bottles with no MSRP listed, or hits with no price, always pass."""
    msrp = (cfg.get("msrp") or {}).get(display_name(h["bottle"]))
    price = re.sub(r"[^\d.]", "", h.get("price") or "")
    if not msrp or not price:
        return False
    return float(price) > float(msrp) * (1 + cfg.get("max_over_msrp", 0.25))


def watch_info(cfg):
    """What the page lists at the bottom: bottles, MSRPs, stores, price limit."""
    msrp = cfg.get("msrp") or {}
    names, stores = [], []
    for store in cfg["stores"]:
        stores.append({"name": store["name"], "url": store["url"]})
        for b in store["bottles"]:
            n = display_name(b)
            if n not in names:
                names.append(n)
    return {
        "bottles": [{"name": n, "msrp": msrp.get(n)} for n in names],
        "stores": stores,
        "max_over_msrp": cfg.get("max_over_msrp", 0.25),
    }


def send_email(alerts):
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("ALERT_TO", user)

    first = alerts[0]
    price = f" ({first['price']})" if first["price"] else ""
    if len(alerts) == 1:
        subject = f"In stock: {first['bottle']}{price} at {first['store']}"
    else:
        subject = f"In stock: {first['bottle']} + {len(alerts) - 1} more"

    blocks = []
    for a in alerts:
        lines = [a["bottle"]]
        detail = " | ".join(x for x in (a["price"], a["size"], a["store"]) if x)
        if detail:
            lines.append(detail)
        lines.append(a["url"])
        blocks.append("\n".join(lines))
    intro = "Good news. This just came into stock:" if len(alerts) == 1 \
        else "Good news. These just came into stock:"
    body = intro + "\n\n" + "\n\n".join(blocks) + "\n\nMove fast, allocated bottles go quickly.\n"

    if not (user and pw):
        print(f"No SMTP creds; would send:\nSubject: {subject}\n\n{body}")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
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
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    old_items = []
    if DATA_FILE.exists():
        try:
            old_items = json.loads(DATA_FILE.read_text()).get("items", [])
        except ValueError:
            old_items = []
    since = {(i["store"], i["url"]): i.get("since", now_iso) for i in old_items}
    items = {}
    ok_stores = set()

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
                hits = check_shopify(base, store["bottles"], products, store)
            elif store.get("search_url"):
                hits = check_search(store, base, store["bottles"])
            else:
                print(f"[{name}] Not Shopify; add a search_url. Skipping.")
                continue
        except Exception as e:
            print(f"[{name}] error: {e}")
            continue  # keep old state for this store

        ok_stores.add(name)
        seen = set()
        for h in hits:
            if h["in_stock"] and over_msrp(h, cfg):
                print(f"[{name}] {h['title']}: IN at {h['price']}, over MSRP limit, skipping")
                h["in_stock"] = False
            if h["in_stock"]:
                items[(name, h["url"])] = {
                    "name": h.get("name") or h["bottle"],
                    "bottle": h["bottle"], "store": name, "url": h["url"],
                    "image": h.get("image", ""), "price": h["price"],
                    "size": h.get("size", ""),
                    "since": since.get((name, h["url"]), now_iso),
                }
            key = f"{name}|{h['url']}"
            seen.add(key)
            was = old.get(key, False)
            new[key] = h["in_stock"]
            print(f"[{name}] {h['title']}: {'IN' if h['in_stock'] else 'out'}")
            quiet = display_name(h["bottle"]) in (cfg.get("no_email") or [])
            if h["in_stock"] and not was and not quiet:
                alerts.append({
                    "bottle": h["bottle"], "store": name, "url": h["url"],
                    "price": h["price"], "size": h.get("size", ""),
                })
        # anything previously tracked for this store but no longer listed -> out
        for key in list(new):
            if key.startswith(f"{name}|") and key not in seen:
                new[key] = False

    # keep last known items for any store that errored this run
    for i in old_items:
        if i["store"] not in ok_stores:
            items[(i["store"], i["url"])] = i
    if alerts:
        send_email(alerts)
    STATE_FILE.write_text(json.dumps(new, indent=2, sort_keys=True))
    DATA_FILE.write_text(json.dumps(
        {"updated": now_iso, "items": sorted(items.values(), key=lambda x: x["name"]),
         "watch": watch_info(cfg)},
        indent=2))


if __name__ == "__main__":
    main()
